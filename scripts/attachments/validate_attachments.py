"""Check completed attachment artifacts and provenance without printing extracted text."""
import argparse
import collections
import json
from pathlib import Path
import re

from learning_common import ROOT, digest_file, dump_json, emit


def validate(directory):
    directory = Path(directory)
    report = json.loads((directory / 'report.json').read_text())
    progress = json.loads((directory / 'progress.json').read_text())
    assert report['status'] == 'completed_local_attachment_candidates'
    assert progress['status'] == 'completed'
    assert progress['counts'] == report['counts']
    for name, artifact in report['artifacts'].items():
        assert digest_file(directory / name) == artifact['sha256']
        assert (directory / name).stat().st_size == artifact['size_bytes']
    occurrences = 0
    referenced = collections.defaultdict(set)
    with (directory / 'occurrences.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            assert re.fullmatch(r'mail_[0-9a-f]{24}', row['parent_source_id'])
            if row['status'] == 'read':
                referenced[row['document_id']].add(row['parent_source_id'])
            occurrences += 1
    documents = {}
    with (directory / 'documents.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            assert row['document_id'] not in documents
            assert set(row['parent_source_ids']) == referenced[row['document_id']]
            if row['status'] != 'processed':
                assert row['exported_units'] == 0
            documents[row['document_id']] = row
    seen = set()
    exported = collections.Counter()
    methods = collections.Counter()
    with (directory / 'engineering_candidates.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            assert row['id'] not in seen
            seen.add(row['id'])
            assert row['source_kind'] == 'untrusted_attachment'
            assert row['training_allowed'] is True and row['manual_review_required'] is False
            assert row['independently_verified'] is False
            assert row['text'].strip() and row['document_topics']
            document = documents[row['document_id']]
            assert row['parent_source_ids'] == document['parent_source_ids']
            limited = any(value and ('limit' in key or 'stopped' in key) for key, value in document['parser_stats'].items())
            partial = (document['parser_stats'].get('embedded_image_errors') or document['parser_stats'].get('archive_member_errors')
                       or document['parser_stats'].get('unsupported_members') or document['counts'].get('residual_units_quarantined'))
            expected = 'limited' if limited else ('partial_components' if partial and report.get('schema_version', 1) >= 2 else 'parsed_text')
            assert row['source_coverage'] == expected
            if row['extraction_method'] == 'local_ocr':
                assert .65 <= row['ocr_min_confidence'] <= 1.000001
            exported[row['document_id']] += 1
            methods[row['extraction_method']] += 1
    assert occurrences == report['counts']['attachment_occurrences']
    assert len(documents) == report['counts']['unique_documents'] == report['counts']['unique_documents_completed']
    assert len(seen) == report['counts']['exported_units']
    assert all(exported[key] == row['exported_units'] for key, row in documents.items())
    assert not list(directory.glob('*.partial'))
    result = {'status': 'passed', 'attachment_occurrences': occurrences, 'unique_documents': len(documents),
              'engineering_documents': len(exported), 'exported_units': len(seen), 'extraction_methods': dict(methods),
              'artifact_checksums_verified': len(report['artifacts']), 'parent_references_verified': True,
              'limited_documents': sum(any(value and ('limit' in key or 'stopped' in key) for key, value in row['parser_stats'].items()) for row in documents.values())}
    dump_json(directory / 'validation.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, default=ROOT / 'data/attachments_v1')
    args = parser.parse_args()
    try:
        emit('attachments_validated', **validate(args.run))
    except Exception as exc:
        emit('attachment_validation_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
