"""Sanitize local submissions/documents; never train on assistant generations."""
import argparse
import contextlib
import io
import os
import time

from pcb_assistant.training.storage import ROOT, fingerprint, lock, read, setup, validate_case
from pcb_assistant.training.common import digest_file, dump_json, emit
from pcb_assistant.privacy.mail import IdentityIndex, Sanitizer, technical_topics
from pcb_assistant.training.prepare import eligible


def sanitizer():
    import spacy
    disable = ['tagger', 'parser', 'attribute_ruler', 'lemmatizer', 'senter']
    return Sanitizer(IdentityIndex().pattern(), spacy.load('zh_core_web_sm', disable=disable),
                     spacy.load('en_core_web_sm', disable=disable))


def ingest(root=ROOT, engine=None):
    setup(root)
    receipts_path = root / 'data/continuous/intake_receipts.json'
    receipts = read(receipts_path, {})
    pending = list((root / '.private/learning_pending').glob('*.json'))
    files = [p for p in (root / 'data/learning_inbox').rglob('*')
             if p.is_file() and not p.is_symlink() and time.time() - p.stat().st_mtime > 5
             and p.suffix.lower() not in ('.partial', '.tmp')]
    counts = {'imported': 0, 'unchanged': 0, 'failed': 0, 'excluded': 0}
    for path in pending + files:
        key = None
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                counts['excluded'] += 1
                continue
            before = path.stat()
            key = fingerprint([digest_file(path), path.suffix.lower(), 'intake-v1'])
            if key in receipts:
                counts['unchanged'] += 1
                if path in pending:
                    path.unlink()
                continue
            engine = engine or sanitizer()
            created = time.time()
            if path in pending or path.suffix.lower() == '.json':
                payload = read(path)
                raw = validate_case(payload['case'] if path in pending else payload)
                fields = {name: engine.clean(raw[name]) for name in ('context', 'conclusion', 'evidence')}
                combined = '\n'.join(fields.values())
                if engine.residuals(combined):
                    raise ValueError('privacy_residual')
                group = 'case_' + fingerprint(raw['case_id'])
                text = '案例条件：' + fields['context'] + '\n记录中的判断：' + fields['conclusion']
                response = ('记录报告的结论：' + fields['conclusion'] + '\n记录中的验证依据：' + fields['evidence']
                            + '\n适用边界：仅适用于本案例条件；该记录尚未经独立审计，不等同于通用量产配方。')
                usable = raw['kind'] != 'note' and eligible(text) and len(fields['evidence']) >= 20
                samples = [{'text': text + '\n记录依据：' + fields['evidence'],
                            'instruction': {'prompt': '根据以下案例条件整理已有记录的结论、依据与适用边界：\n' + fields['context'],
                                            'response': response},
                            'source_kind': 'reported_engineering_case'}] if usable else []
                record = {'kind': raw['kind'], 'group_id': group, 'samples': samples,
                          'recorded_fields': fields,
                          'capability': raw['capability'], 'eligible': usable,
                          'evidence_status': 'user_reported_not_independently_verified'}
            else:
                from pcb_assistant.ingestion.extract import extract_attachment, file_kind
                from pcb_assistant.training.sources import filter_ocr_text
                content = path.read_bytes()
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    units, stats = extract_attachment(content, file_kind(path.name, content))
                cleaned, topics = [], set()
                for unit in units:
                    text = engine.clean(unit.text)
                    if unit.method == 'local_ocr':
                        text, _ = filter_ocr_text(text)
                    if text and not engine.residuals(text):
                        cleaned.append(text)
                        topics.update(technical_topics(text))
                samples = [{'text': '附件文档主题：' + '、'.join(sorted(topics)) + '\n' + text,
                            'source_kind': 'untrusted_attachment'} for text in cleaned] if topics else []
                record = {'kind': 'document', 'group_id': 'doc_' + key, 'samples': samples,
                          'eligible': bool(samples), 'parser_stats': stats,
                          'evidence_status': 'unverified_document'}
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('input_changed_during_ingestion')
            # Content-based identity prevents duplicate uploads from triggering training.
            ident = fingerprint(record)
            target = root / 'data/continuous/records' / (ident + '.json')
            if not target.exists():
                dump_json(target, {**record, 'id': ident, 'created_at': created})
                counts['imported'] += 1
            receipts[key] = {'status': 'processed', 'record_id': ident, 'time': created}
            if not record['eligible']:
                counts['excluded'] += 1
            dump_json(receipts_path, receipts)
            if path in pending:
                path.unlink()  # Raw intake is replaced by its private sanitized record.
        except Exception as exc:
            counts['failed'] += 1
            # Retry transient errors on the next poll. Never log text, paths or messages.
            if key:
                failures = read(root / 'data/continuous/intake_errors.json', {})
                failures[key] = {'error_type': type(exc).__name__, 'time': time.time()}
                dump_json(root / 'data/continuous/intake_errors.json', failures)
    dump_json(root / 'data/continuous/intake_status.json', {**counts, 'checked_at': time.time()})
    from collections import Counter
    latest = {}
    for record in sorted((read(p) for p in (root / 'data/continuous/records').glob('*.json')), key=lambda r: r['created_at']):
        if record['kind'] == 'outcome' and record['eligible']:
            latest[record['group_id']] = record
    dump_json(root / 'data/continuous/capability_counts.json', dict(Counter(r['capability'] for r in latest.values())))
    return counts


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.parse_args()
    try:
        with lock('learning_intake'):
            emit('local_intake_completed', **ingest())
    except Exception as exc:
        emit('local_intake_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
