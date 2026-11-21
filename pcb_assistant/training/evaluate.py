"""Compare served candidate and incumbent weights on identical local holdouts."""
import argparse
import gc
import math
import random
import re

from pcb_assistant.training.storage import ROOT, fingerprint, read
from pcb_assistant.training.data import rows, verify_data
from pcb_assistant.training.common import digest_file, dump_json, emit


def behavior_check(spec, answer):
    if 'quantities' in spec:
        expected = {float(value) for value, _ in spec['quantities']}
        found = {float(x) for x in re.findall(r'[-+]?\d+(?:\.\d+)?', answer)}
        if found != expected:
            return False
        for value, unit in spec['quantities']:
            unit_pattern = '(?:°\\s*C|℃)' if unit == '°C' else re.escape(unit)
            if not re.search(r'(?<![\d.])' + re.escape(value) + r'\s*' + unit_pattern, answer, re.I):
                return False
        return True
    for pattern in spec.get('assertion_forbidden', []):
        for clause in re.split(r'[。！？\n，,；;]', answer):
            for match in re.finditer(pattern, clause, re.I):
                prefix = clause[:match.start()]
                # A warning such as “不能宣称已经验证成功” is not an assertion
                # of success. Keep this conservative rule covered synthetically.
                negated = re.search(r'不能|不可|不应|不宜|无法|尚未|未能|没有|缺乏|能否|是否|不代表', prefix)
                double_negative = re.search(r'不能否认|不能排除|不是不能|并非不能', prefix)
                if not negated or double_negative:
                    return False
    return (all(re.search(pattern, answer, re.I) for pattern in spec.get('required', []))
            and not any(re.search(pattern, answer, re.I) for pattern in spec.get('forbidden', [])))


def decide(incumbent, candidate, policy):
    reasons = []
    old, new = incumbent['losses'], candidate['losses']
    if set(old) != set(new) or not old:
        return {'accepted': False, 'reasons': ['evaluation_schema_mismatch']}
    ratios = []
    for key in old:
        if not all(math.isfinite(v) and v > 0 for v in (old[key], new[key])):
            reasons.append('nonfinite_loss')
            continue
        ratio = new[key] / old[key]
        ratios.append(ratio)
        if ratio > 1 + policy['maximum_loss_regression']:
            reasons.append('loss_regression:' + key)
    if not ratios or sum(ratios) / len(ratios) > 1 - policy['minimum_relative_improvement']:
        reasons.append('no_measurable_improvement')
    # New evidence must improve where independent new-case evaluation exists.
    fresh = [new[k] / old[k] for k in old if k.startswith('new_') and old[k] > 0]
    if fresh and sum(fresh) / len(fresh) >= 1:
        reasons.append('new_evidence_not_improved')
    case_scores = [new[k] / old[k] for k in old if k.startswith('case_') and old[k] > 0]
    if case_scores and sum(case_scores) / len(case_scores) >= 1:
        reasons.append('historical_case_tasks_not_improved')
    before, after = incumbent['checks'], candidate['checks']
    if set(before) != set(after) or not after:
        reasons.append('behavior_schema_mismatch')
    else:
        if any(before[k]['passed'] and not after[k]['passed'] for k in before):
            reasons.append('behavior_regression')
        if any(row['critical'] and not row['passed'] for row in after.values()):
            reasons.append('number_or_unit_failure')
        if sum(row['passed'] for row in after.values()) / len(after) < policy['minimum_behavior_pass_fraction']:
            reasons.append('behavior_floor_not_met')
    return {'accepted': not reasons, 'reasons': reasons, 'mean_relative_loss': sum(ratios) / len(ratios) if ratios else None,
            'scope': 'heldout_loss_and_synthetic_behavior_only', 'chief_engineer_certified': False}


def evaluate(run, foundation, data, output, root=ROOT):
    from pcb_assistant.training.inference import LearnedEngineer
    from pcb_assistant.training.train import instruction_tokens
    verify_data(foundation)
    verify_data(data)
    benchmark = read(root / 'engineering_benchmark.json')
    signature = fingerprint({'report': digest_file(run / 'report.json'),
                             'inference': digest_file(run / 'inference_config.json'),
                             'foundation': digest_file(foundation / 'report.json'),
                             'data': digest_file(data / 'report.json'), 'benchmark': benchmark, 'evaluator': digest_file(__file__)})
    cached = read(output)
    if cached and cached.get('signature') == signature:
        return cached
    engine = LearnedEngineer(run)
    losses, checks, generations = {}, {}, []
    torch = engine.torch
    engine.model.config.use_cache = False
    for label, path, prefix in [('frozen', foundation, 'validation'), ('new', data, 'new_validation'),
                                ('case', data, 'case_validation')]:
        for kind in ('domain', 'instruction'):
            source = path / (prefix + '_' + kind + '.jsonl')
            if not source.exists():
                continue
            records = rows(source)
            if not records:
                continue
            random.Random(20261006).shuffle(records)
            if kind == 'domain':
                stream = []
                for row in records:
                    stream.extend(engine.tokenizer.encode(row['text'], add_special_tokens=False))
                    stream.append(engine.tokenizer.eos_token_id)
                batches = [{'input_ids': stream[i:i+512], 'labels': stream[i:i+512]} for i in range(0, len(stream) - 16, 512)]
            else:
                length = read(data / 'report.json').get('case_study', {}).get('maximum_sequence_length', 512) if label == 'case' else 512
                batches = [instruction_tokens(engine.tokenizer, row, length) for row in records]
            selection = (list(range(len(batches))) if label == 'case' else
                         random.Random(20261007).sample(range(len(batches)), min(64, len(batches))))
            total, tokens = 0., 0
            with torch.inference_mode():
                for index in selection:
                    row = batches[index]
                    ids = torch.tensor([row['input_ids']], device=engine.device)
                    labels = torch.tensor([row['labels']], device=engine.device)
                    count = sum(v != -100 for v in row['labels'][1:])
                    loss = engine.model(input_ids=ids, labels=labels, attention_mask=torch.ones_like(ids)).loss
                    if not torch.isfinite(loss):
                        raise ValueError('nonfinite_evaluation')
                    total += float(loss) * count
                    tokens += count
            if not tokens:
                raise ValueError('empty_evaluation')
            losses[label + '_' + kind] = total / tokens
    for spec in benchmark['cases']:
        answer = engine.answer(spec['question'], max_new_tokens=224)['answer']
        checks[spec['id']] = {'passed': behavior_check(spec, answer), 'critical': spec.get('critical', False),
                               'capability': spec['capability']}
        generations.append({'id': spec['id'], 'answer': answer})
    result = {'signature': signature, 'run_name': run.name, 'losses': losses, 'checks': checks,
              'benchmark_version': benchmark['version'], 'chief_engineer_certified': False}
    dump_json(output.with_name(output.stem + '_private_answers.json'), generations)
    dump_json(output, result)
    del engine
    gc.collect()
    torch.cuda.empty_cache()
    return result


def compare(incumbent, candidate, foundation, data, output, root=ROOT):
    before = evaluate(incumbent, foundation, data, output / 'incumbent_evaluation.json', root)
    after = before if candidate == incumbent else evaluate(candidate, foundation, data, output / 'candidate_evaluation.json', root)
    decision = decide(before, after, read(root / 'continuous_policy.json'))
    decision.update({'incumbent_run': incumbent.name, 'candidate_run': candidate.name,
                     'incumbent_signature': before['signature'], 'candidate_signature': after['signature']})
    dump_json(output / 'promotion_evaluation.json', decision)
    return decision


if __name__ == '__main__':
    from pathlib import Path
    parser = argparse.ArgumentParser()
    for name in ('incumbent', 'candidate', 'foundation', 'data', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compare(args.incumbent, args.candidate, args.foundation, args.data, args.output)
        emit('candidate_compared', **result)
    except Exception as exc:
        emit('candidate_comparison_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
