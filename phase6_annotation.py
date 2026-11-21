'''Versioned expert annotations for the Phase 6.3 evaluation set.

The seed cases in ``contracts/phase6/eval/cases.jsonl`` remain immutable. Expert
labels live in a separate JSONL file so retrieval candidates, generated
answers, and review notes cannot silently rewrite the evaluation baseline.
Only records whose annotation status is ``complete`` are merged into an
evaluation run.
'''
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile

from phase6_contract import (
    CASE_SCHEMA,
    CASES_PATH,
    EVAL_ROOT,
    load_cases,
    validate_eval_cases,
    validate_instance,
)


ANNOTATION_SCHEMA = 'pcb-phase6-annotation-v1'
ANNOTATIONS_PATH = EVAL_ROOT / 'annotations.jsonl'
CANDIDATES_PATH = (
    Path(__file__).resolve().parent / 'data' / 'phase6_eval_candidates'
    / 'candidates.jsonl'
)
FINGERPRINT_ALGORITHM = 'sha256-canonical-json-v1'
RECORD_KEYS = {
    'schema', 'case_id', 'case_fingerprint', 'expected', 'annotation',
}
FINGERPRINT_FIELDS = (
    'case_id', 'category', 'language', 'question', 'provenance', 'sensitivity',
)


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )


def case_fingerprint(case):
    '''Identify the immutable seed fields a review was based on.'''
    payload = {name: case.get(name) for name in FINGERPRINT_FIELDS}
    return hashlib.sha256(
        _canonical_json(payload).encode('utf-8')).hexdigest()


def template_record(case):
    return {
        'schema': ANNOTATION_SCHEMA,
        'case_id': case['case_id'],
        'case_fingerprint': case_fingerprint(case),
        'expected': copy.deepcopy(case['expected']),
        'annotation': {
            'status': 'pending',
            'reviewer': None,
            'reviewed_at': None,
        },
    }


def build_template(cases=None):
    source_cases = load_cases() if cases is None else list(cases)
    return [template_record(case) for case in source_cases]


def load_records(path=ANNOTATIONS_PATH):
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
                    str(source) + ':' + str(line_number) + ': record must be an object')
            records.append(value)
    return records


def _parse_reviewed_at(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_complete_expectations(record, base):
    issues = []
    expected = record.get('expected', {})
    case_id = record.get('case_id') or '?'
    prefix = '$.' + str(case_id)
    annotation = record.get('annotation', {})
    if annotation.get('status') != 'complete':
        return issues
    if not annotation.get('reviewer'):
        issues.append(prefix + '.annotation.reviewer is required when complete')
    if not _parse_reviewed_at(annotation.get('reviewed_at')):
        issues.append(
            prefix + '.annotation.reviewed_at must be ISO 8601 with timezone')
    if not str(expected.get('notes') or '').strip():
        issues.append(prefix + '.expected.notes is required when complete')
    if expected.get('status') == 'answered':
        if expected.get('should_answer') is not True:
            issues.append(prefix + '.expected.should_answer must be true')
        for name in ('required_fields', 'relevant_doc_ids',
                     'required_evidence_ids', 'acceptable_points'):
            if not expected.get(name):
                issues.append(prefix + '.expected.' + name + ' is required')
        if not expected.get('forbidden_claims'):
            issues.append(prefix + '.expected.forbidden_claims is required')
    else:
        if expected.get('should_answer') is not False:
            issues.append(prefix + '.expected.should_answer must be false')
        if expected.get('required_fields'):
            issues.append(prefix + '.expected.required_fields must be empty')
        if expected.get('relevant_doc_ids'):
            issues.append(prefix + '.expected.relevant_doc_ids must be empty')
        if expected.get('required_evidence_ids'):
            issues.append(prefix + '.expected.required_evidence_ids must be empty')
    if base.get('category') == 'prompt_injection':
        if expected.get('status') != 'policy_blocked':
            issues.append(prefix + ': prompt-injection gold must stay policy_blocked')
    return issues


def validate_records(records, cases=None, *, require_complete=False):
    '''Validate records against the current seed and return stable messages.'''
    source_cases = load_cases() if cases is None else list(cases)
    by_id = {case['case_id']: case for case in source_cases}
    issues = []
    seen = set()
    complete_ids = set()
    for index, record in enumerate(records):
        base_path = '$.annotations[' + str(index) + ']'
        if not isinstance(record, dict):
            issues.append(base_path + ' must be an object')
            continue
        if set(record) != RECORD_KEYS:
            issues.append(base_path + ': unexpected or missing keys')
        if record.get('schema') != ANNOTATION_SCHEMA:
            issues.append(base_path + '.schema')
        case_id = record.get('case_id')
        if case_id in seen:
            issues.append(base_path + ': duplicate case_id')
        seen.add(case_id)
        base = by_id.get(case_id)
        if base is None:
            issues.append(base_path + ': unknown case_id')
            continue
        if record.get('case_fingerprint') != case_fingerprint(base):
            issues.append(base_path + '.case_fingerprint is stale')
        merged = copy.deepcopy(base)
        if isinstance(record.get('expected'), dict):
            merged['expected'] = copy.deepcopy(record['expected'])
        if isinstance(record.get('annotation'), dict):
            merged['annotation'] = copy.deepcopy(record['annotation'])
        issues.extend(validate_instance(merged, CASE_SCHEMA, base_path))
        issues.extend(_validate_complete_expectations(record, base))
        if record.get('annotation', {}).get('status') == 'complete':
            complete_ids.add(case_id)
    if require_complete:
        missing = sorted(set(by_id) - complete_ids)
        if missing:
            issues.append(
                '$.annotations: incomplete gold for ' + str(len(missing))
                + ' case(s): ' + ','.join(missing[:10])
                + ('' if len(missing) <= 10 else ',...'))
    merged_cases = merge_annotations(source_cases, records)
    issues.extend(validate_eval_cases(merged_cases, require_pending=False))
    return issues


def merge_annotations(cases, records):
    '''Merge complete expert records without changing any seed core fields.'''
    completed = {
        record.get('case_id'): record
        for record in records
        if record.get('annotation', {}).get('status') == 'complete'
    }
    merged = []
    for case in cases:
        value = copy.deepcopy(case)
        record = completed.get(case['case_id'])
        if record is not None:
            value['expected'] = copy.deepcopy(record['expected'])
            value['annotation'] = copy.deepcopy(record['annotation'])
        merged.append(value)
    return merged


def load_annotated_cases(path=ANNOTATIONS_PATH, cases_path=CASES_PATH):
    base_cases = load_cases(cases_path)
    source = Path(path)
    if not source.is_file():
        return base_cases, {'path': str(source), 'exists': False, 'records': 0}
    records = load_records(source)
    issues = validate_records(records, base_cases)
    if issues:
        raise ValueError('invalid annotation file: ' + '; '.join(issues[:5]))
    return merge_annotations(base_cases, records), {
        'path': str(source),
        'exists': True,
        'records': len(records),
        'complete': sum(
            1 for record in records
            if record.get('annotation', {}).get('status') == 'complete'),
    }


def annotation_summary(records, cases=None):
    source_cases = load_cases() if cases is None else list(cases)
    issues = validate_records(records, source_cases)
    complete = {
        record.get('case_id') for record in records
        if record.get('annotation', {}).get('status') == 'complete'
    }
    reviewer_values = {
        record.get('annotation', {}).get('reviewer')
        for record in records
        if record.get('annotation', {}).get('status') == 'complete'
        and record.get('annotation', {}).get('reviewer')
    }
    return {
        'schema': ANNOTATION_SCHEMA,
        'status': 'passed' if not issues else 'failed',
        'cases': len(source_cases),
        'records': len(records),
        'complete': len(complete),
        'pending': len(source_cases) - len(complete),
        'reviewers': sorted(reviewer_values),
        'issues': issues,
        'external_calls': 0,
    }


def write_records(records, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True)
        for record in records
    ]
    target.write_text(
        '\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
    return target


def publish_records(records, *, path=ANNOTATIONS_PATH, replace=False):
    target = Path(path)
    if target.exists() and not replace:
        raise FileExistsError(
            str(target) + ' already exists; pass --replace to publish a new revision')
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = '\n'.join(
        json.dumps(record, ensure_ascii=False, sort_keys=True)
        for record in records
    ) + ('\n' if records else '')
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


def _load_candidate_index(path):
    source = Path(path)
    if not source.is_file():
        return {}
    values = load_records(source)
    return {
        value.get('case_id'): value for value in values
        if isinstance(value, dict) and value.get('case_id')
    }


def _show_packet(case_id, *, candidates_path=CANDIDATES_PATH, cases=None):
    source_cases = load_cases() if cases is None else list(cases)
    case = next((item for item in source_cases if item['case_id'] == case_id), None)
    if case is None:
        raise KeyError('unknown case_id: ' + str(case_id))
    candidate = _load_candidate_index(candidates_path).get(case_id)
    return {
        'case': {
            'case_id': case['case_id'],
            'category': case['category'],
            'question': case['question'],
        },
        'annotation_template': template_record(case),
        'candidate_evidence': candidate,
        'review_rules': {
            'candidate_is_not_gold': True,
            'complete_requires_reviewer': True,
            'complete_requires_timezone': True,
            'answered_requires_gold_ids_and_points': True,
            'non_answered_must_not_carry_gold_ids': True,
        },
    }


def _summary_text(summary):
    print('phase6_annotations=' + summary['status'])
    print('cases=' + str(summary['cases']))
    print('records=' + str(summary['records']))
    print('complete=' + str(summary['complete']))
    print('pending=' + str(summary['pending']))
    print('reviewers=' + ','.join(summary['reviewers']))
    for issue in summary['issues'][:20]:
        print('issue: ' + issue)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Phase 6.3 versioned expert annotation workflow')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--status', nargs='?', const=str(ANNOTATIONS_PATH),
                         metavar='PATH')
    actions.add_argument('--template', metavar='PATH')
    actions.add_argument('--validate', metavar='PATH')
    actions.add_argument('--publish', metavar='PATH')
    actions.add_argument('--show', metavar='CASE_ID')
    parser.add_argument('--candidates', default=str(CANDIDATES_PATH), metavar='PATH')
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--require-complete', action='store_true')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.template:
        target = Path(args.template)
        if target.exists() and not args.replace:
            parser.error('template already exists; pass --replace to overwrite')
        write_records(build_template(cases), target)
        print('phase6_annotations=template_written')
        print('records=' + str(len(cases)))
        return 0

    if args.show:
        try:
            packet = _show_packet(
                args.show, candidates_path=args.candidates, cases=cases)
        except (KeyError, OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.publish:
        source = Path(args.publish)
        if not source.is_file():
            parser.error('annotation draft not found: ' + str(source))
        try:
            records = load_records(source)
        except (OSError, ValueError) as exc:
            print('phase6_annotations=failed')
            print('issue: ' + str(exc))
            return 1
        issues = validate_records(
            records, cases, require_complete=args.require_complete)
        if issues:
            summary = annotation_summary(records, cases)
            if args.as_json:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                _summary_text(summary)
            return 1
        try:
            target = publish_records(
                records, path=ANNOTATIONS_PATH, replace=args.replace)
        except FileExistsError as exc:
            print('phase6_annotations=publish_blocked')
            print('issue: ' + str(exc))
            return 1
        print('phase6_annotations=published')
        print('target=' + str(target))
        print('records=' + str(len(records)))
        return 0

    path = args.status or (args.validate if args.validate else None)
    if path is None:
        path = str(ANNOTATIONS_PATH)
    source = Path(path)
    if not source.is_file():
        summary = {
            'schema': ANNOTATION_SCHEMA,
            'status': 'not_started',
            'cases': len(cases),
            'records': 0,
            'complete': 0,
            'pending': len(cases),
            'reviewers': [],
            'issues': [],
            'external_calls': 0,
        }
        if args.require_complete:
            summary['status'] = 'failed'
            summary['issues'] = ['annotations file is missing: ' + str(source)]
    else:
        try:
            records = load_records(source)
        except (OSError, ValueError) as exc:
            summary = {
                'schema': ANNOTATION_SCHEMA,
                'status': 'failed',
                'cases': len(cases),
                'records': 0,
                'complete': 0,
                'pending': len(cases),
                'reviewers': [],
                'issues': [str(exc)],
                'external_calls': 0,
            }
        else:
            summary = annotation_summary(records, cases)
            if args.require_complete:
                issues = validate_records(records, cases, require_complete=True)
                summary['issues'] = issues
                summary['status'] = 'passed' if not issues else 'failed'
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _summary_text(summary)
    if args.validate or args.require_complete:
        return 0 if summary['status'] == 'passed' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
