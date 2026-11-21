"""Source-grounded lessons from existing cases, retaining historical holdout."""
import collections
import json
import os
import re
import shutil

from scripts.audits.audit_existing_cases import audit
from continuous_common import ROOT, fingerprint
from continuous_data import initialize, rows, verify_data
from learning_common import digest_file, dump_json, emit

LESSONS = {
    'analysis': ('请整理原记录中的原因线索，保留推测语气。', '记录中的分析线索：', '这不是对唯一原因的独立确认。'),
    'actions': ('原记录提出了哪些处理措施？区分建议和已完成的处理。', '记录中的措施描述：', '是否已实施以原文为准，建议本身不代表已经验证有效。'),
    'verification': ('整理原记录的试验和结果描述，保留未完成、失败等信息。', '记录中的验证描述：', '完成状态和结果以原文为准，不补写未知结果。'),
    'limitations': ('原记录有哪些适用边界或尚未确认的问题？', '记录中的边界说明：', '不能将这条记录直接推广为所有材料通用的工艺。'),
    'conditions': ('复述记录中的材料或工艺条件，保留原来的数字、符号与单位。', '记录中的条件：', '这些数值只属于该记录，不能补成通用量产配方。'),
}
QUANTITY = re.compile(r'[-+]?\d+(?:\.\d+)?(?:\s*(?:°C|℃|bar|MPa|kPa|Pa|psi|min|mm|um|μm|µm|%|％|s))?', re.I)


def fragments(text, limit=140):
    """Contiguous quoted spans; never cut through a recognized quantity/unit."""
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            boundaries = [m.end() for m in re.finditer(r'[，,。；;\n]', text[start:end]) if m.end() >= limit // 2]
            if boundaries:
                end = start + boundaries[-1]
            for quantity in QUANTITY.finditer(text):
                if quantity.start() < end < quantity.end():
                    end = quantity.start() if quantity.start() > start else quantity.end()
                    break
        part = text[start:end]
        if part.strip():
            yield {'text': part, 'start': start, 'end': end}
        start = end


def lessons(case):
    context = next(fragments(case['fields']['phenomenon'][0]['text']))['text']
    for kind, (question, label, boundary) in LESSONS.items():
        for quote in case['fields'][kind]:
            for part in fragments(quote['text']):
                prompt = '下面是同一条历史压合记录的部分已知信息：\n' + context + '\n' + question
                if kind == 'conditions':
                    # Copying numeric evidence is an explicit task, not a request
                    # to infer a process recipe from an incomplete defect report.
                    prompt = '原记录条件片段：\n' + part['text'] + '\n' + question
                response = label + part['text'] + '\n' + boundary
                yield {'prompt': prompt, 'response': response, 'kind': 'instruction',
                       'lesson_kind': kind, 'source_id': case['source_id'], 'group_id': case['group_id'],
                       'source_kind': case['source_kind'], 'preserve_complete_example': True,
                       'target_quote': part['text'], 'target_start': quote['start'] + part['start'],
                       'target_end': quote['start'] + part['end'], 'source_fingerprint': case['source_fingerprint'],
                       'evidence_status': 'archived_record_not_independent_causal_confirmation'}


def prepare(root=ROOT):
    foundation = initialize(root)
    inventory = audit(root)
    source = root / 'data' / foundation['prepared']
    inventory_dir = root / 'data/continuous/existing_cases'
    signature = fingerprint({'foundation': foundation['prepared_report_sha256'],
                             'inventory': digest_file(inventory_dir / 'report.json'), 'builder': digest_file(__file__)})
    output = root / 'data' / ('training_data_cases_' + signature[:12])
    if (output / 'report.json').exists():
        return output, verify_data(output)
    temporary = output.with_name(output.name + '_building')
    temporary.mkdir(exist_ok=True, mode=0o700)
    artifacts = {}
    for name in ('train_domain.jsonl', 'validation_domain.jsonl', 'validation_instruction.jsonl'):
        shutil.copyfile(source / name, temporary / name)
        (temporary / name).chmod(0o600)
    train_seen, counts, coverage = set(), {}, {}
    for split in ('train', 'validation'):
        counter = collections.Counter()
        covered = set()
        name = 'train_instruction.jsonl' if split == 'train' else 'case_validation_instruction.jsonl'
        with (temporary / name).open('w', encoding='utf-8') as stream:
            if split == 'train':
                for row in rows(source / 'train_instruction.jsonl'):
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                    train_seen.add(fingerprint([row['prompt'], row['response']]))
                    counter['original_instruction_replay'] += 1
            seen = set()
            for case in rows(inventory_dir / (split + '_case_inventory.jsonl')):
                for row in lessons(case):
                    ident = fingerprint([row['prompt'], row['response']])
                    if ident in seen or ident in train_seen:
                        counter['duplicate_tasks_excluded'] += 1
                        continue
                    seen.add(ident)
                    row['fingerprint'] = ident
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                    counter[row['lesson_kind']] += 1
                    covered.add(case['source_fingerprint'])
            if split == 'train':
                train_seen.update(seen)
        counts[split] = dict(counter)
        coverage[split] = len(covered)
    for path in temporary.glob('*.jsonl'):
        path.chmod(0o600)
        artifacts[path.name] = {'sha256': digest_file(path)}
    result = {'status': 'completed', 'source_artifact_sha256': signature, 'artifacts': artifacts,
              'case_study': {'foundation': foundation['prepared'], 'inventory_counts': inventory['counts'],
                             'lesson_counts': counts, 'case_passages_with_additional_tasks': coverage,
                             'maximum_sequence_length': 768, 'new_external_evidence': False},
              'limits': ['本轮继续已部署模型的问答训练；完整复习原训练问答并学习存量案例任务。',
                         '附加答案为原文片段和明确的证据边界，不使用模型自答生成事实。',
                         '保留历史训练/留出划分，派生案例评测不宣称为新的盲测案例。',
                         '每个附加任务完整编码；不静默截断目标中的数字、符号或单位。']}
    dump_json(temporary / 'report.json', result)
    temporary.rename(output)
    return output, result


if __name__ == '__main__':
    os.umask(0o077)
    try:
        output, report = prepare()
        emit('existing_case_lessons_prepared', data_run=output.name, **report['case_study'])
    except Exception as exc:
        emit('existing_case_preparation_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
