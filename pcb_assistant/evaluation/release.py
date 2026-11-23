'''Deterministic Phase 6 release-readiness audit.

The audit reads existing local evidence only. It never generates expert labels,
never calls a model, and never treats a missing or partial trial as a pass. Its
output is a release-readiness report with explicit blockers and next actions.
'''
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re

from pcb_assistant.evaluation.annotation import (
    ANNOTATIONS_PATH,
    annotation_summary,
    load_records,
)
from pcb_assistant.interfaces.contract import (
    CONTRACT_VERSION,
    load_cases,
    validate_contracts,
)


from pcb_assistant.paths import ROOT
DOCS = ROOT / 'docs'
# Dated acceptance reports live in docs/ while in use and are later archived
# under .private/project_records/docs/ to keep the public docs slim. The audit
# must still find them; a report that exists nowhere stays a blocker.
ARCHIVE_DOCS = ROOT / '.private' / 'project_records' / 'docs'
REPORT_SCHEMA = 'pcb-phase6-release-readiness-v1'
EXPECTED_CASES = 150
EXPECTED_TOOLS = 8
EXPECTED_PUBLIC_TOOLS = {'pcb_answer'}
EXPECTED_ERRORS = 17
_LOCAL_PATH = re.compile(r'(?i)\b[A-Z]:\\[^\s"\']+|\\\\[^\\\s]+\\[^\s"\']+')
_SECRET = re.compile(r'\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b')

DEFAULT_SOURCES = {
    'contract': 'contracts/phase6',
    'annotations': 'contracts/phase6/eval/annotations.jsonl',
    'local_eval': 'docs/PHASE6_3_LOCAL_EVAL_YYYYMMDD.json',
    'local_conformance': 'docs/PHASE6_4_LOCAL_CONFORMANCE_YYYYMMDD.json',
    'codex': 'docs/PHASE6_4_CODEX_APPSERVER_PREFLIGHT_YYYYMMDD.json',
    'managed_trial': 'docs/PHASE6_2_MANAGED_TRIAL_YYYYMMDD.json',
    'severe_review': 'docs/PHASE6_3_SEVERE_REVIEW_YYYYMMDD.json',
    'hermes': 'docs/PHASE6_4_HERMES_TRIAL_YYYYMMDD.json',
    'dsh': 'docs/PHASE6_4_DSH_TRIAL_YYYYMMDD.json',
}


def _safe_error(exc):
    text = str(exc)
    text = _LOCAL_PATH.sub('[local_path]', text)
    return _SECRET.sub('[redacted]', text)


def _safe_source(path):
    if path is None:
        return None
    value = Path(path)
    try:
        return value.resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return value.name


def _load_json(path):
    if path is None:
        return None
    source = Path(path)
    if not source.is_file():
        return None
    try:
        value = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _latest(pattern):
    '''Newest dated report; docs/ wins over the archive for the same date.'''
    found = [
        (path.name, -priority, path)
        for priority, directory in enumerate((DOCS, ARCHIVE_DOCS))
        for path in directory.glob(pattern)
        if path.is_file()
    ]
    return max(found)[2] if found else None


def load_annotation_state(path=ANNOTATIONS_PATH):
    cases = load_cases()
    source = Path(path)
    if not source.is_file():
        return {
            'schema': 'pcb-phase6-annotation-v1',
            'status': 'not_started',
            'exists': False,
            'cases': len(cases),
            'records': 0,
            'complete': 0,
            'pending': len(cases),
            'reviewers': [],
            'issues': [],
        }
    try:
        records = load_records(source)
        result = annotation_summary(records, cases)
    except (OSError, ValueError) as exc:
        return {
            'schema': 'pcb-phase6-annotation-v1',
            'status': 'failed',
            'exists': True,
            'cases': len(cases),
            'records': 0,
            'complete': 0,
            'pending': len(cases),
            'reviewers': [],
            'issues': [_safe_error(exc)],
        }
    result['exists'] = True
    return result


def _check(name, passed, *, observed, source, blocker, next_action):
    value = {
        'check': name,
        'status': 'passed' if passed else 'blocked',
        'observed': observed,
        'source': source,
    }
    if not passed:
        value['blocker'] = blocker
        value['next_action'] = next_action
    return value


def _metrics(report):
    if not isinstance(report, dict):
        return {}
    values = {}
    for item in report.get('thresholds') or ():
        if isinstance(item, dict) and isinstance(item.get('metric'), str):
            values[item['metric']] = item
    return values


def _metric_passed(metrics, name):
    item = metrics.get(name)
    return isinstance(item, dict) and item.get('status') == 'passed'


def _metric_value(metrics, name):
    item = metrics.get(name)
    return item.get('value') if isinstance(item, dict) else None


def _positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value):
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _client_trial_passed(report):
    return (
        isinstance(report, dict)
        and report.get('schema') == 'pcb-phase6-client-trial-v1'
        and report.get('status') == 'passed'
        and report.get('real_client_trial_complete') is True
    )


def evaluate_readiness(
        *, contract_result, annotation_result, eval_report, local_report,
        codex_report, managed_trial=None, severe_review=None,
        client_reports=None, sources=None):
    '''Evaluate a release-readiness snapshot from already-produced evidence.'''
    source_values = dict(DEFAULT_SOURCES)
    source_values.update(sources or {})
    client_reports = client_reports if isinstance(client_reports, dict) else {}
    checks = []

    counts = contract_result.get('counts') if isinstance(contract_result, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    contract_ok = (
        isinstance(contract_result, dict)
        and contract_result.get('status') == 'passed'
        and counts.get('tools') == EXPECTED_TOOLS
        and counts.get('errors') == EXPECTED_ERRORS
        and counts.get('eval_cases') == EXPECTED_CASES
    )
    checks.append(_check(
        'contract_validation',
        contract_ok,
        observed={
            'tools': counts.get('tools'),
            'errors': counts.get('errors'),
            'eval_cases': counts.get('eval_cases'),
        },
        source=source_values['contract'],
        blocker='contract_invalid',
        next_action='Regenerate or fix the Phase 6 contract and eval artifacts.',
    ))

    dataset = eval_report.get('dataset') if isinstance(eval_report, dict) else {}
    dataset = dataset if isinstance(dataset, dict) else {}
    runtime = eval_report.get('runtime') if isinstance(eval_report, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    dataset_cases = _nonnegative_int(dataset.get('cases'))
    runtime_cases = _nonnegative_int(runtime.get('cases'))
    eval_ok = (
        isinstance(eval_report, dict)
        and eval_report.get('schema') == 'pcb-phase6-eval-run-v1'
        and dataset_cases == EXPECTED_CASES
        and runtime_cases == EXPECTED_CASES
    )
    checks.append(_check(
        'local_eval_integrity',
        eval_ok,
        observed={'dataset_cases': dataset_cases, 'runtime_cases': runtime_cases},
        source=source_values['local_eval'],
        blocker='local_eval_invalid',
        next_action='Run the full Phase 6.3 local evaluation and write its report.',
    ))

    metrics = _metrics(eval_report)
    failed_metrics = sorted(
        name for name, item in metrics.items()
        if isinstance(item, dict) and item.get('status') == 'failed'
    )
    checks.append(_check(
        'local_eval_thresholds',
        eval_ok and not failed_metrics,
        observed={'failed_metrics': failed_metrics},
        source=source_values['local_eval'],
        blocker='local_eval_threshold_failed',
        next_action='Fix the failing local structural or latency threshold.',
    ))

    annotation_cases = _nonnegative_int(annotation_result.get('cases'))
    complete = _nonnegative_int(annotation_result.get('complete'))
    annotation_ok = (
        annotation_result.get('status') == 'passed'
        and annotation_result.get('exists') is True
        and annotation_cases == EXPECTED_CASES
        and complete == EXPECTED_CASES
    )
    checks.append(_check(
        'expert_gold_complete',
        annotation_ok,
        observed={'complete': complete, 'cases': annotation_cases},
        source=source_values['annotations'],
        blocker='expert_gold_incomplete',
        next_action='Complete all expert annotations in the versioned sidecar.',
    ))

    gold_metrics = (
        'no_evidence_precision',
        'evidence_recall_at_8',
        'required_field_coverage',
    )
    gold_metrics_ok = all(_metric_passed(metrics, name) for name in gold_metrics)
    checks.append(_check(
        'gold_metrics',
        gold_metrics_ok,
        observed={
            name: _metric_value(metrics, name) for name in gold_metrics
        },
        source=source_values['local_eval'],
        blocker='gold_metrics_not_measured',
        next_action='Re-run the full evaluation after expert gold is complete.',
    ))

    citation_ok = (
        _metric_passed(metrics, 'citation_validation_pass_rate')
        and _metric_passed(metrics, 'invalid_exc_references')
    )
    checks.append(_check(
        'citation_integrity',
        citation_ok,
        observed={
            'citation_validation_pass_rate': _metric_value(
                metrics, 'citation_validation_pass_rate'),
            'invalid_exc_references': _metric_value(
                metrics, 'invalid_exc_references'),
        },
        source=source_values['local_eval'],
        blocker='citation_integrity_failed',
        next_action='Fix invalid or unsupported citations before release.',
    ))

    prompt_ok = _metric_passed(metrics, 'prompt_injection_pass_rate')
    checks.append(_check(
        'prompt_injection',
        prompt_ok,
        observed=_metric_value(metrics, 'prompt_injection_pass_rate'),
        source=source_values['local_eval'],
        blocker='prompt_injection_failed',
        next_action='Restore the 10/10 prompt-injection gate.',
    ))

    egress_ok = _metric_passed(metrics, 'sensitive_egress_pass_rate')
    checks.append(_check(
        'sensitive_egress',
        egress_ok,
        observed=_metric_value(metrics, 'sensitive_egress_pass_rate'),
        source=source_values['local_eval'],
        blocker='sensitive_egress_failed',
        next_action='Fix sensitive-data egress before any managed trial.',
    ))

    evidence_p95_ok = _metric_passed(metrics, 'evidence_p95_seconds')
    checks.append(_check(
        'evidence_p95',
        evidence_p95_ok,
        observed=_metric_value(metrics, 'evidence_p95_seconds'),
        source=source_values['local_eval'],
        blocker='evidence_p95_exceeded',
        next_action='Reduce deterministic evidence latency to 2 seconds or less.',
    ))

    managed_p95 = None
    managed_requests = 0
    managed_ok = False
    managed_source = source_values['managed_trial']
    if isinstance(managed_trial, dict):
        managed_source = _safe_source(managed_trial.get('source_file'))
        managed_p95 = managed_trial.get('p95_seconds')
        managed_requests = _nonnegative_int(
            managed_trial.get('provider_requests'))
        managed_ok = (
            managed_trial.get('schema') == 'pcb-phase6-managed-trial-v1'
            and managed_trial.get('status') == 'passed'
            and _positive_number(managed_p95)
            and managed_p95 <= 30
            and managed_requests > 0
            and managed_trial.get('data_egress') == 'sanitized_evidence'
        )
    checks.append(_check(
        'managed_p95',
        managed_ok,
        observed={
            'p95_seconds': managed_p95,
            'provider_requests': managed_requests,
        },
        source=managed_source or source_values['managed_trial'],
        blocker='managed_p95_not_measured',
        next_action='Run a real managed trial and record p95 <= 30 seconds.',
    ))

    severe_count = None
    severe_reviewed = 0
    severe_ok = False
    severe_source = source_values['severe_review']
    if isinstance(severe_review, dict):
        severe_source = _safe_source(severe_review.get('source_file'))
        severe_count = severe_review.get('unsupported_conclusions')
        severe_reviewed = _nonnegative_int(
            severe_review.get('reviewed_answers'))
        severe_ok = (
            severe_review.get('schema') == 'pcb-phase6-severe-review-v1'
            and severe_review.get('status') == 'passed'
            and severe_count == 0
            and severe_reviewed > 0
            and bool(severe_review.get('reviewer'))
            and bool(severe_review.get('reviewed_at'))
        )
    checks.append(_check(
        'severe_unsupported_conclusions',
        severe_ok,
        observed={
            'unsupported_conclusions': severe_count,
            'reviewed_answers': severe_reviewed,
        },
        source=severe_source or source_values['severe_review'],
        blocker='severe_unsupported_conclusions_not_reviewed',
        next_action='Complete an expert review of severe unsupported conclusions.',
    ))

    local_ok = (
        isinstance(local_report, dict)
        and local_report.get('schema') == 'pcb-phase6-client-conformance-v1'
        and local_report.get('status') == 'passed'
    )
    checks.append(_check(
        'local_conformance',
        local_ok,
        observed=(local_report or {}).get('status')
        if isinstance(local_report, dict) else None,
        source=source_values['local_conformance'],
        blocker='local_conformance_failed',
        next_action='Re-run the local MCP stdio conformance preflight.',
    ))

    codex_calls = []
    if isinstance(codex_report, dict):
        codex_calls = codex_report.get('calls') or []
    codex_tools = codex_report.get('tools') if isinstance(codex_report, dict) else None
    codex_tools = codex_tools if isinstance(codex_tools, list) else []
    codex_ok = (
        isinstance(codex_report, dict)
        and codex_report.get('schema') == 'pcb-phase6-codex-appserver-preflight-v1'
        and codex_report.get('status') == 'passed'
        and (codex_report.get('client') or {}).get('real_vendor_client') is True
        and codex_report.get('codex_tool_call_complete') is True
        and len(codex_tools) == len(EXPECTED_PUBLIC_TOOLS)
        and {item.get('name') for item in codex_tools if isinstance(item, dict)} == EXPECTED_PUBLIC_TOOLS
        and len(codex_calls) == len(EXPECTED_PUBLIC_TOOLS)
        and {item.get('tool') for item in codex_calls if isinstance(item, dict)} == EXPECTED_PUBLIC_TOOLS
        and all(
            isinstance(item, dict) and item.get('status') == 'passed'
            for item in codex_calls
        )
    )
    checks.append(_check(
        'codex_tool_bridge',
        codex_ok,
        observed={
            'tools': len(codex_tools),
            'calls': len(codex_calls),
        },
        source=source_values['codex'],
        blocker='codex_tool_bridge_incomplete',
        next_action='Re-run the isolated Codex app-server tool-call preflight.',
    ))

    codex_client = codex_report.get('client') if isinstance(codex_report, dict) else {}
    codex_client = codex_client if isinstance(codex_client, dict) else {}
    model_turn_ok = codex_ok and codex_report.get('model_turn_complete') is True
    checks.append(_check(
        'codex_model_turn',
        model_turn_ok,
        observed={
            'model_turn_complete': codex_report.get('model_turn_complete')
            if isinstance(codex_report, dict) else None,
            'auth_status': codex_client.get('auth_status'),
        },
        source=source_values['codex'],
        blocker='codex_model_turn_not_run',
        next_action='Configure Codex authentication and run a real model turn.',
    ))

    for name, blocker in (
            ('hermes', 'hermes_client_trial_not_run'),
            ('dsh', 'dsh_client_trial_not_run')):
        report = client_reports.get(name)
        checks.append(_check(
            name + '_client_trial',
            _client_trial_passed(report),
            observed={
                'report_present': isinstance(report, dict),
                'status': report.get('status') if isinstance(report, dict) else None,
                'real_client_trial_complete': (
                    report.get('real_client_trial_complete')
                    if isinstance(report, dict) else None
                ),
            },
            source=source_values[name],
            blocker=blocker,
            next_action=(
                'Run the client through MCP configuration and record audit and '
                'fallback evidence.'
            ),
        ))

    blockers = []
    for check in checks:
        if check['status'] != 'passed' and check['blocker'] not in blockers:
            blockers.append(check['blocker'])
    release_ready = not blockers
    return {
        'schema': REPORT_SCHEMA,
        'phase': '6-release-readiness',
        'date': date.today().isoformat(),
        'contract_version': CONTRACT_VERSION,
        'status': 'ready' if release_ready else 'blocked',
        'release_ready': release_ready,
        'checks': checks,
        'release_blockers': blockers,
        'external_services': False,
        'data_egress': 'none',
        'local_only': True,
        'inputs': {
            name: {
                'source': source_values[name],
                'present': (
                    name == 'contract'
                    or name == 'annotations' and annotation_result.get('exists', False)
                    or ({
                        'local_eval': eval_report,
                        'local_conformance': local_report,
                        'codex': codex_report,
                        'managed_trial': managed_trial,
                        'severe_review': severe_review,
                        'hermes': client_reports.get('hermes'),
                        'dsh': client_reports.get('dsh'),
                    }.get(name) is not None)
                ),
            }
            for name in source_values
        },
        'notes': [
            'This audit only reads existing local evidence and never creates gold.',
            'A missing report is a blocker, not a pass.',
            'Dated reports are read from docs/ or the archived .private/project_records/docs/.',
            'The audit itself does not authorize a release or cloud egress.',
        ],
    }


def write_report(summary, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    return target


def _display_path(path):
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.name


def _load_optional(path):
    report = _load_json(path)
    if isinstance(report, dict) and path is not None:
        report = dict(report)
        report.setdefault('source_file', _safe_source(path))
    return report


def run_audit(
        *, annotations=ANNOTATIONS_PATH, eval_report=None,
        local_conformance=None, codex_report=None, managed_trial=None,
        severe_review=None, hermes_trial=None, dsh_trial=None):
    '''Run the audit against explicit or latest local artifacts.'''
    eval_path = Path(eval_report) if eval_report else _latest(
        'PHASE6_3_LOCAL_EVAL_*.json')
    local_path = Path(local_conformance) if local_conformance else _latest(
        'PHASE6_4_LOCAL_CONFORMANCE_*.json')
    codex_path = Path(codex_report) if codex_report else _latest(
        'PHASE6_4_CODEX_APPSERVER_PREFLIGHT_*.json')
    managed_path = Path(managed_trial) if managed_trial else _latest(
        'PHASE6_2_MANAGED_TRIAL_*.json')
    severe_path = Path(severe_review) if severe_review else _latest(
        'PHASE6_3_SEVERE_REVIEW_*.json')
    hermes_path = Path(hermes_trial) if hermes_trial else _latest(
        'PHASE6_4_HERMES_TRIAL_*.json')
    dsh_path = Path(dsh_trial) if dsh_trial else _latest(
        'PHASE6_4_DSH_TRIAL_*.json')
    try:
        contract_result = validate_contracts()
    except Exception as exc:
        contract_result = {
            'status': 'failed',
            'issues': [_safe_error(exc)],
            'counts': {},
        }
    sources = {
        'contract': DEFAULT_SOURCES['contract'],
        'annotations': _safe_source(annotations),
        'local_eval': _safe_source(eval_path) if eval_path else DEFAULT_SOURCES['local_eval'],
        'local_conformance': (
            _safe_source(local_path) if local_path
            else DEFAULT_SOURCES['local_conformance']
        ),
        'codex': _safe_source(codex_path) if codex_path else DEFAULT_SOURCES['codex'],
        'managed_trial': (
            _safe_source(managed_path) if managed_path
            else DEFAULT_SOURCES['managed_trial']
        ),
        'severe_review': (
            _safe_source(severe_path) if severe_path
            else DEFAULT_SOURCES['severe_review']
        ),
        'hermes': _safe_source(hermes_path) if hermes_path else DEFAULT_SOURCES['hermes'],
        'dsh': _safe_source(dsh_path) if dsh_path else DEFAULT_SOURCES['dsh'],
    }
    return evaluate_readiness(
        contract_result=contract_result,
        annotation_result=load_annotation_state(annotations),
        eval_report=_load_json(eval_path),
        local_report=_load_json(local_path),
        codex_report=_load_json(codex_path),
        managed_trial=_load_optional(managed_path),
        severe_review=_load_optional(severe_path),
        client_reports={
            'hermes': _load_optional(hermes_path),
            'dsh': _load_optional(dsh_path),
        },
        sources=sources,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Phase 6 release-readiness audit')
    parser.add_argument('--annotations', default=str(ANNOTATIONS_PATH))
    parser.add_argument('--eval-report')
    parser.add_argument('--local-conformance')
    parser.add_argument('--codex-report')
    parser.add_argument('--managed-trial')
    parser.add_argument('--severe-review')
    parser.add_argument('--hermes-trial')
    parser.add_argument('--dsh-trial')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--report')
    parser.add_argument('--json', action='store_true', dest='as_json')
    parser.add_argument('--require-ready', action='store_true')
    args = parser.parse_args(argv)
    summary = run_audit(
        annotations=args.annotations,
        eval_report=args.eval_report,
        local_conformance=args.local_conformance,
        codex_report=args.codex_report,
        managed_trial=args.managed_trial,
        severe_review=args.severe_review,
        hermes_trial=args.hermes_trial,
        dsh_trial=args.dsh_trial,
    )
    if args.write:
        run_date = str(summary['date']).replace('-', '')
        report = Path(args.report) if args.report else (
            ROOT / 'docs' / ('PHASE6_RELEASE_READINESS_' + run_date + '.json'))
        summary['report_file'] = _display_path(report)
        write_report(summary, report)
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        passed = sum(1 for item in summary['checks'] if item['status'] == 'passed')
        print('phase6_release=' + summary['status'])
        print('release_ready=' + str(summary['release_ready']).lower())
        print('checks_passed=' + str(passed) + '/' + str(len(summary['checks'])))
        print('blockers=' + ','.join(summary['release_blockers']))
    if args.require_ready and not summary['release_ready']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
