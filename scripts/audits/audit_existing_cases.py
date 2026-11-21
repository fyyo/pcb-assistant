"""Inventory existing real-source case passages; do not create new evidence.

All quotes remain in private local files. Counts are passages, not independently
verified incidents. Historical train/validation assignments are retained.
"""
import collections
import json
import os
import re

from pcb_assistant.training.storage import ROOT, read
from pcb_assistant.training.data import initialize
from pcb_assistant.training.common import digest_file, dump_json, emit
from pcb_assistant.knowledge import extract_fields

OUTCOME = re.compile(
    r'(?:测试|測試|试验|試驗|验证|驗證|切片|复测|復測|检查|檢查).{0,24}'
    r'(?:结果|結果|显示|顯示|发现|發現|合格|不合格|失效|未通过|未通過)'
    r'|(?:不良率|良率|缺陷率).{0,30}(?:降至|上升|降低|下降|%|％)'
    r'|(?:改善|调整|調整|更换|更換).{0,20}后.{0,20}(?:未再|消失|减少|減少|仍|无改善|無改善)', re.I)
PROPOSED = re.compile(
    r'建议|建議|计划|計劃|预计|預計|拟|擬|如果|假如|待验证|待驗證|尚未|未完成'
    r'|需(?:要)?(?:进行|進行|测试|測試|试验|試驗|验证|驗證)')


def classify(text):
    fields = extract_fields(text, limit=50)
    is_case = bool(fields['phenomenon'] and (fields['analysis'] or fields['actions']))
    # A failed test is an outcome too. Proposals are not promoted to results.
    clues = [item for item in fields['verification'] if OUTCOME.search(item['text'])
             and not PROPOSED.search(item['text'])]
    return fields, is_case, clues


def audit(root=ROOT):
    foundation = initialize(root)
    source = root / 'data' / foundation['prepared']
    output = root / 'data/continuous/existing_cases'
    output.mkdir(exist_ok=True, mode=0o700)
    signature = {'foundation': foundation['prepared_report_sha256'], 'rule': digest_file(__file__)}
    previous = read(output / 'report.json', {})
    if previous.get('signature') == signature and all(
            digest_file(output / name) == checksum for name, checksum in previous['artifacts'].items()):
        return previous
    summaries, artifacts = {}, {}
    for split in ('train', 'validation'):
        counts, groups = collections.Counter(), set()
        destination = output / (split + '_case_inventory.jsonl')
        temporary = destination.with_suffix('.partial')
        with (source / (split + '_domain.jsonl')).open(encoding='utf-8') as inputs, temporary.open('w', encoding='utf-8') as stream:
            for line in inputs:
                row = json.loads(line)
                fields, is_case, clues = classify(row['text'])
                counts['domain_passages'] += 1
                counts['case_like_passages'] += is_case
                counts['passages_with_outcome_language'] += bool(clues)
                counts['case_passages_with_outcome_language'] += bool(is_case and clues)
                if not is_case:
                    continue
                if clues:
                    groups.add(row['group_id'])
                record = {'source_id': row['source_id'], 'group_id': row['group_id'], 'split': split,
                          'source_kind': row.get('source_kind'), 'source_fingerprint': row.get('fingerprint'),
                          'fields': fields, 'reported_outcome_clues': clues,
                          'evidence_status': 'archived_source_not_independently_verified',
                          'new_evidence': False}
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        temporary.chmod(0o600)
        temporary.replace(destination)
        counts['discussion_groups_with_outcome_case_passages'] = len(groups)
        summaries[split] = dict(counts)
        artifacts[destination.name] = digest_file(destination)
    result = {'status': 'completed', 'signature': signature, 'counts': summaries, 'artifacts': artifacts,
              'limits': ['统计的是现有片段和讨论组，不是已确认的独立案例数量。',
                         '结果措辞由规则识别，可能误判或漏判；同片段共现不证明措施与结果的因果关系。',
                         '只核对已经准备的训练及留出资料，未重新解析原始邮件或附件。',
                         '保留原训练/留出划分；已训练内容不能作为模型未见过的独立测试。',
                         '此清单不是新增证据，不加入新增学习队列，不触发重复训练。']}
    dump_json(output / 'report.json', result)
    return result


if __name__ == '__main__':
    os.umask(0o077)
    try:
        result = audit()
        emit('existing_case_inventory_completed', counts=result['counts'])
    except Exception as exc:
        emit('existing_case_inventory_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
