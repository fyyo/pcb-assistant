'''Fail-closed runner for a real Phase 6.2 managed-provider trial.

Preflight is local and makes no network request. A real provider request
requires ``--execute``, server-side cloud opt-in, a configured provider, and
installed local privacy models. Reports contain only timing, status, error
codes, and citation checks; they never contain questions, answers, raw
evidence, API keys, or local paths.
'''
from __future__ import annotations

import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import tempfile
import time

import phase6_managed


ROOT = Path(__file__).resolve().parent
DOCS = ROOT / 'docs'
SCHEMA = 'pcb-phase6-managed-trial-v1'
MAX_P95_SECONDS = 30.0
DEFAULT_QUESTIONS = (
    '压合后出现白斑，应该怎么排查？',
    '真空度会造成哪些压合缺陷？',
    '压合温度曲线应重点核查什么？',
)


def _p95(values):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def preflight(*, environ=None, sanitizer_loader=None):
    '''Check every prerequisite without creating a provider request.'''
    env = os.environ if environ is None else environ
    loader = (phase6_managed._sanitizer_components
              if sanitizer_loader is None else sanitizer_loader)
    checks = []

    opted_in = (
        env.get('PCB_ALLOW_CLOUD_EGRESS', 'false').strip().lower() == 'true')
    checks.append({
        'check': 'server_cloud_opt_in',
        'status': 'passed' if opted_in else 'blocked',
        'reason': None if opted_in else 'cloud_disabled',
    })

    config = phase6_managed.provider_config(env)
    configured = config is not None
    checks.append({
        'check': 'provider_configuration',
        'status': 'passed' if configured else 'blocked',
        'reason': None if configured else 'cloud_not_configured',
    })

    sanitizer_status = 'skipped'
    sanitizer_reason = 'prerequisite_not_met'
    if opted_in and configured:
        try:
            loader()
        except phase6_managed.ManagedFailure as exc:
            sanitizer_status = 'blocked'
            sanitizer_reason = exc.code
        except Exception:
            sanitizer_status = 'blocked'
            sanitizer_reason = 'policy_blocked'
        else:
            sanitizer_status = 'passed'
            sanitizer_reason = None
    checks.append({
        'check': 'local_privacy_models',
        'status': sanitizer_status,
        'reason': sanitizer_reason,
    })

    ready = all(item['status'] == 'passed' for item in checks)
    return {
        'schema': SCHEMA + '-preflight',
        'status': 'passed' if ready else 'blocked',
        'ready': ready,
        'checks': checks,
        'provider': config.provider if config else None,
        'model': config.model if config else None,
        'external_services': False,
        'data_egress': 'none',
    }


def _trial_row(trial_id, outcome, latency_ms):
    validation = outcome.result.get('citation_validation') or {}
    model = outcome.result.get('model') or {}
    attempted = bool(
        outcome.external_services
        and outcome.data_egress == 'sanitized_evidence'
    )
    model_used = bool(
        outcome.status == 'answered'
        and outcome.result.get('generated_by_model')
    )
    citation_ok = validation.get('status') == 'passed'
    return {
        'trial_id': trial_id,
        'status': outcome.status,
        'provider_request_attempted': attempted,
        'generated_by_model': model_used,
        'citation_validation': validation.get('status'),
        'fallback_reason': model.get('fallback_reason'),
        'external_services': bool(outcome.external_services),
        'data_egress': outcome.data_egress,
        'latency_ms': round(max(0.0, latency_ms), 3),
        'passed': attempted and model_used and citation_ok,
    }


def build_report(rows, *, provider=None, model=None,
                 threshold_seconds=MAX_P95_SECONDS):
    '''Build a content-free report from trial result rows.'''
    source_rows = list(rows)
    latencies = [row['latency_ms'] for row in source_rows]
    p95_ms = _p95(latencies)
    p95_seconds = round(p95_ms / 1000.0, 6) if p95_ms is not None else None
    provider_requests = sum(
        1 for row in source_rows if row.get('provider_request_attempted'))
    successful_requests = sum(
        1 for row in source_rows
        if row.get('passed') and row.get('provider_request_attempted'))
    fallback_requests = sum(
        1 for row in source_rows
        if row.get('provider_request_attempted')
        and not row.get('generated_by_model'))
    all_rows_passed = (
        bool(source_rows) and all(row.get('passed') for row in source_rows))
    all_requests_attempted = (
        bool(source_rows) and provider_requests == len(source_rows))
    p95_ok = (
        isinstance(p95_seconds, (int, float))
        and 0 < p95_seconds <= threshold_seconds
    )
    passed = all_rows_passed and all_requests_attempted and p95_ok
    return {
        'schema': SCHEMA,
        'phase': '6.2-managed-trial',
        'date': date.today().isoformat(),
        'status': 'passed' if passed else 'blocked',
        'provider': provider,
        'model': model,
        'provider_requests': provider_requests,
        'successful_requests': successful_requests,
        'fallback_requests': fallback_requests,
        'p95_seconds': p95_seconds,
        'max_latency_seconds': (
            round(max(latencies) / 1000.0, 6) if latencies else None
        ),
        'threshold_seconds': threshold_seconds,
        'data_egress': 'sanitized_evidence' if provider_requests else 'none',
        'external_services': provider_requests > 0,
        'results': source_rows,
        'release_blocker': None if passed else 'managed_p95_not_measured',
        'notes': [
            'Preflight is local and makes no provider request.',
            'Only explicitly executed trials count as provider_requests.',
            'The report excludes question text, answers, evidence text, API keys, and local paths.',
        ],
    }


def run_trial(questions, *, environ=None, limit=6, provider_factory=None,
              session_factory=None, clock=time.monotonic,
              sanitizer_loader=None):
    '''Run explicit managed questions after all local prerequisites pass.'''
    env = os.environ if environ is None else environ
    source_questions = list(questions)
    check = preflight(environ=env, sanitizer_loader=sanitizer_loader)
    if not check['ready']:
        report = build_report(
            [], provider=check['provider'], model=check['model'])
        report['preflight'] = check
        report['blocked_reason'] = next(
            item['reason'] for item in check['checks']
            if item['status'] != 'passed' and item['reason'])
        return report

    rows = []
    for index, question in enumerate(source_questions, 1):
        started = clock()
        outcome = phase6_managed.answer(
            {'question': question, 'mode': 'managed', 'limit': limit},
            'REQ_trial_' + str(index),
            environ=env,
            provider_factory=(
                provider_factory or phase6_managed.OpenAIResponsesProvider),
            session_factory=(
                session_factory or phase6_managed.ChatSession),
        )
        elapsed_ms = (clock() - started) * 1000.0
        rows.append(_trial_row(
            'T' + str(index).zfill(2), outcome, elapsed_ms))

    report = build_report(
        rows, provider=check['provider'], model=check['model'])
    report['preflight'] = check
    report['blocked_reason'] = report['release_blocker']
    return report


def read_questions(path):
    '''Read one non-empty, non-comment question per line.'''
    source = Path(path)
    questions = []
    with source.open('r', encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            value = line.strip()
            if not value or value.startswith('#'):
                continue
            if len(value) > 1000:
                raise ValueError(
                    str(source) + ':' + str(line_number)
                    + ': question exceeds 1000 characters')
            questions.append(value)
    return questions


def write_report(report, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
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


def _default_report_path():
    return DOCS / (
        'PHASE6_2_MANAGED_TRIAL_'
        + date.today().strftime('%Y%m%d') + '.json')


def _display_path(path):
    target = Path(path)
    try:
        return target.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return target.name


def _display_status(report):
    print('phase6_managed_trial=' + report['status'])
    print('provider_requests=' + str(report['provider_requests']))
    print('successful_requests=' + str(report['successful_requests']))
    print('fallback_requests=' + str(report['fallback_requests']))
    print('p95_seconds=' + str(report['p95_seconds']))
    print('data_egress=' + report['data_egress'])
    if report.get('blocked_reason'):
        print('blocked_reason=' + str(report['blocked_reason']))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Phase 6.2 explicit real managed-provider trial')
    parser.add_argument(
        '--execute', action='store_true',
        help='Allow the explicit managed provider requests')
    parser.add_argument(
        '--question', action='append', default=[],
        help='Question to run; may be repeated')
    parser.add_argument(
        '--questions', metavar='PATH',
        help='UTF-8 file with one question per line')
    parser.add_argument('--limit', type=int, default=6)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--report', metavar='PATH')
    parser.add_argument('--require-pass', action='store_true')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= 8:
        parser.error('--limit must be between 1 and 8')
    if args.write and not args.execute:
        parser.error(
            '--write requires --execute so preflight is not recorded as a trial')

    if not args.execute:
        report = preflight()
        report['requested_action'] = 'preflight_only'
        if args.as_json:
            print(json.dumps(
                report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            summary = dict(report)
            summary.update({
                'provider_requests': 0,
                'successful_requests': 0,
                'fallback_requests': 0,
                'p95_seconds': None,
            })
            summary['blocked_reason'] = next(
                (item['reason'] for item in report['checks']
                 if item['status'] != 'passed' and item['reason']), None)
            _display_status(summary)
            for item in report['checks']:
                suffix = ''
                if item.get('reason'):
                    suffix = ' (' + str(item['reason']) + ')'
                print('check:' + item['check'] + '=' + item['status'] + suffix)
        return 1 if args.require_pass and not report['ready'] else 0

    questions = list(args.question)
    if args.questions:
        try:
            questions.extend(read_questions(args.questions))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if not questions:
        questions = list(DEFAULT_QUESTIONS)

    report = run_trial(questions, limit=args.limit)
    if args.write:
        target = write_report(report, args.report or _default_report_path())
        report['report_file'] = _display_path(target)
    if args.as_json:
        print(json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _display_status(report)
        if report.get('report_file'):
            print('report_file=' + report['report_file'])
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
