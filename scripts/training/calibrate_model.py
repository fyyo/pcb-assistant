"""Calibrate learned adapter strength against synthetic instruction checks.

All generated answers stay local. This is not an engineering accuracy benchmark.
"""
import argparse
from pathlib import Path
import random
import re

from pcb_assistant.training.common import ROOT, dump_json, emit
from pcb_assistant.training.inference import LearnedEngineer
from pcb_assistant.training.train import jsonl, instruction_tokens

CALIBRATION = [
    {'question': '请复述这些试验条件，不增加新数值：温度180°C，压力25 bar，保温60 min。', 'numbers': [180, 25, 60]},
    {'question': '没有材料牌号和叠构信息，可以直接确定通用的量产压合温度吗？', 'kind': 'uncertainty'},
    {'question': '试验尚未完成，是否可以说验证已经通过？', 'kind': 'uncertainty'},
    {'question': '不同材料邮件中的温度、压力能否直接拼成一套配方？', 'kind': 'uncertainty'},
    {'question': '压合后出现白斑，应该补充哪些信息再分析？', 'kind': 'context'},
]
ADDITIONAL = [
    {'question': '原样列出条件：温度165°C，压力22 bar，保温55 min。不要添加新的数值。', 'numbers': [165, 22, 55]},
    {'question': '只知道一个降温建议，还没有做试验，能认定这个建议已经验证有效吗？', 'kind': 'uncertainty'},
    {'question': '两种不同材料的生产记录，可以直接合成一种通用量产工艺吗？', 'kind': 'uncertainty'},
    {'question': '原样复述，不要补充：真空为-0.085 MPa，保温时间为70 min。', 'numbers': [-.085, 70]},
    {'question': '出现板翘时，判断原因前应先了解哪些材料与工艺条件？', 'kind': 'context'},
]


def check(spec, answer):
    if 'numbers' in spec:
        numbers = {float(value) for value in re.findall(r'[-+]?\d+(?:\.\d+)?', answer)}
        expected = set(spec['numbers'])
        return expected <= numbers and not {value for value in numbers if abs(value) > 10 and value not in expected}
    if spec['kind'] == 'uncertainty':
        return bool(re.search(r'不能|不应|不建[议議]|不可以|不宜|不够|无法|無法|尚未|未完成|未验证|需要|具体|確認|确认', answer))
    return bool(re.search(r'材料|叠构|疊構|温度|溫度|压力|壓力|真空|切片|位置|分布|条件|條件', answer))


def main():
    parser = argparse.ArgumentParser(description='校准本地学习权重强度并检查指令能力')
    parser.add_argument('--run', type=Path, default=ROOT / 'data/model_training_v1')
    parser.add_argument('--data', type=Path, default=ROOT / 'data/training_data_v1')
    args = parser.parse_args()
    run = args.run
    engine = LearnedEngineer(run)
    generations = []
    summaries = []
    for scale in (1.0, .5, .25, .125, .0625, .03125, .015625):
        engine.set_adapter_scale(scale)
        checks = []
        for spec in CALIBRATION:
            result = engine.answer(spec['question'], max_new_tokens=224)
            passed = check(spec, result['answer'])
            checks.append(passed)
            generations.append({'scale': scale, 'suite': 'calibration', 'question': spec['question'],
                                'answer': result['answer'], 'passed': passed})
        summary = {'scale': scale, 'passed': sum(checks), 'total': len(checks), 'checks': checks}
        summaries.append(summary)
        emit('adapter_strength_checked', **summary)
        # Evaluate from strongest to weakest and stop at the first passing configuration.
        if all(checks):
            selected = scale
            break
    else:
        dump_json(run / 'calibration_generations.json', generations)
        dump_json(run / 'calibration_report.json', {'status': 'no_passing_scale', 'checks': summaries})
        raise ValueError('no_passing_adapter_scale')
    additional = []
    for spec in ADDITIONAL:
        result = engine.answer(spec['question'], max_new_tokens=224)
        passed = check(spec, result['answer'])
        additional.append(passed)
        generations.append({'scale': selected, 'suite': 'additional', 'question': spec['question'],
                            'answer': result['answer'], 'passed': passed})
    emit('additional_generation_checks', scale=selected, passed=sum(additional), total=len(additional))

    # Measure the actual served strength on the same held-out batches as training.
    data = args.data
    domain_rows = jsonl(data / 'validation_domain.jsonl')
    random.Random(20261006).shuffle(domain_rows)
    stream = []
    for row in domain_rows:
        stream.extend(engine.tokenizer.encode(row['text'], add_special_tokens=False))
        stream.append(engine.tokenizer.eos_token_id)
    domain = [{'input_ids': stream[start:start + 512], 'labels': stream[start:start + 512]}
              for start in range(0, len(stream) - 16, 512)]
    instructions = [instruction_tokens(engine.tokenizer, row, 512)
                    for row in jsonl(data / 'validation_instruction.jsonl')]
    measured = {}
    torch = engine.torch
    engine.model.config.use_cache = False
    with torch.inference_mode():
        for kind, rows in (('domain', domain), ('instruction', instructions)):
            indices = random.Random(20261007).sample(range(len(rows)), min(64, len(rows)))
            total, count = 0.0, 0
            for index in indices:
                row = rows[index]
                inputs = torch.tensor([row['input_ids']], device=engine.device)
                labels = torch.tensor([row['labels']], device=engine.device)
                targets = sum(value != -100 for value in row['labels'][1:])
                with torch.autocast(engine.device, dtype=torch.float16):
                    loss = engine.model(input_ids=inputs, labels=labels, attention_mask=torch.ones_like(inputs)).loss
                total += float(loss) * targets
                count += targets
            measured[kind + '_loss'] = total / count
            measured[kind + '_target_tokens'] = count
    baseline = engine.report['evaluations']['baseline']
    improved = all(measured[key] < baseline[key] for key in ('domain_loss', 'instruction_loss'))
    report = {'status': 'completed' if improved and all(additional) else 'not_accepted', 'selected_scale': selected, 'calibration': summaries,
              'additional_checks': additional, 'served_model_evaluation': measured,
              'both_losses_below_base': improved,
              'limits': '这些是合成指令检查与词元损失，不是工程准确率认证；选强度使用了校准问题。'}
    dump_json(run / 'calibration_generations.json', generations)
    dump_json(run / 'calibration_report.json', report)
    if not improved:
        raise ValueError('calibrated_losses_not_improved')
    if not all(additional):
        raise ValueError('additional_generation_check_failed')
    dump_json(run / 'inference_config.json', {'adapter_scale': selected,
              'adapter_sha256': engine.report['adapter_artifacts'][engine.report['selected_adapter']]['adapter_model.safetensors'],
              'reason': 'preserve_instruction_following_while_retaining_measured_domain_adaptation'})
    emit('adapter_calibration_completed', selected_scale=selected, both_losses_below_base=improved, **measured)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('adapter_calibration_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
