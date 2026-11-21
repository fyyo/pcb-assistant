"""Cumulative snapshots with frozen historical holdout and grouped new evidence."""
import json
from pathlib import Path
import time

from pcb_assistant.training.storage import ROOT, fingerprint, lock, read, setup, verify_pointer
from pcb_assistant.training.common import digest_file, dump_json

FILES = [f'{split}_{kind}.jsonl' for split in ('train', 'validation') for kind in ('domain', 'instruction')]


def rows(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def verify_data(path):
    report = read(path / 'report.json')
    if report['status'] != 'completed':
        raise ValueError('incomplete_data')
    for name, info in report['artifacts'].items():
        if Path(name).name != name or digest_file(path / name) != info['sha256']:
            raise ValueError('data_checksum_mismatch')
    return report


def initialize(root=ROOT):
    setup(root)
    manifest_path = root / 'data/continuous/foundation.json'
    with lock('learning_foundation', root, blocking=True):
        manifest = read(manifest_path)
        if manifest:
            seed = root / 'data' / manifest['prepared']
            if digest_file(seed / 'report.json') != manifest['prepared_report_sha256']:
                raise ValueError('foundation_changed')
            verify_data(seed)
            return manifest
        pointer = read(root / 'data/current_model.json')
        run = verify_pointer(pointer, root)
        config = read(run / 'run_config.json')
        matches = [p.parent for p in (root / 'data').glob('training_data*/report.json')
                   if digest_file(p) == config['data_report_sha256']]
        if len(matches) != 1:
            raise ValueError('foundation_data_not_identified')
        seed = matches[0]
        verify_data(seed)
        manifest = {'created_at': time.time(), 'pointer': pointer, 'prepared': seed.name,
                    'prepared_report_sha256': digest_file(seed / 'report.json')}
        dump_json(manifest_path, manifest)
        return manifest


def overlap_keys(text):
    from pcb_assistant.training.prepare import canonical
    value = canonical(text)
    # A sliding window on new text is checked against stride-30 anchors of old
    # text, detecting long copied fragments despite shifted passage boundaries.
    return value


def snapshot(root=ROOT):
    from pcb_assistant.training.prepare import Groups, canonical, eligible, instruction
    from pcb_assistant.knowledge import passages
    foundation = initialize(root)
    seed = root / 'data' / foundation['prepared']
    records = [read(p) for p in sorted((root / 'data/continuous/records').glob('*.json'))]
    # A case correction supersedes earlier submissions for that same case ID.
    latest = {}
    for record in sorted(records, key=lambda r: (r['created_at'], r['id'])):
        if record['eligible']:
            latest[record['group_id']] = record
    records = [r for r in latest.values() if r['eligible']]
    revision = fingerprint({'foundation': foundation['prepared_report_sha256'],
                            'records': sorted(r['id'] for r in records), 'recipe': 'cumulative-v1'})
    destination = root / 'data' / ('training_data_cont_' + revision[:12])
    if not records:
        return seed, {**verify_data(seed), 'continuous': {'revision': revision, 'training_record_ids': [],
                      'holdout_record_ids': [], 'excluded_holdout_overlap': 0, 'oldest_new_at': None}}
    if (destination / 'report.json').exists():
        report = verify_data(destination)
        return destination, report
    base = {name: rows(seed / name) for name in FILES}
    base_groups = {r['group_id'] for name in FILES for r in base[name]}
    groups = Groups(base_groups | {r['group_id'] for r in records})
    validation_groups = {r['group_id'] for r in base['validation_domain.jsonl']}
    training_groups = {r['group_id'] for r in base['train_domain.jsonl']}
    anchors, seen = {}, {}
    for split in ('train', 'validation'):
        for row in base[split + '_domain.jsonl']:
            value = overlap_keys(row['text'])
            seen[fingerprint(value)] = row['group_id']
            for start in range(0, len(value) - 89, 30):
                key = value[start:start + 90]
                if key in anchors:
                    # Historical corpus has already been grouped. Do not alter
                    # its split; mark bridges by joining groups for intake only.
                    groups.join(row['group_id'], anchors[key])
                anchors[key] = row['group_id']
    prepared = []
    for record in records:
        group = record['group_id']
        for sample in record['samples']:
            value = overlap_keys(sample['text'])
            for start in range(max(0, len(value) - 89)):
                owner = anchors.get(value[start:start + 90])
                if owner:
                    groups.join(group, owner)
            for start in range(0, len(value) - 89, 30):
                anchors[value[start:start + 90]] = group
            pieces = passages(sample['text'], max_chars=700)
            for piece in pieces:
                if not eligible(piece['text']):
                    continue
                key = fingerprint(canonical(piece['text']))
                if key in seen:
                    groups.join(group, seen[key])
                    continue
                seen[key] = group
                prepared.append({'source_id': record['id'], 'group_id': group, 'fingerprint': key,
                                 'text': piece['text'], 'source_kind': sample['source_kind'], 'kind': 'domain',
                                 'created_at': record['created_at']})
        # Preserve supplied corrections/outcomes as targets, never model outputs.
        for sample in record['samples']:
            if sample.get('instruction'):
                key = fingerprint(sample['instruction'])
                if key in seen:
                    groups.join(group, seen[key])
                    continue
                seen[key] = group
                prepared.append({**sample['instruction'], 'source_id': record['id'], 'group_id': group,
                                 'kind': 'instruction', 'created_at': record['created_at'],
                                 'fingerprint': fingerprint(sample['instruction'])})
    held = {groups.find(g) for g in validation_groups}
    trained = {groups.find(g) for g in training_groups}
    new_rows = {name: [] for name in FILES}
    excluded, train_ids, hold_ids = set(), set(), set()
    fraction = read(root / 'config/training/continuous.json')['new_case_holdout_fraction']
    assignments_path = root / 'data/continuous/split_assignments.json'
    assignments = read(assignments_path, {})
    labels = {}
    for record in records:
        group = groups.find(record['group_id'])
        labels.setdefault(group, set())
        if record['group_id'] in assignments:
            labels[group].add(assignments[record['group_id']])
        if group in trained:
            labels[group].add('train')
        if group in held:
            labels[group].add('historical_holdout')
    for group, values in labels.items():
        if 'historical_holdout' in values or 'conflict' in values or len(values) > 1:
            labels[group] = 'conflict'
        elif values:
            labels[group] = next(iter(values))
        else:
            labels[group] = 'validation' if int(fingerprint(group)[:8], 16) / 2**32 < fraction else 'train'
    for record in records:
        assignments[record['group_id']] = labels[groups.find(record['group_id'])]
    dump_json(assignments_path, assignments)
    for item in prepared:
        group = groups.find(item['group_id'])
        if labels[group] == 'conflict':
            excluded.add(item['source_id'])
            continue  # No new copy of a historical holdout enters training.
        split = labels[group]
        (train_ids if split == 'train' else hold_ids).add(item['source_id'])
        name = split + '_' + item['kind'] + '.jsonl'
        new_rows[name].append(item | {'group_id': group})
        if item['kind'] == 'domain' and item['source_kind'] == 'untrusted_attachment':
            example = instruction(item['text'])
            if example:
                new_rows[split + '_instruction.jsonl'].append({**example, 'kind': 'instruction',
                            'source_id': item['source_id'], 'group_id': group, 'fingerprint': item['fingerprint']})
    # No usable additions: reuse the foundation without writing duplicate corpora.
    if not train_ids and not hold_ids:
        return seed, {**verify_data(seed), 'continuous': {'revision': revision, 'training_record_ids': [],
                     'holdout_record_ids': [], 'excluded_holdout_overlap': len(excluded), 'oldest_new_at': None}}
    temporary = root / 'data' / (destination.name + '_building')
    temporary.mkdir(exist_ok=True, mode=0o700)
    artifacts = {}
    for name in FILES:
        additions = new_rows[name] if name.startswith('train_') else []
        with (temporary / name).open('w', encoding='utf-8') as stream:
            for row in base[name] + additions:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        artifacts[name] = {'sha256': digest_file(temporary / name)}
    # Independent new-case holdout remains separate from the historical score.
    for kind in ('domain', 'instruction'):
        name = 'new_validation_' + kind + '.jsonl'
        with (temporary / name).open('w', encoding='utf-8') as stream:
            for row in new_rows['validation_' + kind + '.jsonl']:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        artifacts[name] = {'sha256': digest_file(temporary / name)}
    report = {'status': 'completed', 'source_artifact_sha256': revision, 'artifacts': artifacts,
              'counts': {name[:-6]: len(base[name]) + (len(new_rows[name]) if name.startswith('train_') else 0) for name in FILES},
              'continuous': {'revision': revision, 'foundation': foundation['prepared'],
                             'training_record_ids': sorted(train_ids), 'holdout_record_ids': sorted(hold_ids),
                             'excluded_holdout_overlap': len(excluded),
                             'oldest_new_at': min((r['created_at'] for r in records if r['id'] in train_ids), default=None)},
              'limits': ['全量历史训练资料与新增合格资料各训练一轮。',
                         '历史留出集固定；90字复制片段隔离不保证识别全部近义改写。',
                         '用户提供的结果依据未经独立审计；模型输出不会自动成为训练答案。']}
    dump_json(temporary / 'report.json', report)
    temporary.rename(destination)
    return destination, report
