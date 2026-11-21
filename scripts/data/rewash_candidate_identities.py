"""Repair identities in sanitized candidate runs without reopening PST files."""
from collections import Counter
from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

from candidate_privacy import CandidateIdentityCleaner
from catalog_common import ROOT, digest_file
from scripts.data.derive_mail_candidates import build_identity_index, connect_archive


NUMBERS = re.compile(r'\d+(?:[.,]\d+)*')
RELEASE = {'training_allowed': False, 'published': False, 'external_mail_transmission': False,
           'reason': 'Automated identity repair remains a local candidate; no external release.'}


def write_json(path, value):
    with Path(path).open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read_rows(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def snapshot(paths):
    return {str(path): {'sha256': digest_file(path), 'bytes': path.stat().st_size,
                        'mtime_ns': path.stat().st_mtime_ns} for path in paths}


def repair_run(source, output, cleaner, *, kind):
    source, output = Path(source).resolve(), Path(output).resolve()
    if kind not in ('mail', 'attachment'):
        raise ValueError('invalid_candidate_kind')
    if output.exists():
        raise ValueError('output_already_exists')
    if output == source or source in output.parents:
        raise ValueError('output_overlaps_source')
    required = (['candidates.jsonl', 'engineering_candidates.jsonl'] if kind == 'mail' else
                ['documents.jsonl', 'occurrences.jsonl', 'engineering_candidates.jsonl'])
    report = json.loads((source / 'report.json').read_text(encoding='utf-8'))
    # Verify all declared artifacts, including mail status/quarantine sidecars.
    inputs = [source / 'report.json']
    for name, expected in report['artifacts'].items():
        path = (source / name).resolve()
        if path.parent != source or not path.is_file() or digest_file(path) != expected['sha256']:
            raise ValueError('source_artifact_mismatch')
        inputs.append(path)
    if any(not (source / name).is_file() for name in required):
        raise ValueError('required_source_artifact_missing')
    before = snapshot(inputs)
    output.mkdir(parents=True, exist_ok=False)
    stats = Counter()
    operations_before = cleaner.counts.copy()
    text_files = ['candidates.jsonl', 'engineering_candidates.jsonl'] if kind == 'mail' else ['engineering_candidates.jsonl']
    mappings = []
    identity_fields = ('id',) if kind == 'mail' else ('id', 'document_id', 'parent_source_ids')
    try:
        for name in text_files:
            stats[name + '_rows'] = 0
            with (output / name).open('w', encoding='utf-8', newline='\n') as stream:
                for row in read_rows(source / name):
                    original = dict(row)
                    changed = False
                    for field in ('subject', 'text'):
                        text = row[field]
                        updated = cleaner.clean(text)
                        if NUMBERS.findall(text) != NUMBERS.findall(updated):
                            raise ValueError('engineering_numbers_changed')
                        if cleaner.residuals(updated):
                            raise ValueError('candidate_residual_audit_failed')
                        if updated != text:
                            row[field] = updated
                            stats[field + '_fields_changed'] += 1
                            changed = True
                    if any(row.get(key) != original.get(key) for key in identity_fields):
                        raise ValueError('source_identity_changed')
                    if kind == 'mail':
                        row['training_allowed'] = False
                        row['review_required'] = True
                    else:
                        row['training_allowed'] = False
                        row['manual_review_required'] = True
                    mappings.append({'artifact': name, 'id': row['id'],
                                     'source_record_sha256': hashlib.sha256(
                                         json.dumps(original, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                                     'changed': changed})
                    stats[name + '_rows'] += 1
                    stats['rows_changed'] += changed
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        for name in report['artifacts']:
            if name in text_files or name in ('report.json', 'progress.json', 'RELEASE_BLOCKED.json'):
                continue
            shutil.copyfile(source / name, output / name)
        with (output / 'rewash_map.jsonl').open('w', encoding='utf-8', newline='\n') as stream:
            for row in mappings:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        write_json(output / 'RELEASE_BLOCKED.json', RELEASE)
        if before != snapshot(inputs):
            raise ValueError('source_changed_during_repair')
        # Independent read-back and one-to-one correspondence against inputs.
        for name in text_files:
            originals = list(read_rows(source / name))
            repaired = list(read_rows(output / name))
            if len(originals) != len(repaired):
                raise ValueError('candidate_record_count_changed')
            for first, second in zip(originals, repaired):
                if any(first.get(key) != second.get(key) for key in identity_fields):
                    raise ValueError('candidate_id_order_changed')
                for field in ('subject', 'text'):
                    if cleaner.residuals(second[field]) or NUMBERS.findall(first[field]) != NUMBERS.findall(second[field]):
                        raise ValueError('read_back_audit_failed')
                stats['records_read_back'] += 1
        result = dict(report)
        result.update({'status': 'completed_identity_repair_local_candidates',
                       'release': RELEASE,
                       'rewash': {'source_run': source.name, 'output_run': output.name, 'kind': kind,
                                  'source_snapshots': before, 'source_unchanged': True,
                                  'statistics': dict(stats), 'operations': dict(cleaner.counts - operations_before),
                                  'numbers_preserved': True, 'ids_and_order_preserved': True,
                                  'residual_audit': {},
                                  'limits': ['Rule and archive-contact checks do not prove all identities removed.',
                                             'No PST read, OCR, model training or external service used.']}})
        result['artifacts'] = {path.name: {'sha256': digest_file(path), 'size_bytes': path.stat().st_size}
                               for path in sorted(output.iterdir()) if path.is_file()}
        write_json(output / 'report.json', result)
        return {'source': source.name, 'output': output.name, **result['rewash']}
    except Exception as exc:
        write_json(output / 'REPAIR_FAILED.json', {'status': 'failed', 'error_type': type(exc).__name__,
                   'error_code': str(exc) if isinstance(exc, ValueError) else 'candidate_repair_failed'})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=ROOT / 'data/mail_archive_v1/mail_archive.sqlite3')
    parser.add_argument('--sensitive-terms', type=Path, default=ROOT / '.private/sensitive_terms.txt')
    parser.add_argument('--mail-run', action='append', default=[], help='SOURCE=OUTPUT')
    parser.add_argument('--attachment-run', action='append', default=[], help='SOURCE=OUTPUT')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    runs = []
    for kind, values in (('mail', args.mail_run), ('attachment', args.attachment_run)):
        for value in values:
            source, separator, output = value.partition('=')
            if not separator or not source or not output:
                parser.error('run must be SOURCE=OUTPUT')
            if Path(output).exists():
                parser.error('output already exists')
            runs.append((kind, source, output))
    if not runs:
        parser.error('at least one candidate run is required')
    started = time.monotonic()
    archive_before = snapshot([args.archive])
    print(json.dumps({'event': 'building_local_contact_index'}), flush=True)
    with closing(connect_archive(args.archive)) as connection:
        identities = build_identity_index(connection, args.sensitive_terms)
    cleaner = CandidateIdentityCleaner(identities.terms)
    print(json.dumps({'event': 'contact_index_ready', 'terms': len(cleaner.terms),
                      'contextual_aliases': len(cleaner.aliases)}), flush=True)
    results = []
    for kind, source, output in runs:
        result = repair_run(source, output, cleaner, kind=kind)
        results.append(result)
        print(json.dumps({'event': 'candidate_repaired', 'output': result['output'],
                          'statistics': result['statistics'], 'operations': result['operations']}), flush=True)
    if archive_before != snapshot([args.archive]):
        raise ValueError('archive_changed_during_repair')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    # Persist only counts/hashes; identity terms and source text stay in memory.
    write_json(args.report, {'schema': 'candidate-identity-repair-v1', 'status': 'completed_local_candidates',
        'runs': results, 'archive_unchanged': True, 'archive_sha256': digest_file(args.archive),
        'identity_terms': len(cleaner.terms), 'contextual_aliases': len(cleaner.aliases),
        'elapsed_seconds': round(time.monotonic()-started, 1), 'release': RELEASE,
        'external_services': False})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
