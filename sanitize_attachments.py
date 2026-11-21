"""Read PST attachments locally, deduplicate, extract/OCR and sanitize before export."""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import contextlib
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import time
import zipfile

from sanitize_pst import collect_contacts, iter_messages, safe_property, hash_file, write_json, emit
from mail_privacy import IdentityIndex, Sanitizer, opaque_id, technical_topics
from attachment_extract import extract_attachment, file_kind, MAX_BYTES

ROOT = Path(__file__).resolve().parent
ENGINE = None
OCR_ENABLED = True


def _init_worker(identity_terms, ocr_enabled):
    """Initialize an isolated worker for spawn-based platforms."""
    global ENGINE, OCR_ENABLED
    import spacy
    from mail_privacy import IdentityMatcher, Sanitizer
    nlp_zh = spacy.load('zh_core_web_sm', disable=['tagger', 'parser', 'attribute_ruler', 'lemmatizer', 'senter'])
    nlp_en = spacy.load('en_core_web_sm', disable=['tagger', 'parser', 'attribute_ruler', 'lemmatizer', 'senter'])
    ENGINE = Sanitizer(IdentityMatcher(identity_terms), nlp_zh, nlp_en)
    OCR_ENABLED = ocr_enabled


def error_reason(exc):
    message = str(exc).lower()
    if 'encrypt' in message or 'password' in message:
        return 'encrypted_or_password_required'
    if 'limit' in message:
        return 'resource_limit'
    if 'corrupt' in message or 'bof record' in message or 'not a zip' in message:
        return 'invalid_or_corrupt_format'
    return 'parser_error'


def source_coverage(result):
    stats = result['parser_stats']
    if any(value and ('limit' in key or 'stopped' in key) for key, value in stats.items()):
        return 'limited'
    if (stats.get('embedded_image_errors') or stats.get('archive_member_errors') or stats.get('unsupported_members')
            or result['counts'].get('residual_units_quarantined')):
        return 'partial_components'
    return 'parsed_text'


def cache_reusable(result, signature, compatible_signature=None):
    if not result:
        return False
    if result.get('processing_signature') == signature:
        return True
    if (not compatible_signature or result.get('processing_signature') != compatible_signature
            or result.get('status') != 'processed'):
        return False
    return not any(value and ('limit' in key or 'stopped' in key) for key, value in result.get('parser_stats', {}).items())


def process_attachment(job):
    doc_id, kind, content = job
    try:
        media_formats = collections.Counter()
        if kind in ('xlsx', 'xlsm', 'docx', 'pptx'):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for info in archive.infolist():
                    if info.filename.startswith(('xl/media/', 'word/media/', 'ppt/media/')) and not info.is_dir():
                        ext = Path(info.filename).suffix.lower().lstrip('.')
                        media_formats[ext if ext in {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tif', 'tiff', 'wmf', 'emf', 'svg'} else 'other'] += 1
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            units, parser_stats = extract_attachment(content, kind, ocr=OCR_ENABLED)
        counts = collections.Counter()
        records = []
        topics = set()
        for number, unit in enumerate(units):
            counts['extracted_units'] += 1
            text = ENGINE.clean(unit.text)
            if not text.strip():
                counts['empty_after_redaction'] += 1
                continue
            if ENGINE.residuals(text):
                counts['residual_units_quarantined'] += 1
                continue
            unit_topics = technical_topics(text)
            topics.update(unit_topics)
            records.append({'id': doc_id + '_unit_' + str(number), 'document_id': doc_id,
                            'subject': kind.upper() + ' ' + unit.label, 'text': text,
                            'topics': unit_topics, 'extraction_method': unit.method,
                            'ocr_min_confidence': unit.confidence,
                            'source_kind': 'untrusted_attachment', 'manual_review_required': False,
                            'training_allowed': True, 'independently_verified': False})
        return {'document_id': doc_id, 'status': 'processed', 'format': kind, 'records': records,
                'embedded_media_formats': dict(media_formats),
                'document_topics': sorted(topics), 'counts': dict(counts), 'parser_stats': parser_stats}
    except Exception as exc:
        return {'document_id': doc_id, 'format': kind, 'status': 'parse_failed',
                'error_type': type(exc).__name__, 'error_reason': error_reason(exc),
                'records': [], 'document_topics': [], 'counts': {}, 'parser_stats': {}}


def run(args):
    global ENGINE, OCR_ENABLED
    OCR_ENABLED = not args.no_ocr
    import pypff
    import spacy
    os.umask(0o077)
    output = ROOT / 'data' / args.run_name
    output.mkdir(parents=True, exist_ok=args.resume, mode=0o700)
    cache = ROOT / '.private' / (args.run_name + '_cache')
    cache.mkdir(exist_ok=True, mode=0o700)
    source = Path(args.source).resolve()
    before = source.stat()
    key = (ROOT / '.private/pseudonym.key').read_bytes()
    policy = json.loads((ROOT / 'attachment_policy.json').read_text())
    if (policy['local_attachment_processing_allowed'] is not True or policy['external_services'] is not False
            or policy['execute_macros_or_formulas'] is not False):
        raise ValueError('attachment_policy_mismatch')
    import attachment_extract
    attachment_extract.MAX_CHARACTERS = policy['max_document_characters']
    attachment_extract.MAX_PAGES = policy['max_document_pages']
    attachment_extract.MAX_ROWS = policy.get('max_table_rows', 50000)
    attachment_extract.MAX_COLUMNS = policy.get('max_table_columns', 200)
    compatible_signature = None
    if args.reuse_completed_cache:
        compatibility = json.loads((ROOT / '.private/attachment_cache_compatibility.json').read_text())
        if (compatibility['source_run'] != args.run_name
                or compatibility['sanitizer_sha256'] != hash_file(ROOT / 'mail_privacy.py')):
            raise ValueError('incompatible_privacy_cache')
        compatible_signature = compatibility['signature']
    signature = hashlib.sha256(''.join(hash_file(ROOT / name) for name in
                              ('attachment_extract.py', 'mail_privacy.py', 'attachment_policy.json')).encode()).hexdigest()
    previous_cache = None
    if args.cache_from:
        previous_report = json.loads((ROOT / 'data' / args.cache_from / 'report.json').read_text())
        if (previous_report['status'] != 'completed_local_attachment_candidates'
                or previous_report['processing_signature'] != signature):
            raise ValueError('previous_cache_not_compatible')
        previous_cache = ROOT / '.private' / (args.cache_from + '_cache')
    stats, scan, formats, totals = (collections.Counter() for _ in range(4))
    started = time.monotonic()
    def progress(stage):
        write_json(output / 'progress.json', {'status': stage, 'counts': dict(stats),
                                             'scan': dict(scan), 'elapsed_seconds': round(time.monotonic() - started)})
        emit('attachment_progress', stage=stage, **stats, elapsed_seconds=round(time.monotonic() - started))
    progress('loading_local_models')
    nlp_zh = spacy.load('zh_core_web_sm', disable=['tagger', 'parser', 'attribute_ruler', 'lemmatizer', 'senter'])
    nlp_en = spacy.load('en_core_web_sm', disable=['tagger', 'parser', 'attribute_ruler', 'lemmatizer', 'senter'])
    pst = pypff.file()
    pst.open(str(source), 'r')
    index = IdentityIndex()
    failed_contacts = set()
    progress('collecting_identities')
    for message in iter_messages(pst.root_folder, scan):
        stats['identity_messages'] += 1
        try:
            collect_contacts(message, index)
        except Exception:
            failed_contacts.add(message.identifier)
            stats['identity_errors'] += 1
        if stats['identity_messages'] % 1000 == 0:
            progress('collecting_identities')
    sensitive = ROOT / '.private/sensitive_terms.txt'
    if sensitive.exists():
        for line in sensitive.read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                index.add(line)
    ENGINE = Sanitizer(index.pattern(), nlp_zh, nlp_en)
    try:
        process_context = multiprocessing.get_context('fork')
        worker_kwargs = {}
    except ValueError:
        process_context = multiprocessing.get_context('spawn')
        worker_kwargs = {'initializer': _init_worker,
                         'initargs': (frozenset(index.terms), OCR_ENABLED)}
    registry = {}
    seen = set()
    results = {}
    last = time.monotonic()
    traversal = collections.Counter()
    def consume(result):
        if result.get('processing_signature') and result['processing_signature'] != signature:
            result.setdefault('origin_processing_signature', result['processing_signature'])
        result['processing_signature'] = signature
        write_json(cache / (result['document_id'] + '.json'), result)
        results[result['document_id']] = result
        stats['unique_documents_completed'] += 1
        stats[result['status']] += 1
        totals.update(result['counts'])
        totals.update(result['parser_stats'])
    progress('extracting_and_sanitizing')
    with (output / 'occurrences.jsonl.partial').open('w', encoding='utf-8') as occurrence_out:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers,
                mp_context=process_context, **worker_kwargs) as pool:
            pending = set()
            visited = 0
            for message in iter_messages(pst.root_folder, traversal):
                visited += 1
                if args.limit_messages and visited > args.limit_messages:
                    break
                stats['attachment_messages_visited'] += 1
                parent_id = opaque_id(key, message.identifier)
                try:
                    count = message.number_of_attachments
                except Exception:
                    stats['attachment_list_errors'] += 1
                    continue
                for number in range(count):
                    stats['attachment_occurrences'] += 1
                    occurrence = {'parent_source_id': parent_id, 'attachment_ordinal': number}
                    try:
                        if message.identifier in failed_contacts:
                            occurrence['status'] = 'parent_contact_error'
                            stats['parent_contact_error_attachments'] += 1
                        else:
                            attachment = message.get_attachment(number)
                            size = attachment.size
                            if not 0 < size <= MAX_BYTES:
                                occurrence['status'] = 'empty_or_oversize_attachment'
                                stats['empty_or_oversize_attachments'] += 1
                            else:
                                content = attachment.read_buffer(size)
                                if len(content) != size:
                                    raise ValueError('incomplete_attachment_read')
                                raw_digest = hashlib.sha256(content).hexdigest()
                                doc_id = 'att_' + opaque_id(key, 'attachment:' + raw_digest)[5:]
                                occurrence['document_id'] = doc_id
                                occurrence['status'] = 'read'
                                if doc_id in seen:
                                    stats['duplicate_occurrences'] += 1
                                    registry[doc_id]['parents'].add(parent_id)
                                else:
                                    kind = file_kind(str(safe_property(attachment, 'long_filename', '') or ''), content)
                                    seen.add(doc_id)
                                    formats[kind] += 1
                                    registry[doc_id] = {'parents': {parent_id}, 'format': kind}
                                    stats['unique_documents'] += 1
                                    cached = cache / (doc_id + '.json')
                                    if not cached.exists() and previous_cache is not None:
                                        cached = previous_cache / cached.name
                                    result = json.loads(cached.read_text()) if cached.exists() else None
                                    if ((cache_reusable(result, signature, compatible_signature)
                                            or (args.reuse_any_cache and result and result.get('status') == 'processed'))
                                            and not (args.retry_failed and result.get('status') == 'parse_failed')
                                            and not (args.retry_partial_images and result.get('parser_stats', {}).get('embedded_image_errors'))):
                                        stats['cache_hits'] += 1
                                        consume(result)
                                    else:
                                        pending.add(pool.submit(process_attachment, (doc_id, kind, content)))
                                    while len(pending) >= args.workers * 2:
                                        done, pending = concurrent.futures.wait(pending, timeout=20,
                                                return_when=concurrent.futures.FIRST_COMPLETED)
                                        for future in done:
                                            consume(future.result())
                                        if time.monotonic() - last >= 20:
                                            progress('extracting_and_sanitizing')
                                            last = time.monotonic()
                    except Exception:
                        occurrence['status'] = 'attachment_read_error'
                        stats['attachment_read_errors'] += 1
                    occurrence_out.write(json.dumps(occurrence, ensure_ascii=False) + '\n')
                    if time.monotonic() - last >= 20:
                        progress('extracting_and_sanitizing')
                        last = time.monotonic()
            while pending:
                done, pending = concurrent.futures.wait(pending, timeout=20,
                                                        return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    consume(future.result())
                if time.monotonic() - last >= 20:
                    progress('extracting_and_sanitizing')
                    last = time.monotonic()
    pst.close()
    progress('auditing_attachment_outputs')
    artifacts = {}
    manifest_path = output / 'documents.jsonl.partial'
    candidate_path = output / 'engineering_candidates.jsonl.partial'
    with manifest_path.open('w', encoding='utf-8') as manifests, candidate_path.open('w', encoding='utf-8') as candidates:
        for doc_id, result in results.items():
            parents = sorted(registry[doc_id]['parents'])
            topics = result['document_topics']
            exported = 0
            for record in result['records']:
                if ENGINE.residuals(record['subject'] + '\n' + record['text']):
                    raise ValueError('attachment_readback_audit_failed')
                if not topics:
                    continue
                record = record | {'parent_source_ids': parents, 'document_topics': topics,
                                   'source_coverage': source_coverage(result)}
                candidates.write(json.dumps(record, ensure_ascii=False) + '\n')
                exported += 1
            stats['exported_units'] += exported
            stats['engineering_documents'] += bool(exported)
            manifests.write(json.dumps({'document_id': doc_id, 'parent_source_ids': parents,
                              'format': result['format'], 'status': result['status'], 'exported_units': exported,
                              'error_type': result.get('error_type'), 'origin_processing_signature': result.get('origin_processing_signature'),
                              'error_reason': result.get('error_reason'), 'embedded_media_formats': result.get('embedded_media_formats', {}),
                              'counts': result['counts'], 'parser_stats': result['parser_stats']}, ensure_ascii=False) + '\n')
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('source_changed')
    if len(results) != len(registry):
        raise ValueError('incomplete_attachment_results')
    for name in ('occurrences.jsonl', 'documents.jsonl', 'engineering_candidates.jsonl'):
        (output / (name + '.partial')).replace(output / name)
        artifacts[name] = {'sha256': hash_file(output / name), 'size_bytes': (output / name).stat().st_size}
    report = {'status': 'completed_local_attachment_candidates', 'schema_version': 2, 'counts': dict(stats),
              'cache_source_run': args.cache_from,
              'unique_formats': dict(formats), 'extraction_counts': dict(totals),
              'artifacts': artifacts, 'source_metadata_unchanged': True, 'policy': policy,
              'processing_signature': signature, 'elapsed_seconds': round(time.monotonic() - started),
              'limits': ['OCR仅识别图像中的文字，不理解剖面、图形或曲线的几何含义；识别的数值可能有误。',
                         '表格读取静态或缓存值，不运行公式、宏或外部链接。',
                         '无法解析的内容不进入候选；超限或部分组件失败的文档仅导出已成功解析部分，并标记覆盖限制。']}
    write_json(output / 'report.json', report)
    progress('completed')
    emit('attachments_completed', **stats)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='本地附件提取、OCR、脱敏与去重')
    parser.add_argument('source', nargs='?', default=str(ROOT / '19924784-20240420.pst'))
    parser.add_argument('--run-name', default='attachments_v1')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--reuse-completed-cache', action='store_true', help='仅复用已验证兼容、未触及上限的旧版本缓存')
    parser.add_argument('--retry-failed', action='store_true', help='重新处理已有缓存中的解析失败条目')
    parser.add_argument('--cache-from', help='复用一个已完成且提取规则一致的附件运行')
    parser.add_argument('--retry-partial-images', action='store_true', help='补处理存在嵌入图片失败的文档')
    parser.add_argument('--no-ocr', action='store_true', help='跳过图片 OCR，仅提取原生文本')
    parser.add_argument('--reuse-any-cache', action='store_true', help='复用已完成的旧缓存，忽略实现指纹变化')
    parser.add_argument('--limit-messages', type=int, default=0, help='仅处理前 N 封邮件（用于抽样）')
    args = parser.parse_args()
    import re
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.run_name) or not 1 <= args.workers <= 6:
        parser.error('invalid run configuration')
    if args.cache_from and not re.fullmatch(r'[a-zA-Z0-9_-]+', args.cache_from):
        parser.error('invalid cache source')
    try:
        run(args)
    except Exception as exc:
        emit('attachment_processing_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
