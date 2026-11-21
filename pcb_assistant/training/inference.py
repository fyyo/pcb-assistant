"""Use learned LoRA weights for local generation. No corpus search or database access."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

from pcb_assistant.training.common import ROOT, SYSTEM_PROMPT, digest_file, dump_json, emit


class LearnedEngineer:
    def __init__(self, run, base=None):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from transformers.utils import logging
        from peft import PeftModel
        logging.set_verbosity_error()
        torch.set_num_threads(4)
        self.torch = torch
        self.run = Path(run)
        self.report = json.loads((self.run / 'report.json').read_text())
        if self.report['status'] != 'completed' or self.report['optimizer_updates'] <= 0:
            raise ValueError('trained_model_not_ready')
        self.base = Path(base) if base else ROOT / 'models/Qwen2.5-0.5B-Instruct'
        name = self.report['selected_adapter']
        if name not in ('domain_adapter', 'instruction_adapter'):
            raise ValueError('invalid_adapter')
        for filename, checksum in self.report['adapter_artifacts'][name].items():
            if digest_file(self.run / name / filename) != checksum:
                raise ValueError('trained_adapter_checksum_mismatch')
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.tokenizer = AutoTokenizer.from_pretrained(self.base, local_files_only=True, trust_remote_code=False)
        base_model = AutoModelForCausalLM.from_pretrained(
            self.base, local_files_only=True, trust_remote_code=False, use_safetensors=True,
            torch_dtype=torch.float16 if self.device == 'cuda' else torch.float32,
            attn_implementation='sdpa', low_cpu_mem_usage=True)
        self.model = PeftModel.from_pretrained(base_model, self.run / name, local_files_only=True, is_trainable=False)
        self.model.to(self.device).eval()
        self.lock = threading.Lock()
        self.adapter_scale = 1.0
        inference_path = self.run / 'inference_config.json'
        if inference_path.exists():
            inference = json.loads(inference_path.read_text())
            if inference['adapter_sha256'] != self.report['adapter_artifacts'][name]['adapter_model.safetensors']:
                raise ValueError('inference_configuration_adapter_mismatch')
            self.set_adapter_scale(inference['adapter_scale'])

    def set_adapter_scale(self, scale):
        if not isinstance(scale, (int, float)) or not .01 <= scale <= 1:
            raise ValueError('invalid_adapter_scale')
        from peft.tuners.lora.layer import LoraLayer
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                module.set_scale('default', scale)
        self.adapter_scale = scale

    def status(self):
        return {'status': 'ready', 'mode': 'learned_model_generation', 'model': self.report['base_model'],
                'device': self.device, 'optimizer_updates': self.report.get('cumulative_optimizer_updates', self.report['optimizer_updates']),
                'trained_tokens': self.report.get('cumulative_trained_tokens', self.report['trained_tokens']), 'selected_adapter': self.report['selected_adapter'],
                'adapter_scale': self.adapter_scale,
                'includes_attachment_training': (self.report.get('inherits_attachment_training', False) or
                    (self.report.get('source_token_counts', {}).get('train_attachment_tokens', 0) > 0
                    and self.report.get('configuration', {}).get('domain_steps') == 0)),
                'retrieval_used': False, 'measured_loss_improved': self.report['measured_loss_improved']}

    def answer(self, question, history=None, max_new_tokens=320, baseline=False):
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            raise ValueError('invalid_question')
        history = history or []
        if not isinstance(history, list) or len(history) > 8:
            raise ValueError('invalid_history')
        for index, row in enumerate(history):
            if (not isinstance(row, dict) or row.get('role') != ('user' if index % 2 == 0 else 'assistant')
                    or not isinstance(row.get('content'), str) or len(row['content']) > 3000):
                raise ValueError('invalid_history')
        if len(history) % 2:
            raise ValueError('invalid_history')
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}] + history + [{'role': 'user', 'content': question.strip()}]
        while True:
            ids = self.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
            if len(ids) <= 1536:
                break
            if len(messages) <= 2:
                raise ValueError('question_token_limit')
            del messages[1:3]
        started = time.monotonic()
        with self.lock, self.torch.inference_mode():
            input_ids = self.torch.tensor([ids], dtype=self.torch.long, device=self.device)
            import contextlib
            context = self.model.disable_adapter() if baseline else contextlib.nullcontext()
            with context:
                output = self.model.generate(input_ids=input_ids, attention_mask=self.torch.ones_like(input_ids),
                                             max_new_tokens=max_new_tokens, do_sample=False,
                                             temperature=1.0, top_p=1.0, top_k=50,
                                             repetition_penalty=1.08, use_cache=True,
                                             pad_token_id=self.tokenizer.eos_token_id)
            generated = output[0, len(ids):]
            text = self.tokenizer.decode(generated, skip_special_tokens=True)
            if not text.strip():
                raise RuntimeError('empty_model_answer')
        return {'answer': text, 'mode': 'learned_model_generation', 'retrieval_used': False,
                'generated_tokens': len(generated), 'elapsed_seconds': round(time.monotonic() - started, 2)}


def main():
    parser = argparse.ArgumentParser(description='使用经过本地训练的压合工程模型生成回答')
    parser.add_argument('--run', type=Path, default=None)
    commands = parser.add_subparsers(dest='command', required=True)
    question = commands.add_parser('ask')
    question.add_argument('question')
    commands.add_parser('check')
    args = parser.parse_args()
    try:
        if args.run is None:
            pointer_path = ROOT / 'data/current_model.json'
            if pointer_path.exists():
                pointer = json.loads(pointer_path.read_text())
                args.run = (ROOT / 'data' / pointer['run_name']).resolve()
                if args.run.parent != (ROOT / 'data').resolve() or digest_file(args.run / 'report.json') != pointer['report_sha256']:
                    raise ValueError('current_model_pointer_mismatch')
                if ('inference_config_sha256' in pointer
                        and digest_file(args.run / 'inference_config.json') != pointer['inference_config_sha256']):
                    raise ValueError('current_inference_configuration_mismatch')
            else:
                args.run = ROOT / 'data/model_training_v1'
        engine = LearnedEngineer(args.run)
        if args.command == 'ask':
            print(engine.answer(args.question)['answer'])
        elif args.command == 'check':
            # Persist text locally; only aggregate checks appear in the tool conversation.
            questions = ['请复述这些试验条件，不增加新数值：温度180°C，压力25 bar，保温60 min。',
                         '没有材料牌号和叠构信息，可以直接确定通用的量产压合温度吗？',
                         '试验尚未完成，是否可以说验证已经通过？',
                         '不同材料邮件中的温度、压力能否直接拼成一套配方？',
                         '压合后出现白斑，应该补充哪些信息再分析？']
            results = []
            for number, text in enumerate(questions, 1):
                before = engine.answer(text, max_new_tokens=160, baseline=True)
                after = engine.answer(text, max_new_tokens=160)
                results.append({'question': text, 'baseline': before, 'adapted': after})
                emit('generation_compared', question_number=number, baseline_tokens=before['generated_tokens'],
                     adapted_tokens=after['generated_tokens'], answer_changed=before['answer'] != after['answer'])
            dump_json(args.run / 'generation_comparison.json', {'results': results, 'kind': 'synthetic_prompts_not_expert_accuracy_test'})
            emit('learned_model_inference_checked', **engine.status())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        emit('learned_model_failed', error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
