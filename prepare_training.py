"""Construct private domain-adaptation and instruction data from sanitized mail.

No model API, mail upload, guessed answers, or raw PST access. Holdout splitting
groups repeated evidence and informative subjects before any samples are emitted.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import sys

from pcb_knowledge import ROOT, PLACEHOLDERS, digest_file, dump_json, extract_fields, passages
from mail_privacy import technical_topics
from learning_common import SYSTEM_PROMPT


def canonical(text):
    return re.sub(r'[^a-z0-9\u3400-\u9fff]', '', PLACEHOLDERS.sub('', text).casefold())


def eligible(text):
    clean = PLACEHOLDERS.sub('', text)
    if len(canonical(clean)) < 45 or len(clean) < len(text) * .65:
        return False
    if not technical_topics(clean):
        return False
    if re.search(r'忽略.{0,15}(?:指令|规则)|ignore.{0,20}instructions|<script', clean, re.I):
        return False
    return True


class Groups:
    def __init__(self, ids):
        self.parents = {value: value for value in ids}

    def find(self, value):
        while value != self.parents[value]:
            self.parents[value] = self.parents[self.parents[value]]
            value = self.parents[value]
        return value

    def join(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parents[max(left, right)] = min(left, right)


def instruction(piece):
    fields = extract_fields(piece, limit=2)
    if not fields['phenomenon'] or not (fields['analysis'] or fields['actions']):
        return None
    context = []
    for key, label in [('phenomenon', '现象'), ('materials', '材料'), ('conditions', '条件')]:
        if fields[key]:
            context.append(label + '：' + fields[key][0]['text'])
    answers = []
    for key, label in [('analysis', '记录中的分析线索'), ('actions', '记录中的措施建议'),
                       ('verification', '验证记录提及'), ('limitations', '适用边界')]:
        if fields[key]:
            answers.append(label + '：' + fields[key][0]['text'])
    if not fields['verification']:
        answers.append('验证情况：这段记录没有明确给出验证结果。')
    if not fields['limitations']:
        answers.append('适用边界：还需结合实际材料、叠构与工艺条件确认，不能直接作为通用配方。')
    return {'prompt': '以下是同一段压合工程记录中的已知信息：\n' + '\n'.join(context)
                      + '\n请整理相应的分析线索、处理建议和验证边界，不补写未知数值。',
            'response': '\n'.join(answers)}


def prepare(source_run, output, holdout_fraction=.1):
    policy = json.loads((ROOT / 'training_policy.json').read_text())
    if policy.get('local_model_training_allowed') is not True or policy.get('mail_upload_allowed') is not False:
        raise ValueError('training_not_authorized')
    source_run, output = Path(source_run), Path(output)
    report = json.loads((source_run / 'report.json').read_text())
    source = source_run / 'engineering_candidates.jsonl'
    source_hash = digest_file(source)
    if report['status'] not in ('completed_local_candidates_require_review', 'completed_local_training_sources') or source_hash != report['artifacts'][source.name]['sha256']:
        raise ValueError('source_not_complete_or_changed')
    with source.open(encoding='utf-8') as stream:
        records = [json.loads(line) for line in stream]
    if len({row['id'] for row in records}) != len(records):
        raise ValueError('duplicate_source_id')
    ids = {row['id'] for row in records}
    parent_ids = {parent for row in records for parent in row.get('parent_source_ids', [])}
    groups = Groups(ids | parent_ids)
    document_owners = {}
    for row in records:
        for parent in row.get('parent_source_ids', []):
            groups.join(row['id'], parent)
        document_id = row.get('document_id')
        if document_id:
            if document_id in document_owners:
                groups.join(row['id'], document_owners[document_id])
            document_owners[document_id] = row['id']
    subjects, duplicate_documents, evidence_owners = {}, {}, {}
    prepared = []
    rejected = collections.Counter()
    for row in records:
        ident = row['id']
        # Remove common reply/forward prefixes before grouping subjects.
        subject = canonical(re.sub(r'(?i)^(?:(?:re|fw|fwd|回复|回覆|转发|轉寄)\s*[:：]\s*)+', '', row['subject']))
        if len(subject) >= 8 and row.get('source_kind') != 'untrusted_attachment':
            if subject in subjects:
                groups.join(ident, subjects[subject])
            subjects[subject] = ident
        full_hash = hashlib.sha256(canonical(row['text']).encode()).hexdigest()
        if full_hash in duplicate_documents:
            groups.join(ident, duplicate_documents[full_hash])
        duplicate_documents[full_hash] = ident
        for piece in passages(row['text'], max_chars=700):
            if not eligible(piece['text']):
                rejected['filtered_passages'] += 1
                continue
            key = hashlib.sha256(canonical(piece['text']).encode()).hexdigest()
            if key in evidence_owners:
                groups.join(ident, evidence_owners[key])
                rejected['duplicate_passages'] += 1
                continue
            evidence_owners[key] = ident
            prepared.append({'source_id': ident, 'fingerprint': key, 'text': piece['text'],
                             'source_kind': row.get('source_kind', 'untrusted_email'),
                             'extraction_method': row.get('extraction_method', 'email_text'),
                             'start': piece['start'], 'end': piece['end']})
    # Group long exact technical sentences as well: forwarded messages can shift chunk boundaries.
    sentence_owners = {}
    for item in prepared:
        for sentence in re.split(r'[\n。；]', item['text']):
            sentence = canonical(sentence)
            if len(sentence) < 90 or not technical_topics(sentence):
                continue
            key = hashlib.sha256(sentence.encode()).hexdigest()
            if key in sentence_owners:
                groups.join(item['source_id'], sentence_owners[key])
            sentence_owners[key] = item['source_id']
    grouped = collections.defaultdict(list)
    for item in prepared:
        grouped[groups.find(item['source_id'])].append(item)
    if len(grouped) < 3:
        raise ValueError('insufficient_independent_groups')
    ordered = sorted(grouped, key=lambda key: hashlib.sha256(('pcb-split-v1:' + key).encode()).hexdigest())
    target = max(1, round(len(prepared) * holdout_fraction))
    held_groups, held_count = set(), 0
    for key in ordered:
        if held_count >= target:
            break
        # Keep oversized discussion clusters in training instead of consuming the holdout.
        if len(grouped[key]) > max(target, len(prepared) * .25):
            continue
        held_groups.add(key)
        held_count += len(grouped[key])
    if not held_groups or len(held_groups) == len(grouped):
        raise ValueError('cannot_form_holdout')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    counts = collections.Counter()
    artifact_paths = {}
    try:
        for split in ('train', 'validation'):
            for kind in ('domain', 'instruction'):
                name = f'{split}_{kind}.jsonl'
                artifact_paths[name] = (output / (name + '.partial')).open('w', encoding='utf-8')
        for group, items in grouped.items():
            split = 'validation' if group in held_groups else 'train'
            for item in items:
                domain = item | {'group_id': group, 'kind': 'domain'}
                artifact_paths[f'{split}_domain.jsonl'].write(json.dumps(domain, ensure_ascii=False) + '\n')
                counts[f'{split}_domain'] += 1
                counts[f'{split}_characters'] += len(item['text'])
                sample = instruction(item['text'])
                if sample:
                    sample.update({'source_id': item['source_id'], 'group_id': group,
                                   'fingerprint': item['fingerprint'], 'kind': 'instruction',
                                   'system': SYSTEM_PROMPT})
                    artifact_paths[f'{split}_instruction.jsonl'].write(json.dumps(sample, ensure_ascii=False) + '\n')
                    counts[f'{split}_instruction'] += 1
    finally:
        for stream in artifact_paths.values():
            stream.close()
    artifacts = {}
    for name in artifact_paths:
        (output / (name + '.partial')).replace(output / name)
        (output / name).chmod(0o600)
        artifacts[name] = {'sha256': digest_file(output / name), 'size_bytes': (output / name).stat().st_size}
    counts['source_records'] = len(records)
    counts['source_messages'] = sum(row.get('source_kind') != 'untrusted_attachment' for row in records)
    counts['source_attachment_units'] = len(records) - counts['source_messages']
    counts['included_messages'] = len({item['source_id'] for item in prepared if item['source_kind'] != 'untrusted_attachment'})
    counts['included_attachment_units'] = len({item['source_id'] for item in prepared if item['source_kind'] == 'untrusted_attachment'})
    counts['groups'] = len(grouped)
    counts['validation_groups'] = len(held_groups)
    result = {'status': 'completed', 'counts': dict(counts), 'filtered': dict(rejected),
              'largest_group_passages': max(map(len, grouped.values())),
              'source_artifact_sha256': source_hash, 'artifacts': artifacts, 'policy': policy,
              'split_method': 'grouped_parent_email_attachment_document_subject_exact_passage_and_long_technical_sentence',
              'limits': ['规则生成的任务样本不是人工标注答案；邮件经验未经独立验证。',
                         '同主题与精确重复分组降低泄漏，不能证明所有近义改写均已隔离。']}
    dump_json(output / 'report.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description='生成本地模型训练数据')
    parser.add_argument('--source-run', type=Path, default=ROOT / 'data/sanitized_v3')
    parser.add_argument('--run-name', default='training_data_v1')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.run_name):
        parser.error('invalid run name')
    try:
        report = prepare(args.source_run, ROOT / 'data' / args.run_name)
        print(json.dumps({'event': 'training_data_prepared', **report['counts']}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'event': 'preparation_failed', 'error_type': type(exc).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
