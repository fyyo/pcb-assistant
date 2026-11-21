'''Local-only proposed evidence candidates for Phase 6 evaluation annotation.

This module never writes ``cases.jsonl`` and never marks a candidate as gold.
It only runs deterministic, extractive Catalog retrieval so a reviewer has a
bounded starting point. Prompt-injection cases are deliberately not retrieved.
'''
from __future__ import annotations

from pcb_assistant.evaluation.common import ordered_unique

import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
from pathlib import Path
import re

import pcb_assistant.catalog.agent as catalog_agent
from pcb_assistant.interfaces.contract import DOC_ID_PATTERN, EXCERPT_ID_PATTERN, load_cases
from pcb_assistant.interfaces.mcp import sanitize_payload


from pcb_assistant.paths import ROOT
CANDIDATE_ROOT = ROOT / 'data' / 'phase6_eval_candidates'
CANDIDATES_PATH = CANDIDATE_ROOT / 'candidates.jsonl'
SCHEMA = 'pcb-eval-candidates-v1'
MAX_EXCERPT_CHARS = 360

_GOLD_KEYS = {'relevant_doc_ids', 'required_evidence_ids'}




def _has_forbidden_key(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _GOLD_KEYS or key == 'source_path' or key.endswith('_path'):
                return True
            if _has_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


def _base_record(case):
    return {
        'schema': SCHEMA,
        'case_id': case['case_id'],
        'category': case['category'],
        'question': case['question'],
        'expected_status': case['expected']['status'],
        'retrieval_status': 'not_retrieved',
        'retrieval_query': None,
        'retrieval_term_groups': [],
        'matched_terms': [],
        'candidate_doc_ids': [],
        'candidate_excerpt_ids': [],
        'candidate_sources': [],
        'candidate_excerpts': [],
        'field_coverage': [],
        'missing_information': [],
        'candidate_quality': 'not_applicable',
        'quality_basis': 'query_term_coverage_and_field_patterns_only',
        'quality_flags': ['expert_confirmation_required', 'semantic_relevance_not_verified'],
        'evidence_relevance': None,
        'retrieval_fingerprint': None,
        'annotation_status': 'proposed',
        'annotation': {'reviewer': None, 'reviewed_at': None},
        'generation': {'method': 'deterministic_extractive_retrieval', 'external_calls': 0},
    }


def build_candidate(case, *, limit=3):
    record = _base_record(case)
    if case['category'] == 'prompt_injection':
        record['quality_flags'].append('policy_blocked_not_retrieved')
        return record
    query = case['question'].strip()
    try:
        result = catalog_agent.ask_agent(question=query, limit=limit, max_sources=limit)
    except Exception:
        record['retrieval_status'] = 'retrieval_error'
        record['candidate_quality'] = 'none'
        record['quality_flags'].append('retrieval_error')
        return record

    groups = result.get('evidence_groups', [])
    sources = []
    excerpts = []
    for group in groups:
        sources.append({
            'doc_id': group.get('doc_id'),
            'type': group.get('type'),
            'title': group.get('title'),
            'source_type': group.get('source_type'),
            'source_version': group.get('source_version'),
            'content_status': group.get('content_status'),
            'evidence_status': group.get('evidence_status'),
            'independently_verified': group.get('independently_verified'),
        })
        for excerpt in group.get('excerpts', ()):
            excerpts.append({
                'excerpt_id': excerpt.get('excerpt_id'),
                'doc_id': group.get('doc_id'),
                'type': group.get('type'),
                'title': group.get('title'),
                'text': excerpt.get('text', '')[:MAX_EXCERPT_CHARS],
                'matched_terms': list(excerpt.get('matched_terms') or ()),
                'line_start': excerpt.get('line_start'),
                'line_end': excerpt.get('line_end'),
                'sha256': excerpt.get('sha256'),
            })

    relevance = result.get('evidence_relevance') or {}
    coverage = result.get('evidence_field_coverage') or {}
    retrieval = result.get('retrieval') or {}
    term_groups = (retrieval.get('ranking') or {}).get('term_groups') or []
    doc_ids = ordered_unique(source.get('doc_id') for source in sources)
    excerpt_ids = ordered_unique(excerpt.get('excerpt_id') for excerpt in excerpts)
    matched_terms = ordered_unique(term for excerpt in excerpts for term in excerpt['matched_terms'])
    covered_fields = list(coverage.get('covered') or ())

    flags = ['expert_confirmation_required', 'semantic_relevance_not_verified']
    if result.get('status') != 'evidence_found':
        flags.append('no_evidence_found')
    if not excerpts:
        flags.append('no_bounded_excerpts')
    if relevance.get('label') != 'strong':
        flags.append('query_term_coverage_not_strong')
    if len(covered_fields) < 3:
        flags.append('limited_field_coverage')
    if len(doc_ids) < 2:
        flags.append('single_candidate_source')
    if len(term_groups) > 6:
        flags.append('broad_query_terms')
    if any(source.get('content_status') != 'ready' for source in sources):
        flags.append('metadata_only_candidates_present')
    if case['expected']['status'] == 'no_evidence':
        flags.append('case_expects_insufficient_evidence')
    if case['expected']['status'] == 'needs_clarification':
        flags.append('case_expects_clarification')

    if not excerpts:
        quality = 'none'
    elif relevance.get('label') == 'strong' and len(covered_fields) >= 3 and len(doc_ids) >= 2:
        quality = 'high'
    elif excerpts and (relevance.get('label') in ('strong', 'moderate') or covered_fields):
        quality = 'medium'
    else:
        quality = 'low'

    record.update({
        'retrieval_status': result.get('status'),
        'retrieval_query': result.get('retrieval_query'),
        'retrieval_term_groups': term_groups,
        'matched_terms': matched_terms,
        'candidate_doc_ids': doc_ids,
        'candidate_excerpt_ids': excerpt_ids,
        'candidate_sources': sources,
        'candidate_excerpts': excerpts,
        'field_coverage': covered_fields,
        'missing_information': list(result.get('missing_information') or ()),
        'candidate_quality': quality,
        'quality_flags': flags,
        'evidence_relevance': relevance,
        'retrieval_fingerprint': result.get('result_fingerprint'),
    })
    return sanitize_payload(record)


def build_candidates(cases=None, *, limit=3, workers=4):
    source_cases = load_cases() if cases is None else list(cases)
    worker_count = max(1, min(8, int(workers)))
    if worker_count == 1:
        return [build_candidate(case, limit=limit) for case in source_cases]
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        return list(pool.map(lambda case: build_candidate(case, limit=limit), source_cases))


def write_candidates(candidates, path=CANDIDATES_PATH):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in candidates]
    target.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
    return target


def load_candidates(path=CANDIDATES_PATH):
    records = []
    with Path(path).open('r', encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            text = line.strip()
            if not text:
                continue
            try:
                records.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(str(path) + ':' + str(line_number) + ': ' + str(exc)) from exc
    return records


def validate_candidates(candidates, cases=None):
    issues = []
    source_cases = load_cases() if cases is None else list(cases)
    expected = {case['case_id']: case for case in source_cases}
    ids = [item.get('case_id') for item in candidates]
    if len(ids) != len(set(ids)):
        issues.append('$.candidates: duplicate case_id')
    if set(ids) != set(expected):
        issues.append('$.candidates: case set does not match cases.jsonl')
    for index, item in enumerate(candidates):
        base = '$.candidates[' + str(index) + ']'
        case = expected.get(item.get('case_id'))
        if item.get('schema') != SCHEMA:
            issues.append(base + '.schema')
        if case is None:
            continue
        if item.get('category') != case['category']:
            issues.append(base + '.category')
        if item.get('question') != case['question']:
            issues.append(base + '.question')
        if item.get('annotation_status') != 'proposed':
            issues.append(base + '.annotation_status must be proposed')
        if item.get('quality_basis') != 'query_term_coverage_and_field_patterns_only':
            issues.append(base + '.quality_basis must remain retrieval-signal-only')
        flags = set(item.get('quality_flags') or ())
        for required_flag in ('expert_confirmation_required', 'semantic_relevance_not_verified'):
            if required_flag not in flags:
                issues.append(base + ': missing quality flag ' + required_flag)
        if _has_forbidden_key(item):
            issues.append(base + ': must not contain gold IDs or local paths')
        for doc_id in item.get('candidate_doc_ids', ()):
            if re.fullmatch(DOC_ID_PATTERN, doc_id) is None:
                issues.append(base + ': invalid candidate doc_id')
        for excerpt_id in item.get('candidate_excerpt_ids', ()):
            if re.fullmatch(EXCERPT_ID_PATTERN, excerpt_id) is None:
                issues.append(base + ': invalid candidate excerpt_id')
        for excerpt in item.get('candidate_excerpts', ()):
            if len(excerpt.get('text', '')) > MAX_EXCERPT_CHARS:
                issues.append(base + ': excerpt exceeds bounded length')
        if case['category'] == 'prompt_injection':
            if item.get('candidate_doc_ids') or item.get('candidate_excerpt_ids'):
                issues.append(base + ': prompt injection must not be retrieved')
            if item.get('retrieval_status') != 'not_retrieved':
                issues.append(base + ': prompt injection retrieval_status')
    return issues


def validate_candidate_file(path=CANDIDATES_PATH):
    target = Path(path)
    if not target.is_file():
        return ['missing candidate file: ' + str(target)]
    try:
        candidates = load_candidates(target)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    return validate_candidates(candidates)


def _summary(candidates):
    return {
        'candidates': len(candidates),
        'categories': dict(Counter(item.get('category') for item in candidates)),
        'annotations': dict(Counter(item.get('annotation_status') for item in candidates)),
        'quality': dict(Counter(item.get('candidate_quality') for item in candidates)),
        'with_excerpts': sum(1 for item in candidates if item.get('candidate_excerpt_ids')),
        'prompt_injection_not_retrieved': sum(
            1 for item in candidates
            if item.get('category') == 'prompt_injection'
            and item.get('retrieval_status') == 'not_retrieved'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Phase 6 local candidate evidence queue')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--json', action='store_true', dest='as_json')
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args(argv)
    if args.write:
        candidates = build_candidates(limit=args.limit, workers=args.workers)
        write_candidates(candidates)
    issues = validate_candidate_file()
    try:
        candidates = load_candidates() if Path(CANDIDATES_PATH).is_file() else []
    except (OSError, ValueError):
        candidates = []
    result = {
        'status': 'passed' if not issues else 'failed',
        'issues': issues,
        'external_calls': 0,
        'summary': _summary(candidates),
    }
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('phase6_candidates=' + result['status'])
        print('candidates=' + str(result['summary']['candidates']))
        for issue in issues:
            print('issue: ' + issue)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
