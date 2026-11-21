'''Structured engineering review for the five internal-pilot questions.

The tool prepares a private packet from deterministic local evidence answers and
lets an engineer review every citation in context. It never fabricates reviewer
identities, never marks a review complete, and never turns an incomplete packet
into a passing report.
'''
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
import json
from pathlib import Path
import re

from pcb_assistant.evaluation.annotation import case_fingerprint
from pcb_assistant.evaluation.review import load_records, prepare_packet, write_records
import pcb_assistant.evaluation.review as phase6_severe_review


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / 'docs'
PRIVATE_ROOT = ROOT / '.private' / 'internal_pilot_review'
DEFAULT_CASES_PATH = ROOT / 'cases/internal_pilot.json'
CASES_SCHEMA = 'pcb-internal-pilot-cases-v1'
RECORD_SCHEMA = 'pcb-internal-pilot-review-record-v1'
REPORT_SCHEMA = 'pcb-internal-pilot-review-v1'
DEFAULT_LIMIT = 8
DEFAULT_WORKERS = 4
CITATION_SUPPORT = {'direct', 'partial', 'unsupported', 'not_applicable'}
RECORD_KEYS = {
    'schema', 'case_id', 'case_fingerprint', 'answer_fingerprint',
    'category', 'question', 'answer', 'citations', 'review',
}
REVIEW_KEYS = {
    'status', 'reviewer', 'reviewed_at', 'citation_reviews',
    'unsupported_conclusions', 'missing_trusted_references',
    'clarification_adequate', 'notes',
}
CITATION_REVIEW_KEYS = {'excerpt_id', 'context_checked', 'support', 'notes'}
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_EXCERPT = re.compile(r'^EXC_[0-9a-f]{24}$')
_CASE_ID = re.compile(r'^PILOT_[0-9A-Z_]+$')


def _default_packet_path():
    return PRIVATE_ROOT / date.today().strftime('%Y%m%d') / 'packet.jsonl'


def _default_status_packet_path():
    current = _default_packet_path()
    if current.is_file():
        return current
    try:
        directories = list(PRIVATE_ROOT.iterdir())
    except OSError:
        return current
    candidates = []
    for directory in directories:
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
        'INTERNAL_PILOT_REVIEW_' + date.today().strftime('%Y%m%d') + '.json')


def _display_path(path):
    target = Path(path)
    try:
        return target.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return target.name


def _parse_reviewed_at(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def load_cases(path=DEFAULT_CASES_PATH):
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(str(source) + ': invalid cases file') from exc
    if not isinstance(value, dict) or value.get('schema') != CASES_SCHEMA:
        raise ValueError(str(source) + ': invalid cases schema')
    rows = value.get('cases')
    if not isinstance(rows, list) or not rows:
        raise ValueError(str(source) + ': cases must be a non-empty array')
    cases = []
    seen = set()
    for index, row in enumerate(rows):
        prefix = '$.cases[' + str(index) + ']'
        if not isinstance(row, dict):
            raise ValueError(prefix + ' must be an object')
        case_id = row.get('case_id')
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise ValueError(prefix + '.case_id')
        if case_id in seen:
            raise ValueError(prefix + ': duplicate case_id')
        seen.add(case_id)
        question = row.get('question')
        if not isinstance(question, str) or not question.strip() or len(question) > 1000:
            raise ValueError(prefix + '.question')
        category = row.get('category')
        if not isinstance(category, str) or not category.strip():
            raise ValueError(prefix + '.category')
        language = row.get('language')
        if not isinstance(language, str) or not language.strip():
            raise ValueError(prefix + '.language')
        provenance = row.get('provenance')
        if not isinstance(provenance, dict):
            raise ValueError(prefix + '.provenance')
        sensitivity = row.get('sensitivity')
        if not isinstance(sensitivity, str) or not sensitivity.strip():
            raise ValueError(prefix + '.sensitivity')
        cases.append({
            'case_id': case_id,
            'category': category,
            'language': language,
            'question': question,
            'provenance': provenance,
            'sensitivity': sensitivity,
        })
    return cases


def pending_review(citations):
    return {
        'status': 'pending',
        'reviewer': None,
        'reviewed_at': None,
        'citation_reviews': [
            {
                'excerpt_id': citation['excerpt_id'],
                'context_checked': None,
                'support': None,
                'notes': '',
            }
            for citation in citations
        ],
        'unsupported_conclusions': None,
        'missing_trusted_references': None,
        'clarification_adequate': None,
        'notes': '',
    }


def answer_case(case, *, limit=DEFAULT_LIMIT, catalog_directory=None):
    base = phase6_severe_review.run_case(
        case, limit=limit, catalog_directory=catalog_directory)
    if base is None:
        return None
    citations = base.get('citations')
    if not isinstance(citations, list):
        citations = []
    return {
        'schema': RECORD_SCHEMA,
        'case_id': base['case_id'],
        'case_fingerprint': base['case_fingerprint'],
        'answer_fingerprint': base['answer_fingerprint'],
        'category': base.get('category'),
        'question': base['question'],
        'answer': base['answer'],
        'citations': citations,
        'review': pending_review(citations),
    }


def prepare_review_packet(cases, *, limit=DEFAULT_LIMIT, workers=DEFAULT_WORKERS,
                          answer_runner=None, catalog_directory=None):
    runner = answer_case if answer_runner is None else answer_runner
    return prepare_packet(
        cases,
        limit=limit,
        workers=workers,
        answer_runner=runner,
        catalog_directory=catalog_directory,
    )


def _validate_citation_reviews(review, citations, prefix):
    issues = []
    rows = review.get('citation_reviews')
    if not isinstance(rows, list):
        return [prefix + '.citation_reviews must be an array']
    expected_ids = [citation.get('excerpt_id') for citation in citations]
    actual_ids = [
        row.get('excerpt_id') if isinstance(row, dict) else None
        for row in rows
    ]
    if actual_ids != expected_ids:
        issues.append(prefix + '.citation_reviews must match citations in order')
    pending = review.get('status') == 'pending'
    for index, row in enumerate(rows):
        row_prefix = prefix + '.citation_reviews[' + str(index) + ']'
        if not isinstance(row, dict):
            issues.append(row_prefix + ' must be an object')
            continue
        if set(row) != CITATION_REVIEW_KEYS:
            issues.append(row_prefix + ': unexpected or missing keys')
        excerpt_id = row.get('excerpt_id')
        if not isinstance(excerpt_id, str) or not _EXCERPT.fullmatch(excerpt_id):
            issues.append(row_prefix + '.excerpt_id')
        if not isinstance(row.get('notes'), str):
            issues.append(row_prefix + '.notes must be a string')
        if pending:
            if row.get('context_checked') is not None:
                issues.append(row_prefix + '.context_checked must be null while pending')
            if row.get('support') is not None:
                issues.append(row_prefix + '.support must be null while pending')
            continue
        if row.get('context_checked') is not True:
            issues.append(row_prefix + '.context_checked must be true when complete')
        support = row.get('support')
        if support not in CITATION_SUPPORT:
            issues.append(row_prefix + '.support is invalid')
        elif support != 'direct' and not str(row.get('notes') or '').strip():
            issues.append(row_prefix + '.notes are required unless support is direct')
    return issues


def _validate_review(review, citations, prefix):
    if not isinstance(review, dict):
        return [prefix + ' must be an object']
    issues = []
    if set(review) != REVIEW_KEYS:
        issues.append(prefix + ': unexpected or missing keys')
    status = review.get('status')
    if status not in ('pending', 'complete'):
        issues.append(prefix + '.status must be pending or complete')
        return issues
    if not isinstance(review.get('notes', ''), str):
        issues.append(prefix + '.notes must be a string')
    if status == 'pending':
        for name in (
                'reviewer', 'reviewed_at', 'unsupported_conclusions',
                'missing_trusted_references', 'clarification_adequate'):
            if review.get(name) is not None:
                issues.append(prefix + '.' + name + ' must be null while pending')
    else:
        if not str(review.get('reviewer') or '').strip():
            issues.append(prefix + '.reviewer is required when complete')
        if not _parse_reviewed_at(review.get('reviewed_at')):
            issues.append(prefix + '.reviewed_at must be ISO 8601 with timezone')
        unsupported = review.get('unsupported_conclusions')
        if (isinstance(unsupported, bool)
                or not isinstance(unsupported, int) or unsupported < 0):
            issues.append(prefix + '.unsupported_conclusions must be non-negative')
        if not isinstance(review.get('missing_trusted_references'), bool):
            issues.append(prefix + '.missing_trusted_references must be boolean')
        if not isinstance(review.get('clarification_adequate'), bool):
            issues.append(prefix + '.clarification_adequate must be boolean')
        if review.get('missing_trusted_references') is True and not str(
                review.get('notes') or '').strip():
            issues.append(prefix + '.notes are required when trusted references are missing')
    issues.extend(_validate_citation_reviews(review, citations, prefix))
    return issues


def validate_records(records, cases=None):
    source_cases = None if cases is None else list(cases)
    by_id = {case['case_id']: case for case in source_cases} if source_cases else None
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
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            issues.append(prefix + '.case_id')
        elif case_id in seen:
            issues.append(prefix + ': duplicate case_id')
        else:
            seen.add(case_id)
        for name in ('case_fingerprint', 'answer_fingerprint'):
            if not isinstance(record.get(name), str) or not _HEX64.fullmatch(record[name]):
                issues.append(prefix + '.' + name + ' must be sha256 hex')
        if not isinstance(record.get('question'), str) or not record['question'].strip():
            issues.append(prefix + '.question is required')
        if not isinstance(record.get('answer'), str) or not record['answer'].strip():
            issues.append(prefix + '.answer is required')
        citations = record.get('citations')
        if not isinstance(citations, list) or not citations:
            issues.append(prefix + '.citations must be a non-empty array')
            citations = citations if isinstance(citations, list) else []
        citation_ids = set()
        for citation_index, citation in enumerate(citations):
            citation_path = prefix + '.citations[' + str(citation_index) + ']'
            if not isinstance(citation, dict):
                issues.append(citation_path + ' must be an object')
                continue
            excerpt_id = citation.get('excerpt_id')
            if not isinstance(excerpt_id, str) or not _EXCERPT.fullmatch(excerpt_id):
                issues.append(citation_path + '.excerpt_id')
            elif excerpt_id in citation_ids:
                issues.append(citation_path + ': duplicate excerpt_id')
            else:
                citation_ids.add(excerpt_id)
        if by_id is not None:
            base = by_id.get(case_id)
            if base is None:
                issues.append(prefix + ': unknown case_id')
            elif record.get('case_fingerprint') != case_fingerprint(base):
                issues.append(prefix + '.case_fingerprint is stale')
        issues.extend(_validate_review(record.get('review'), citations, prefix + '.review'))
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
    support_counts = Counter()
    citation_total = 0
    citation_checked = 0
    unsupported_citations = 0
    unsupported_conclusions = 0
    missing_trusted = 0
    clarification_inadequate = 0
    reviewers = set()
    reviewed_at_values = []
    for record in complete:
        review = _review(record)
        reviewers.add(review.get('reviewer'))
        if _parse_reviewed_at(review.get('reviewed_at')):
            reviewed_at_values.append(review.get('reviewed_at'))
        citation_total += len(record.get('citations') or ())
        for row in review.get('citation_reviews') or ():
            if not isinstance(row, dict):
                continue
            if row.get('context_checked') is True:
                citation_checked += 1
            support = row.get('support')
            if support in CITATION_SUPPORT:
                support_counts[support] += 1
                if support in {'unsupported', 'not_applicable'}:
                    unsupported_citations += 1
        value = review.get('unsupported_conclusions')
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            unsupported_conclusions += value
        if review.get('missing_trusted_references') is True:
            missing_trusted += 1
        if review.get('clarification_adequate') is False:
            clarification_inadequate += 1
    issues = list(validation_issues or ())
    quality_blocked_reasons = []
    if not all_complete:
        quality_blocked_reasons.append('review_incomplete')
    elif issues:
        quality_blocked_reasons.append('review_record_invalid')
    if all_complete and unsupported_conclusions > 0:
        quality_blocked_reasons.append('unsupported_conclusions_present')
    if all_complete and unsupported_citations > 0:
        quality_blocked_reasons.append('unsupported_citations_present')
    if all_complete and clarification_inadequate > 0:
        quality_blocked_reasons.append('clarification_inadequate')
    if all_complete:
        review_state = 'complete'
        status = ('passed' if not quality_blocked_reasons
                  else 'reviewed_with_findings')
    elif complete:
        review_state = 'in_progress'
        status = 'in_progress'
    else:
        review_state = 'awaiting_optional_review'
        status = 'awaiting_optional_review'
    quality_claim_ready = all_complete and not quality_blocked_reasons
    reviewers = sorted(
        reviewer for reviewer in reviewers
        if isinstance(reviewer, str) and reviewer.strip())
    return {
        'schema': REPORT_SCHEMA,
        'phase': 'internal-pilot-review',
        'date': date.today().isoformat(),
        'status': status,
        'review_state': review_state,
        'usage_blocked': False,
        'quality_claim_ready': quality_claim_ready,
        'reviewed_cases': len(complete),
        'total_cases': len(source_records),
        'pending_cases': pending,
        'citation_reviews_checked': citation_checked if all_complete else None,
        'citation_reviews_total': citation_total if all_complete else None,
        'support_counts': dict(sorted(support_counts.items())) if all_complete else None,
        'unsupported_citations': unsupported_citations if all_complete else None,
        'unsupported_conclusions': unsupported_conclusions if all_complete else None,
        'missing_trusted_references': missing_trusted if all_complete else None,
        'clarification_inadequate': clarification_inadequate if all_complete else None,
        'reviewer': ','.join(reviewers) if reviewers else None,
        'reviewed_at': max(reviewed_at_values) if reviewed_at_values else None,
        'quality_blocked_reasons': quality_blocked_reasons,
        'validation_issues': issues,
        'external_services': False,
        'data_egress': 'none',
        'notes': [
            'Only deterministic local evidence answers are included in the packet.',
            'Pending review is an optional retrospective audit and does not block internal use.',
            'A passing report requires every citation to be checked in context by an engineer.',
            'Unsupported citations block the quality claim, not continued internal use with feedback.',
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


def _print_summary(summary, report_path=None):
    print('internal_pilot_review=' + summary['status'])
    print('reviewed_cases=' + str(summary['reviewed_cases']))
    print('total_cases=' + str(summary['total_cases']))
    print('pending_cases=' + str(summary['pending_cases']))
    print('usage_blocked=' + str(summary['usage_blocked']).lower())
    print('quality_claim_ready=' + str(summary['quality_claim_ready']).lower())
    if summary.get('unsupported_conclusions') is not None:
        print('unsupported_conclusions=' + str(summary['unsupported_conclusions']))
    if summary.get('quality_blocked_reasons'):
        print('quality_blocked_reasons=' + ','.join(
            summary['quality_blocked_reasons']))
    for issue in summary.get('validation_issues', ())[:20]:
        print('issue: ' + issue)
    if report_path:
        print('report_file=' + _display_path(report_path))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Engineering review for the five internal-pilot questions')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--prepare', nargs='?', const=str(_default_packet_path()),
                         metavar='PATH')
    actions.add_argument('--status', nargs='?', const=str(_default_status_packet_path()),
                         metavar='PATH')
    actions.add_argument('--validate', metavar='PATH')
    actions.add_argument('--finalize', metavar='PATH')
    parser.add_argument('--cases', default=str(DEFAULT_CASES_PATH))
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
    try:
        cases = load_cases(args.cases)
    except ValueError as exc:
        print('internal_pilot_review=failed')
        print('issue: ' + str(exc))
        return 1
    if args.prepare:
        records, meta = prepare_review_packet(
            cases, limit=args.limit, workers=args.workers)
        try:
            target = write_records(records, args.prepare, replace=args.replace)
        except FileExistsError as exc:
            print('internal_pilot_review=packet_exists')
            print('issue: ' + str(exc))
            return 1
        summary = review_summary(records)
        summary['packet_file'] = _display_path(target)
        summary['prepared'] = meta
        if args.as_json:
            print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
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
        print('internal_pilot_review=failed')
        print('issue: ' + str(exc))
        return 1
    issues = validate_records(records, cases)
    summary = review_summary(records, validation_issues=issues)
    if args.finalize and args.write:
        if issues:
            summary['status'] = 'blocked'
        else:
            target = write_report(summary, args.report or _default_report_path())
            summary['report_file'] = _display_path(target)
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
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
