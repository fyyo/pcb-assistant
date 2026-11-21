'''Expert review workflow for severe unsupported conclusions.

The workflow has three explicit phases:

1. ``--prepare`` runs deterministic evidence-only answers and writes a private
   review packet containing the question, answer, and local citations.
2. A domain expert edits only the ``review`` object in that packet.
3. ``--finalize`` validates every expert review and writes the release audit
   report. The tool never creates a passing review on its own.
'''
from __future__ import annotations

from pcb_assistant.evaluation.common import valid_review_time

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from pcb_assistant.evaluation.annotation import case_fingerprint
from pcb_assistant.interfaces.contract import load_cases
import pcb_assistant.interfaces.managed as phase6_managed


from pcb_assistant.paths import ROOT
PRIVATE_ROOT = ROOT / '.private' / 'phase6_severe_review'
DOCS = ROOT / 'docs'
RECORD_SCHEMA = 'pcb-phase6-severe-review-record-v1'
REPORT_SCHEMA = 'pcb-phase6-severe-review-v1'
DEFAULT_LIMIT = 8
DEFAULT_WORKERS = 4
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
RECORD_KEYS = {
    'schema', 'case_id', 'case_fingerprint', 'answer_fingerprint',
    'category', 'question', 'answer', 'citations', 'review',
}
REVIEW_KEYS = {
    'status', 'reviewer', 'reviewed_at', 'unsupported_conclusions', 'notes',
}


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _default_packet_path():
    return PRIVATE_ROOT / date.today().strftime('%Y%m%d') / 'packet.jsonl'


def _default_status_packet_path():
    current = _default_packet_path()
    if current.is_file():
        return current
    try:
        packet_directories = list(PRIVATE_ROOT.iterdir())
    except OSError:
        return current
    candidates = []
    for directory in packet_directories:
        if not directory.is_dir() or not re.fullmatch(r'\d{8}', directory.name):
            continue
        try:
            packet_date = datetime.strptime(directory.name, '%Y%m%d').date()
        except ValueError:
            continue
        packet = directory / 'packet.jsonl'
        if packet.is_file():
            candidates.append((packet_date, packet))
    return max(candidates, default=(None, current), key=lambda item: item[0])[1]


def _default_report_path():
    return DOCS / (
        'PHASE6_3_SEVERE_REVIEW_'
        + date.today().strftime('%Y%m%d') + '.json')




def _citation_row(citation):
    if not isinstance(citation, dict):
        return None
    excerpt_id = citation.get('excerpt_id')
    if not isinstance(excerpt_id, str) or not excerpt_id.startswith('EXC_'):
        return None
    return {
        'excerpt_id': excerpt_id,
        'doc_id': citation.get('doc_id'),
        'title': citation.get('title'),
        'source_type': citation.get('source_type'),
        'text': citation.get('text'),
        'evidence_status': citation.get('evidence_status'),
    }


def run_case(case, *, limit=DEFAULT_LIMIT, catalog_directory=None):
    '''Return a private review record for one answered case, or None.'''
    outcome = phase6_managed.answer(
        {'question': case['question'], 'mode': 'evidence_only', 'limit': limit},
        'REQ_severe_' + case['case_id'],
        environ={},
        catalog_directory=catalog_directory,
    )
    result = outcome.result
    answer = result.get('answer')
    if outcome.status != 'answered' or not isinstance(answer, str) or not answer.strip():
        return None
    citations = [
        row for row in (
            _citation_row(item) for item in result.get('citations') or ())
        if row is not None
    ]
    fingerprint = result.get('result_fingerprint')
    if not isinstance(fingerprint, str) or not _HEX64.match(fingerprint):
        fingerprint = hashlib.sha256(
            _canonical_json({
                'answer': answer,
                'citations': citations,
            }).encode('utf-8')).hexdigest()
    return {
        'schema': RECORD_SCHEMA,
        'case_id': case['case_id'],
        'case_fingerprint': case_fingerprint(case),
        'answer_fingerprint': fingerprint,
        'category': case.get('category'),
        'question': case['question'],
        'answer': answer,
        'citations': citations,
        'review': {
            'status': 'pending',
            'reviewer': None,
            'reviewed_at': None,
            'unsupported_conclusions': None,
            'notes': '',
        },
    }


def prepare_packet(cases, *, limit=DEFAULT_LIMIT, workers=DEFAULT_WORKERS,
                   answer_runner=None, catalog_directory=None):
    '''Run local evidence-only answers and return private review records.'''
    source_cases = list(cases)
    runner = run_case if answer_runner is None else answer_runner
    count = max(1, min(8, int(workers)))
    if count == 1:
        rows = [
            runner(
                case, limit=limit, catalog_directory=catalog_directory)
            for case in source_cases
        ]
    else:
        with ThreadPoolExecutor(max_workers=count) as pool:
            rows = list(pool.map(
                lambda case: runner(
                    case, limit=limit,
                    catalog_directory=catalog_directory),
                source_cases,
            ))
    records = [row for row in rows if row is not None]
    return records, {
        'total_cases': len(source_cases),
        'answered': len(records),
        'skipped': len(source_cases) - len(records),
        'external_services': False,
        'data_egress': 'none',
    }


def load_records(path):
    source = Path(path)
    records = []
    with source.open('r', encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    str(source) + ':' + str(line_number) + ': ' + str(exc)
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    str(source) + ':' + str(line_number)
                    + ': record must be an object')
            records.append(value)
    return records


def write_records(records, path, *, replace=False):
    target = Path(path)
    if target.exists() and not replace:
        raise FileExistsError(str(target) + ' already exists')
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True)
        for record in records
    ]
    payload = '\n'.join(lines) + ('\n' if lines else '')
    handle = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', dir=str(target.parent), delete=False,
        prefix=target.name + '.', suffix='.tmp')
    try:
        with handle as stream:
            stream.write(payload)
            stream.flush()
        Path(handle.name).replace(target)
    finally:
        temporary = Path(handle.name)
        if temporary.exists():
            temporary.unlink()
    return target


def _validate_review(review, prefix):
    issues = []
    if not isinstance(review, dict):
        return [prefix + ' must be an object']
    if set(review) != REVIEW_KEYS:
        issues.append(prefix + ': unexpected or missing keys')
    status = review.get('status')
    if status not in ('pending', 'complete'):
        issues.append(prefix + '.status must be pending or complete')
        return issues
    if not isinstance(review.get('notes', ''), str):
        issues.append(prefix + '.notes must be a string')
    if status == 'pending':
        if review.get('reviewer') is not None:
            issues.append(prefix + '.reviewer must be null while pending')
        if review.get('reviewed_at') is not None:
            issues.append(prefix + '.reviewed_at must be null while pending')
        if review.get('unsupported_conclusions') is not None:
            issues.append(
                prefix + '.unsupported_conclusions must be null while pending')
        return issues
    if not str(review.get('reviewer') or '').strip():
        issues.append(prefix + '.reviewer is required when complete')
    if not valid_review_time(review.get('reviewed_at')):
        issues.append(
            prefix + '.reviewed_at must be ISO 8601 with timezone')
    unsupported = review.get('unsupported_conclusions')
    if (isinstance(unsupported, bool)
            or not isinstance(unsupported, int) or unsupported < 0):
        issues.append(
            prefix + '.unsupported_conclusions must be a non-negative integer')
    elif unsupported > 0 and not str(review.get('notes') or '').strip():
        issues.append(prefix + '.notes are required when unsupported > 0')
    return issues


def validate_records(records, cases=None):
    source_cases = None if cases is None else list(cases)
    by_id = (
        {case['case_id']: case for case in source_cases}
        if source_cases is not None else None)
    issues = []
    seen = set()
    for index, record in enumerate(records):
        prefix = '$.records[' + str(index) + ']'
        if not isinstance(record, dict):
            issues.append(prefix + ' must be an object')
            continue
        if set(record) != RECORD_KEYS:
            issues.append(prefix + ': unexpected or missing keys')
        if record.get('schema') != RECORD_SCHEMA:
            issues.append(prefix + '.schema')
        case_id = record.get('case_id')
        if not isinstance(case_id, str) or not case_id:
            issues.append(prefix + '.case_id is required')
        elif case_id in seen:
            issues.append(prefix + ': duplicate case_id')
        else:
            seen.add(case_id)
        for name in ('case_fingerprint', 'answer_fingerprint'):
            if (not isinstance(record.get(name), str)
                    or not _HEX64.match(record[name])):
                issues.append(prefix + '.' + name + ' must be sha256 hex')
        if not isinstance(record.get('question'), str) or not record['question'].strip():
            issues.append(prefix + '.question is required')
        if not isinstance(record.get('answer'), str) or not record['answer'].strip():
            issues.append(prefix + '.answer is required')
        citations = record.get('citations')
        if not isinstance(citations, list):
            issues.append(prefix + '.citations must be an array')
        else:
            for citation_index, citation in enumerate(citations):
                citation_path = prefix + '.citations[' + str(citation_index) + ']'
                if not isinstance(citation, dict):
                    issues.append(citation_path + ' must be an object')
                    continue
                excerpt_id = citation.get('excerpt_id')
                if (not isinstance(excerpt_id, str)
                        or not re.match(r'^EXC_[0-9a-f]{24}$', excerpt_id)):
                    issues.append(citation_path + '.excerpt_id')
        if by_id is not None:
            base = by_id.get(case_id)
            if base is None:
                issues.append(prefix + ': unknown case_id')
            elif record.get('case_fingerprint') != case_fingerprint(base):
                issues.append(prefix + '.case_fingerprint is stale')
        issues.extend(_validate_review(record.get('review'), prefix + '.review'))
    return issues


def _review(record):
    value = record.get('review') if isinstance(record, dict) else None
    return value if isinstance(value, dict) else {}


def review_summary(records, *, validation_issues=None):
    source_records = list(records)
    complete = [
        record for record in source_records
        if _review(record).get('status') == 'complete'
    ]
    pending = len(source_records) - len(complete)
    all_complete = bool(source_records) and pending == 0
    unsupported_values = [
        _review(record).get('unsupported_conclusions')
        for record in complete
    ]
    unsupported_valid = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in unsupported_values)
    unsupported = (
        sum(unsupported_values)
        if all_complete and unsupported_valid else None
    )
    reviewers = sorted({
        _review(record)['reviewer'] for record in complete
        if isinstance(_review(record).get('reviewer'), str)
        and _review(record)['reviewer'].strip()
    })
    reviewed_at_values = [
        _review(record)['reviewed_at'] for record in complete
        if valid_review_time(_review(record).get('reviewed_at'))
    ]
    metadata_valid = bool(reviewers) and bool(reviewed_at_values)
    if (all_complete and unsupported == 0 and metadata_valid
            and not validation_issues):
        status = 'passed'
        blocked_reason = None
    elif all_complete and unsupported is not None and unsupported > 0:
        status = 'blocked'
        blocked_reason = 'unsupported_conclusions_present'
    elif all_complete:
        status = 'blocked'
        blocked_reason = 'review_record_invalid'
    else:
        status = 'blocked'
        blocked_reason = 'review_incomplete'
    return {
        'schema': REPORT_SCHEMA,
        'phase': '6.3-severe-review',
        'date': date.today().isoformat(),
        'status': status,
        'unsupported_conclusions': unsupported,
        'reviewed_answers': len(complete),
        'total_answers': len(source_records),
        'pending_answers': pending,
        'reviewer': ','.join(reviewers) if reviewers else None,
        'reviewed_at': max(reviewed_at_values) if reviewed_at_values else None,
        'blocked_reason': blocked_reason,
        'validation_issues': list(validation_issues or ()),
        'external_services': False,
        'data_egress': 'none',
        'notes': [
            'Only deterministic local evidence answers are included in the packet.',
            'A passing report requires every packet answer to have a complete expert review.',
            'The report excludes questions, answers, evidence text, API keys, and local paths.',
        ],
    }


def write_report(report, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    return target


def _display_path(path):
    target = Path(path)
    try:
        return target.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return target.name


def _print_summary(summary, report_path=None):
    print('phase6_severe_review=' + summary['status'])
    print('reviewed_answers=' + str(summary['reviewed_answers']))
    print('total_answers=' + str(summary['total_answers']))
    print('pending_answers=' + str(summary['pending_answers']))
    print('unsupported_conclusions=' + str(summary['unsupported_conclusions']))
    if summary.get('blocked_reason'):
        print('blocked_reason=' + str(summary['blocked_reason']))
    for issue in summary.get('validation_issues', ())[:20]:
        print('issue: ' + issue)
    if report_path:
        print('report_file=' + _display_path(report_path))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Phase 6.3 severe unsupported-conclusion expert review')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        '--prepare', nargs='?', const=str(_default_packet_path()),
        metavar='PATH')
    actions.add_argument(
        '--status', nargs='?', const=str(_default_status_packet_path()),
        metavar='PATH')
    actions.add_argument('--validate', metavar='PATH')
    actions.add_argument('--finalize', metavar='PATH')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS)
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--report', metavar='PATH')
    parser.add_argument('--require-pass', action='store_true')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= 10:
        parser.error('--limit must be between 1 and 10')
    if not 1 <= args.workers <= 8:
        parser.error('--workers must be between 1 and 8')

    cases = load_cases()
    if args.prepare:
        records, meta = prepare_packet(
            cases, limit=args.limit, workers=args.workers)
        try:
            target = write_records(
                records, args.prepare, replace=args.replace)
        except FileExistsError as exc:
            print('phase6_severe_review=packet_exists')
            print('issue: ' + str(exc))
            return 1
        summary = review_summary(records)
        summary['packet_file'] = _display_path(target)
        summary['prepared'] = meta
        if args.as_json:
            print(json.dumps(
                summary, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_summary(summary)
            print('packet_file=' + summary['packet_file'])
            print('prepared_answered=' + str(meta['answered']))
            print('prepared_skipped=' + str(meta['skipped']))
        return 0

    path = args.status or args.validate or args.finalize
    try:
        records = load_records(path)
    except (OSError, ValueError) as exc:
        print('phase6_severe_review=failed')
        print('issue: ' + str(exc))
        return 1
    issues = validate_records(records, cases)
    summary = review_summary(records, validation_issues=issues)

    if args.finalize and args.write:
        if issues:
            summary['status'] = 'blocked'
        else:
            target = write_report(
                summary, args.report or _default_report_path())
            summary['report_file'] = _display_path(target)

    if args.as_json:
        print(json.dumps(
            summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(summary, summary.get('report_file'))

    if args.validate:
        return 0 if not issues else 1
    if args.finalize:
        return 0 if summary['status'] == 'passed' else 1
    if args.require_pass and summary['status'] != 'passed':
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
