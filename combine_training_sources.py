"""Create a new version of local training sources from sanitized mail and attachments."""
import argparse
import json
from pathlib import Path
import re

from learning_common import ROOT, digest_file, dump_json, emit


def filter_ocr_text(text):
    """Do not join spaced digits: they may be separate cells or a recognition error."""
    kept, removed = [], 0
    for line in text.splitlines():
        if re.search(r'\d[ \t]+\d', line):
            removed += 1
        else:
            kept.append(line)
    return '\n'.join(kept), removed


def combine(body_run, attachment_run, output):
    body_run, attachment_run, output = map(Path, (body_run, attachment_run, output))
    body_report = json.loads((body_run / 'report.json').read_text())
    attachment_report = json.loads((attachment_run / 'report.json').read_text())
    if (body_report['status'] != 'completed_local_candidates_require_review'
            or attachment_report['status'] != 'completed_local_attachment_candidates'):
        raise ValueError('source_run_incomplete')
    name = 'engineering_candidates.jsonl'
    for directory, report in ((body_run, body_report), (attachment_run, attachment_report)):
        if digest_file(directory / name) != report['artifacts'][name]['sha256']:
            raise ValueError('source_checksum_mismatch')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    counts = {'body_records': 0, 'attachment_records': 0, 'ocr_records': 0, 'ambiguous_numeric_ocr_lines_excluded': 0}
    seen = set()
    with (output / (name + '.partial')).open('w', encoding='utf-8') as stream:
        for directory, key in ((body_run, 'body_records'), (attachment_run, 'attachment_records')):
            with (directory / name).open(encoding='utf-8') as inputs:
                for line in inputs:
                    row = json.loads(line)
                    if row['id'] in seen:
                        raise ValueError('duplicate_source_identifier')
                    seen.add(row['id'])
                    if key == 'attachment_records':
                        if row['source_kind'] != 'untrusted_attachment' or not row.get('parent_source_ids'):
                            raise ValueError('attachment_parent_missing')
                        if row['extraction_method'] == 'local_ocr':
                            row['text'], removed = filter_ocr_text(row['text'])
                            counts['ambiguous_numeric_ocr_lines_excluded'] += removed
                            row['ocr_ambiguous_numeric_lines_excluded'] = removed
                        # This explicit metadata context keeps numeric table rows associated with their document topic.
                        row['text'] = '附件文档主题：' + '、'.join(row['document_topics']) + '\n' + row['text']
                        row['document_context_added'] = True
                        counts['ocr_records'] += row['extraction_method'] == 'local_ocr'
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                    counts[key] += 1
    (output / (name + '.partial')).replace(output / name)
    report = {'status': 'completed_local_training_sources', 'counts': counts,
              'source_runs': {'body': body_run.name, 'attachments': attachment_run.name},
              'artifacts': {name: {'sha256': digest_file(output / name), 'size_bytes': (output / name).stat().st_size}},
              'manual_review_required': False, 'local_training_allowed': True,
              'limits': ['附件与父邮件及同一文档必须在同一训练/评测分组。',
                         'OCR 原文保存在附件结果中；含数字间空格的歧义行从训练输入排除，不猜测或拼接数值。',
                         'OCR存在识别误差，不等同于识别图形或曲线含义。']}
    dump_json(output / 'report.json', report)
    dump_json(output / 'progress.json', {'status': 'completed', 'counts': counts})
    if output.resolve().parent == (ROOT / 'data').resolve():
        dump_json(ROOT / 'data/current_training_sources.json', {'run_name': output.name,
                  'source_artifact_sha256': report['artifacts'][name]['sha256']})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='合并正文和附件的本地训练来源')
    parser.add_argument('--body-run', type=Path, default=ROOT / 'data/sanitized_v3')
    parser.add_argument('--attachment-run', type=Path, default=ROOT / 'data/attachments_v1')
    parser.add_argument('--run-name', default='combined_sources_v1')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.run_name):
        parser.error('invalid run name')
    try:
        result = combine(args.body_run, args.attachment_run, ROOT / 'data' / args.run_name)
        emit('combined_sources_ready', **result['counts'])
    except Exception as exc:
        emit('source_combination_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
