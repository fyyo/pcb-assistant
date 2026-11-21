"""Actual offline LoRA optimization with held-out loss, checkpoints, and no retrieval."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import sys
import time

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['WANDB_DISABLED'] = 'true'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ.setdefault('PYTORCH_CUDA_ALLOC_CONF', 'expandable_segments:True')

from learning_common import ROOT, SYSTEM_PROMPT, digest_file, dump_json, emit


def jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]


def instruction_tokens(tokenizer, row, max_length):
    if row.get('preserve_complete_example'):
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': row['prompt']}]
        prefix = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
        full = tokenizer.apply_chat_template(messages + [{'role': 'assistant', 'content': row['response']}],
                                              tokenize=True, add_generation_prompt=False)
        if full[:len(prefix)] != prefix or len(full) <= len(prefix) + 8:
            raise ValueError('invalid_complete_instruction')
        if len(full) > max_length:
            raise ValueError('complete_instruction_exceeds_sequence_length')
        return {'input_ids': full, 'labels': [-100] * len(prefix) + full[len(prefix):]}
    # Reserve explicit space for the assistant target; never train prompt-only samples.
    prompt_ids = tokenizer.encode(row['prompt'], add_special_tokens=False)
    budget = max(32, max_length // 2 - 90)
    if len(prompt_ids) > budget:
        # Keep both the initial known conditions and the final task instruction.
        prompt_ids = prompt_ids[:max(1, budget - 32)] + prompt_ids[-32:]
    prompt = tokenizer.decode(prompt_ids, skip_special_tokens=True)
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': prompt}]
    prefix = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    full = tokenizer.apply_chat_template(messages + [{'role': 'assistant', 'content': row['response']}],
                                        tokenize=True, add_generation_prompt=False)
    if full[:len(prefix)] != prefix:
        raise ValueError('chat_template_prefix_mismatch')
    full = full[:max_length]
    if len(full) <= len(prefix) + 8:
        raise ValueError('insufficient_assistant_tokens')
    return {'input_ids': full, 'labels': [-100] * len(prefix) + full[len(prefix):]}


def prepare_tokens(tokenizer, data, max_length, instruction_only=False):
    datasets = {}
    source_token_counts = {}
    for split in ('train', 'validation'):
        stream = []
        rows = [] if instruction_only and split == 'train' else jsonl(data / f'{split}_domain.jsonl')
        random.Random(20261006).shuffle(rows)
        for row in rows:
            encoded = tokenizer.encode(row['text'], add_special_tokens=False)
            kind = {'untrusted_attachment': 'attachment', 'reported_engineering_case': 'case'}.get(row.get('source_kind'), 'email')
            counter_key = split + '_' + kind + '_tokens'
            source_token_counts[counter_key] = source_token_counts.get(counter_key, 0) + len(encoded) + 1
            stream.extend(encoded)
            stream.append(tokenizer.eos_token_id)
        domain = []
        for start in range(0, len(stream) - 16, max_length):
            ids = stream[start:start + max_length]
            domain.append({'input_ids': ids, 'labels': ids})
        instructions = [instruction_tokens(tokenizer, row, max_length)
                        for row in jsonl(data / f'{split}_instruction.jsonl')]
        if (not domain and not (instruction_only and split == 'train')) or not instructions:
            raise ValueError('empty_training_phase')
        datasets[split + '_domain'] = domain
        datasets[split + '_instruction'] = instructions
        emit('tokenized', split=split, domain_tokens=len(stream), domain_batches=len(domain), instruction_samples=len(instructions))
    datasets['_source_token_counts'] = source_token_counts
    emit('training_source_tokens', **source_token_counts)
    return datasets


def run(args):
    import torch
    from peft import LoraConfig, get_peft_model, set_peft_model_state_dict
    from safetensors.torch import load_file
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from transformers.utils import logging
    logging.set_verbosity_error()
    os.umask(0o077)
    torch.set_num_threads(4)
    if not torch.cuda.is_available():
        raise RuntimeError('cuda_unavailable')
    policy = json.loads((ROOT / 'training_policy.json').read_text())
    if policy['local_model_training_allowed'] is not True or policy['mail_upload_allowed'] is not False:
        raise ValueError('training_policy_mismatch')
    source_report = json.loads((args.data / 'report.json').read_text())
    for name, artifact in source_report['artifacts'].items():
        if digest_file(args.data / name) != artifact['sha256']:
            raise ValueError('training_data_checksum_mismatch')
    base_spec = json.loads((ROOT / 'base_model.json').read_text())
    for name in ('model.safetensors', 'config.json', 'tokenizer.json', 'tokenizer_config.json'):
        if digest_file(args.base / name) != base_spec['files'][name][1]:
            raise ValueError('base_model_checksum_mismatch')
    output = ROOT / 'data' / args.run_name
    output.mkdir(parents=True, exist_ok=args.resume, mode=0o700)
    if args.resume and not (output / 'run_config.json').exists():
        raise ValueError('resume_run_missing')
    configuration = {'base_model': base_spec['model_id'], 'base_revision': base_spec['revision'],
                     'data_report_sha256': digest_file(args.data / 'report.json'),
                     'sequence_length': args.sequence_length, 'domain_steps': args.domain_steps,
                     'instruction_epochs': args.instruction_epochs, 'instruction_limit': args.instruction_limit,
                     'gradient_accumulation': args.accumulation, 'lora_rank': 8,
                     'validation_batches': args.validation_batches, 'seed': 20261006}
    parent_report = None
    if args.initial_run:
        if not (source_report.get('case_study') or source_report.get('public_curriculum')):
            raise ValueError('declared_curriculum_required_for_continuation')
        from continuous_common import pointer_for, verify_pointer
        parent_pointer = pointer_for(args.initial_run)
        verify_pointer(parent_pointer)
        parent_report = json.loads((args.initial_run / 'report.json').read_text())
        if parent_report['configuration']['base_revision'] != base_spec['revision']:
            raise ValueError('parent_base_revision_mismatch')
        parent_scale = json.loads((args.initial_run / 'inference_config.json').read_text())['adapter_scale']
        configuration.update({'initial_model': parent_pointer, 'instruction_only': True,
                              'adapter_scale': parent_scale, 'instruction_learning_rate': args.instruction_learning_rate,
                              'trainer_sha256': digest_file(__file__)})
    if args.resume and json.loads((output / 'run_config.json').read_text()) != configuration:
        raise ValueError('resume_configuration_mismatch')
    dump_json(output / 'run_config.json', configuration)
    random.seed(configuration['seed'])
    torch.manual_seed(configuration['seed'])
    torch.cuda.manual_seed_all(configuration['seed'])
    tokenizer = AutoTokenizer.from_pretrained(args.base, local_files_only=True, trust_remote_code=False)
    datasets = prepare_tokens(tokenizer, args.data, args.sequence_length, instruction_only=bool(args.initial_run))
    model = AutoModelForCausalLM.from_pretrained(args.base, local_files_only=True, trust_remote_code=False,
                                               use_safetensors=True, torch_dtype=torch.float16,
                                               attn_implementation='sdpa', low_cpu_mem_usage=True)
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(r=8, lora_alpha=16, lora_dropout=.05,
                                           target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj'],
                                           bias='none', task_type='CAUSAL_LM'))
    if args.initial_run:
        from peft.tuners.lora.layer import LoraLayer
        parent_weights = load_file(args.initial_run / parent_report['selected_adapter'] / 'adapter_model.safetensors')
        loaded = set_peft_model_state_dict(model, parent_weights)
        if loaded.unexpected_keys or any('lora_' in name for name in loaded.missing_keys):
            raise ValueError('parent_adapter_shape_mismatch')
        for module in model.modules():
            if isinstance(module, LoraLayer):
                module.set_scale('default', parent_scale)
    model.to('cuda')
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    model.enable_input_require_grads()
    trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
    initial = {name: parameter.detach().cpu().clone() for name, parameter in model.named_parameters() if parameter.requires_grad}
    parameters = sum(parameter.numel() for parameter in trainable)
    emit('model_loaded', trainable_parameters=parameters, total_parameters=sum(p.numel() for p in model.parameters()),
         gpu_memory_allocated_mib=round(torch.cuda.memory_allocated() / 2**20))
    started = time.monotonic()
    state = {'phase': 'instruction' if args.initial_run else 'domain', 'next_update': 0,
             'optimizer_updates': 0, 'trained_tokens': 0, 'skipped_updates': 0}
    evaluations = {}
    checkpoint_pointer = output / 'latest_checkpoint.json'
    resume_optimizer = None
    if args.resume and checkpoint_pointer.exists():
        pointer = json.loads(checkpoint_pointer.read_text())
        checkpoint = (output / pointer['directory']).resolve()
        if checkpoint.parent != (output / 'checkpoints').resolve():
            raise ValueError('invalid_checkpoint_path')
        for name, checksum in pointer['sha256'].items():
            if digest_file(checkpoint / name) != checksum:
                raise ValueError('checkpoint_checksum_mismatch')
        saved = torch.load(checkpoint / 'optimizer.pt', map_location='cpu', weights_only=True)
        state = json.loads((checkpoint / 'state.json').read_text())
        adapter = load_file(checkpoint / 'adapter_model.safetensors', device='cpu')
        set_peft_model_state_dict(model, adapter)
        resume_optimizer = saved
        torch.set_rng_state(saved['cpu_rng'])
        torch.cuda.set_rng_state(saved['cuda_rng'])
        evaluations = json.loads((output / 'evaluations.json').read_text())
        emit('checkpoint_resumed', phase=state['phase'], next_update=state['next_update'])

    def batch(row):
        ids = torch.tensor([row['input_ids']], device='cuda', dtype=torch.long)
        labels = torch.tensor([row['labels']], device='cuda', dtype=torch.long)
        return {'input_ids': ids, 'labels': labels, 'attention_mask': torch.ones_like(ids)}

    def evaluate():
        model.eval()
        results = {}
        with torch.inference_mode():
            for kind in ('domain', 'instruction'):
                rows = datasets['validation_' + kind]
                selection = random.Random(20261007).sample(range(len(rows)), min(args.validation_batches, len(rows)))
                total, count = 0.0, 0
                for index in selection:
                    row = rows[index]
                    targets = sum(label != -100 for label in row['labels'][1:])
                    with torch.autocast('cuda', dtype=torch.float16):
                        loss = model(**batch(row)).loss
                    if not torch.isfinite(loss):
                        raise ValueError('nonfinite_validation_loss')
                    total += float(loss) * targets
                    count += targets
                results[kind + '_loss'] = total / count
                results[kind + '_target_tokens'] = count
        model.train()
        return results

    if not evaluations:
        emit('baseline_evaluation_started')
        evaluations['baseline'] = evaluate()
        dump_json(output / 'evaluations.json', evaluations)
        emit('baseline_evaluated', **evaluations['baseline'])
    if args.initial_run and not (output / 'domain_adapter').exists():
        # Keep the original served parent adapter, including on checkpoint resume.
        import shutil
        shutil.copytree(args.initial_run / parent_report['selected_adapter'], output / 'domain_adapter')
        evaluations['after_domain'] = evaluations['baseline']
        dump_json(output / 'evaluations.json', evaluations)

    def save_checkpoint(optimizer, scaler):
        checkpoint = output / 'checkpoints' / f"{state['phase']}_{state['next_update']}_{time.time_ns()}"
        checkpoint.mkdir(parents=True)
        # A new generation is published only after all its files have been verified.
        model.save_pretrained(checkpoint, safe_serialization=True)
        temp = checkpoint / 'optimizer.pt.tmp'
        torch.save({'optimizer': optimizer.state_dict(), 'scaler': scaler.state_dict(),
                    'cpu_rng': torch.get_rng_state(), 'cuda_rng': torch.cuda.get_rng_state()}, temp)
        temp.replace(checkpoint / 'optimizer.pt')
        dump_json(checkpoint / 'state.json', state)
        hashes = {name: digest_file(checkpoint / name) for name in
                  ('adapter_model.safetensors', 'adapter_config.json', 'optimizer.pt', 'state.json')}
        previous = json.loads(checkpoint_pointer.read_text())['directory'] if checkpoint_pointer.exists() else None
        dump_json(checkpoint_pointer, {'directory': str(checkpoint.relative_to(output)), 'sha256': hashes})
        # The committed checkpoint plus its predecessor support recovery without
        # accumulating every optimizer snapshot over years of learning cycles.
        import re
        import shutil
        keep = {str(checkpoint.relative_to(output)), previous}
        for old in (output / 'checkpoints').iterdir():
            if (old.is_dir() and not old.is_symlink() and re.fullmatch(r'(?:domain|instruction)_\d+_\d+', old.name)
                    and str(old.relative_to(output)) not in keep):
                shutil.rmtree(old)

    for phase in ('domain', 'instruction'):
        if state['phase'] == 'instruction' and phase == 'domain':
            continue
        rows = datasets['train_' + phase]
        order = list(range(len(rows)))
        random.Random(20261006 if phase == 'domain' else 20261008).shuffle(order)
        if phase == 'domain':
            updates = args.domain_steps or math.ceil(len(order) / args.accumulation)
            order = (order * math.ceil(updates * args.accumulation / len(order)))[:updates * args.accumulation]
        else:
            if args.instruction_limit:
                order = order[:args.instruction_limit]
            order *= args.instruction_epochs
            updates = math.ceil(len(order) / args.accumulation)
        learning_rate = 1e-4 if phase == 'domain' else (args.instruction_learning_rate if args.initial_run else 5e-5)
        optimizer = torch.optim.AdamW(trainable, lr=learning_rate, weight_decay=.01)
        scaler = torch.amp.GradScaler('cuda')
        start_update = state['next_update'] if state['phase'] == phase else 0
        state['phase'] = phase
        if resume_optimizer is not None:
            optimizer.load_state_dict(resume_optimizer['optimizer'])
            scaler.load_state_dict(resume_optimizer['scaler'])
            resume_optimizer = None
        model.train()
        running_loss, running_count = 0.0, 0
        phase_started = time.monotonic()
        emit('training_phase_started', phase=phase, total_updates=updates, start_update=start_update)
        for update in range(start_update, updates):
            optimizer.zero_grad(set_to_none=True)
            warmup = max(1, min(10, updates // 10))
            rate = learning_rate * min(1.0, (update + 1) / warmup) * max(.1, .5 * (1 + math.cos(math.pi * update / max(1, updates))))
            for group in optimizer.param_groups:
                group['lr'] = rate
            indices = order[update * args.accumulation:(update + 1) * args.accumulation]
            current_tokens = 0
            for index in indices:
                row = rows[index]
                with torch.autocast('cuda', dtype=torch.float16):
                    loss = model(**batch(row)).loss
                if not torch.isfinite(loss):
                    raise ValueError('nonfinite_training_loss')
                running_loss += float(loss.detach())
                running_count += 1
                current_tokens += sum(label != -100 for label in row['labels'][1:])
                scaler.scale(loss / len(indices)).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            previous_scale = scaler.get_scale()
            scaler.step(optimizer)
            scaler.update()
            if scaler.get_scale() < previous_scale:
                state['skipped_updates'] += 1
            else:
                state['optimizer_updates'] += 1
                state['trained_tokens'] += current_tokens
            state['next_update'] = update + 1
            if (update + 1) % 5 == 0 or update + 1 == updates:
                elapsed = time.monotonic() - phase_started
                progress = {'status': 'training', **state, 'phase_total_updates': updates,
                            'mean_recent_loss': running_loss / running_count,
                            'elapsed_seconds': round(time.monotonic() - started),
                            'phase_seconds_per_update': elapsed / (update + 1 - start_update),
                            'peak_gpu_mib': round(torch.cuda.max_memory_allocated() / 2**20)}
                dump_json(output / 'progress.json', progress)
                emit('training_progress', **progress)
                running_loss, running_count = 0.0, 0
            if (update + 1) % 25 == 0 or update + 1 == updates:
                save_checkpoint(optimizer, scaler)
        evaluations['after_' + phase] = evaluate()
        dump_json(output / 'evaluations.json', evaluations)
        emit('phase_evaluated', phase=phase, **evaluations['after_' + phase])
        model.save_pretrained(output / (phase + '_adapter'), safe_serialization=True)
        state['phase'] = 'instruction'
        state['next_update'] = 0
        # The completed domain checkpoint can replay its zero remaining updates on resume.

    changes = {name: float((parameter.detach().cpu() - initial[name]).square().sum())
               for name, parameter in model.named_parameters() if name in initial}
    changed = sum(value > 0 for value in changes.values())
    if not changed or state['optimizer_updates'] == 0:
        raise ValueError('adapter_did_not_learn')
    baseline = evaluations['baseline']
    candidates = {phase: .5 * (evaluations['after_' + phase]['domain_loss'] / baseline['domain_loss']
                              + evaluations['after_' + phase]['instruction_loss'] / baseline['instruction_loss'])
                  for phase in ('domain', 'instruction')}
    best_phase = 'instruction' if args.initial_run else min(candidates, key=candidates.get)
    result = {'status': 'completed', 'method': 'domain_adaptation_then_instruction_lora',
              'base_model': base_spec['model_id'], 'configuration': configuration,
              'trainable_parameters': parameters, 'changed_parameter_tensors': changed,
              'source_token_counts': datasets['_source_token_counts'],
              'adapter_squared_change': sum(changes.values()), **state,
              'evaluations': evaluations, 'relative_validation_scores': candidates,
              'selected_adapter': best_phase + '_adapter',
              'adapter_artifacts': {phase + '_adapter': {
                  name: digest_file(output / (phase + '_adapter') / name)
                  for name in ('adapter_model.safetensors', 'adapter_config.json')}
                  for phase in ('domain', 'instruction')},
              'measured_loss_improved': candidates[best_phase] < 1,
              'elapsed_seconds': round(time.monotonic() - started),
              'peak_gpu_mib': round(torch.cuda.max_memory_allocated() / 2**20),
              'scope': ('continuous_local_small_model_training' if source_report.get('continuous') else 'first_local_small_model_training'), 'retrieval_used_for_inference': False,
              'limits': ['留出集损失下降只说明对该数据分布的适配，不证明工程判断正确或达到主任能力。',
                         ('领域训练集已完整遍历一轮；评测采用固定抽取的留出样本。' if args.domain_steps == 0
                         else '本轮按配置限制领域训练步数，未完整遍历全部领域文本。')]}
    if args.initial_run:
        result.update({'method': 'continue_served_adapter_with_case_instruction_replay',
                       'scope': 'existing_case_study', 'parent_model': configuration['initial_model'],
                       'inherited_source_token_counts': parent_report.get('inherited_source_token_counts', parent_report['source_token_counts']),
                       'inherits_attachment_training': parent_report.get('inherits_attachment_training',
                           parent_report.get('source_token_counts', {}).get('train_attachment_tokens', 0) > 0),
                       'cumulative_optimizer_updates': parent_report.get('cumulative_optimizer_updates', parent_report['optimizer_updates']) + state['optimizer_updates'],
                       'cumulative_trained_tokens': parent_report.get('cumulative_trained_tokens', parent_report['trained_tokens']) + state['trained_tokens'],
                       'limits': ['继承已部署模型及其对话强度；本轮完整遍历案例任务和原问答，不重复领域正文训练。',
                                  '案例目标来自原文及明确的证据边界；结果措辞不等于已核验的因果关系。',
                                  '是否部署由单独的新旧模型比较决定，留出损失下降不是工程能力认证。']})
        if source_report.get('public_curriculum'):
            result.update(method='continue_served_adapter_with_public_curriculum_and_replay',
                          scope='public_curriculum', public_curriculum=source_report['public_curriculum'],
                          limits=['公开课程为有出处的原创归纳；本轮不训练原始文档全文或保留评测。',
                                  '旧问答仅按预先确定的抽样计划复习，不代表重新遍历全部历史数据。',
                                  '参数更新和预测损失不等于独立工程能力验证；部署须单独比较。'])
    dump_json(output / 'report.json', result)
    if args.initial_run:
        dump_json(output / 'inference_config.json', {'adapter_scale': parent_scale,
                  'adapter_sha256': result['adapter_artifacts'][result['selected_adapter']]['adapter_model.safetensors'],
                  'reason': 'same_scale_as_parent_used_throughout_continued_training'})
    dump_json(output / 'progress.json', {'status': 'completed', 'optimizer_updates': state['optimizer_updates'],
                                       'trained_tokens': state['trained_tokens']})
    emit('model_training_completed', optimizer_updates=state['optimizer_updates'], trained_tokens=state['trained_tokens'],
         changed_parameter_tensors=changed, measured_loss_improved=result['measured_loss_improved'],
         selected_adapter=result['selected_adapter'], elapsed_seconds=result['elapsed_seconds'])


def main():
    parser = argparse.ArgumentParser(description='在本机 GPU 上训练压合工程 LoRA 小模型')
    parser.add_argument('--base', type=Path, default=ROOT / 'models/Qwen2.5-0.5B-Instruct')
    parser.add_argument('--data', type=Path, default=ROOT / 'data/training_data_v1')
    parser.add_argument('--run-name', default='model_training_v1')
    parser.add_argument('--domain-steps', type=int, default=120, help='0 表示完整领域训练集一轮')
    parser.add_argument('--instruction-epochs', type=int, default=1)
    parser.add_argument('--instruction-limit', type=int, default=0)
    parser.add_argument('--sequence-length', type=int, default=512)
    parser.add_argument('--accumulation', type=int, default=4)
    parser.add_argument('--validation-batches', type=int, default=32)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--initial-run', type=Path, help='从已部署 LoRA 继续完整案例问答训练，保留其强度')
    parser.add_argument('--instruction-learning-rate', type=float, default=1e-5)
    args = parser.parse_args()
    import re
    if (not re.fullmatch(r'[a-zA-Z0-9_-]+', args.run_name) or args.domain_steps < 0
            or args.instruction_epochs < 1 or args.instruction_limit < 0
            or not 256 <= args.sequence_length <= 1024 or args.accumulation < 1 or args.validation_batches < 1
            or not 0 < args.instruction_learning_rate <= 1e-3):
        parser.error('invalid training configuration')
    try:
        import fcntl
        lock_directory = ROOT / '.private'
        lock_directory.mkdir(exist_ok=True, mode=0o700)
        with (lock_directory / 'model_training.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run(args)
    except KeyboardInterrupt:
        emit('training_interrupted_checkpoint_available')
        return 130
    except Exception as exc:
        import traceback
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        emit('training_failed', error_type=type(exc).__name__,
             location=Path(frame.filename).name, function=frame.name, line=frame.lineno)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
