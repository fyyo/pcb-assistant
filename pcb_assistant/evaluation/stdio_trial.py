'''Local stdio client conformance preflight for Phase 6.4.

This runner starts the real ``phase6_mcp.py`` server as a subprocess and speaks
newline-delimited JSON-RPC to it. It validates the read-only tool surface,
envelopes, evidence-only default, and path-free output. It is deliberately not
labelled as a Codex/Hermes/DSH vendor-client trial: those clients must still be
configured and exercised manually before Phase 6.4 can be completed.
'''
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys

from pcb_assistant.interfaces.contract import CONTRACT_VERSION, ENVELOPE_SCHEMA, validate_instance
from pcb_assistant.interfaces.mcp import SUPPORTED_PROTOCOL_VERSIONS, catalog_version


from pcb_assistant.paths import ROOT
SERVER = ROOT / 'phase6_mcp.py'
REPORT_SCHEMA = 'pcb-phase6-client-conformance-v1'
DEFAULT_TIMEOUT_SECONDS = 90
EXPECTED_TOOLS = 1
SAMPLE_DOC_ID = 'KNOWLEDGE_079d6df7ac8cc272572281a243b15833'
SAMPLE_EXCERPT_ID = 'EXC_2ba4cda4fc3fbc2b133250fc'
_FORBIDDEN_KEYS = {
    'source_path', 'path', 'file_path', 'filesystem_path', 'absolute_path',
    'local_path', 'artifact_path', 'storage_path', 'source_file', 'pst_path',
    'original_text', 'normalized_text',
}
_LOCAL_PATH = re.compile(r'(?i)\b[A-Z]:\\[^\s"\']+|\\\\[^\\\s]+\\[^\s"\']+')
_SECRET = re.compile(r'\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b')


def _requests():
    calls = [('pcb_answer', {
        'question': '压合后板边出现白斑，应该怎么排查？', 'limit': 3,
    })]
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-06-18'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
    ]
    for index, (name, arguments) in enumerate(calls, 101):
        messages.append({
            'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments},
        })
    return messages, [name for name, _arguments in calls]


def _contains_forbidden_key(value):
    if isinstance(value, dict):
        return any(
            key.lower() in _FORBIDDEN_KEYS
            or key.lower().endswith('_path')
            or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _path_or_secret_free(value):
    text = json.dumps(value, ensure_ascii=False)
    return not bool(_LOCAL_PATH.search(text) or _SECRET.search(text))


def _safe_error(exc):
    text = str(exc)
    text = _LOCAL_PATH.sub('[local_path]', text)
    return _SECRET.sub('[redacted]', text)


def _invoke_server(*, timeout=DEFAULT_TIMEOUT_SECONDS, catalog=None):
    messages, tool_names = _requests()
    payload = '\n'.join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) for item in messages
    ) + '\n'
    command = [sys.executable, '-X', 'utf8', '-B', str(SERVER)]
    if catalog is not None:
        command.extend(['--catalog', str(Path(catalog).resolve())])
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
    )
    try:
        stdout, stderr = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise TimeoutError('MCP server did not finish within ' + str(timeout) + 's')
    responses = []
    for line in stdout.splitlines():
        text = line.strip()
        if text:
            responses.append(json.loads(text))
    return responses, tool_names, bool(stderr.strip()), process.returncode


def _check(name, passed, detail=None):
    return {
        'check': name,
        'status': 'passed' if passed else 'failed',
        'detail': detail,
    }


def evaluate_responses(responses, tool_names, *, server_exit_code=0, expected_catalog_version=None):
    by_id = {
        response.get('id'): response
        for response in responses if isinstance(response, dict)
    }
    initialize = by_id.get(1, {}).get('result', {})
    listed = by_id.get(3, {}).get('result', {}).get('tools', [])
    calls = []
    catalog_versions = set()
    all_envelopes_valid = True
    all_read_only = True
    all_local_only = True
    all_path_free = True
    all_calls_ok = True
    answer_evidence_only = False
    answer_auto_diagnosis = False
    for index, tool_name in enumerate(tool_names, 101):
        result = by_id.get(index)
        if not isinstance(result, dict) or 'result' not in result:
            calls.append({
                'tool': tool_name,
                'status': 'missing',
                'envelope_valid': False,
            })
            all_calls_ok = False
            all_envelopes_valid = False
            all_read_only = False
            all_local_only = False
            all_path_free = False
            continue
        call_result = result.get('result', {})
        envelope = call_result.get('structuredContent')
        is_error = call_result.get('isError')
        if isinstance(envelope, dict) and envelope.get('catalog_version'):
            catalog_versions.add(envelope['catalog_version'])
        envelope_valid = (
            isinstance(envelope, dict)
            and not validate_instance(envelope, ENVELOPE_SCHEMA, '$.envelope')
        )
        read_only = isinstance(envelope, dict) and envelope.get('read_only') is True
        local_only = (
            isinstance(envelope, dict)
            and envelope.get('external_services') is False
            and envelope.get('data_egress') == 'none'
        )
        path_free = (
            isinstance(envelope, dict)
            and _path_or_secret_free(envelope)
            and not _contains_forbidden_key(envelope)
        )
        if tool_name == 'pcb_answer' and isinstance(envelope, dict):
            answer_result = envelope.get('result', {})
            answer_evidence_only = (
                answer_result.get('answer_mode') != 'managed'
                and answer_result.get('generated_by_model') is False
            )
            answer_auto_diagnosis = (answer_result.get('workflow') or {}).get('route') == 'diagnosis'
        all_calls_ok = all_calls_ok and is_error is False
        all_envelopes_valid = all_envelopes_valid and envelope_valid
        all_read_only = all_read_only and read_only
        all_local_only = all_local_only and local_only
        all_path_free = all_path_free and path_free
        calls.append({
            'tool': tool_name,
            'status': 'passed' if (
                is_error is False and envelope_valid and read_only
                and local_only and path_free) else 'failed',
            'envelope_status': envelope.get('status') if isinstance(envelope, dict) else None,
            'envelope_valid': envelope_valid,
            'read_only': read_only,
            'local_only': local_only,
            'path_free': path_free,
        })
    checks = [
        _check('server_exit_code', server_exit_code == 0, server_exit_code),
        _check(
            'protocol_negotiation',
            initialize.get('protocolVersion') in SUPPORTED_PROTOCOL_VERSIONS,
            initialize.get('protocolVersion')),
        _check(
            'tools_list_count',
            len(listed) == EXPECTED_TOOLS and {item.get('name') for item in listed} == {'pcb_answer'},
            len(listed)),
        _check(
            'all_calls_returned',
            tool_names == ['pcb_answer']
            and len(calls) == EXPECTED_TOOLS
            and all(call['status'] != 'missing' for call in calls),
            len(calls)),
        _check('all_calls_ok', all_calls_ok),
        _check('all_envelopes_valid', all_envelopes_valid),
        _check('all_read_only', all_read_only),
        _check('all_local_only', all_local_only),
        _check('path_and_secret_free', all_path_free),
        _check('answer_defaults_to_evidence_only', answer_evidence_only),
        _check('answer_routes_diagnosis_automatically', answer_auto_diagnosis),
    ]
    if expected_catalog_version is not None:
        checks.append(_check('selected_catalog_version_matches',
                             catalog_versions == {expected_catalog_version}))
    status = 'passed' if all(item['status'] == 'passed' for item in checks) else 'failed'
    return {
        'schema': REPORT_SCHEMA,
        'phase': '6.4-preflight',
        'date': date.today().isoformat(),
        'contract_version': CONTRACT_VERSION,
        'transport': 'stdio',
        'client': {
            'kind': 'local_python_jsonrpc_client',
            'real_vendor_client': False,
        },
        'server': initialize.get('serverInfo'),
        'status': status,
        'checks': checks,
        'calls': calls,
        'catalog_versions': sorted(catalog_versions),
        'external_services': False,
        'real_client_trial_complete': False,
        'release_ready': False,
        'release_blockers': [
            'expert_gold_incomplete',
            'managed_p95_not_measured',
            'severe_unsupported_conclusions_not_reviewed',
            'codex_client_trial_incomplete',
            'hermes_client_trial_not_run',
            'dsh_client_trial_not_run',
        ],
        'notes': [
            'This is a local stdio protocol preflight, not a vendor-client trial.',
            'Codex, Hermes, and DSH must still connect through MCP configuration.',
            'Phase 6.3 expert gold and managed latency gates remain separate blockers.',
        ],
    }


def run_trial(*, timeout=DEFAULT_TIMEOUT_SECONDS, catalog=None):
    expected_version = catalog_version(Path(catalog).resolve()) if catalog is not None else None
    if catalog is not None and expected_version is None:
        raise ValueError('selected Catalog has no valid version registry')
    responses, tool_names, stderr_present, returncode = _invoke_server(
        timeout=timeout, catalog=catalog)
    summary = evaluate_responses(
        responses, tool_names, server_exit_code=returncode,
        expected_catalog_version=expected_version)
    summary['server_stderr_present'] = stderr_present
    summary['response_count'] = len(responses)
    return summary


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
        return str(resolved)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Phase 6.4 local MCP stdio conformance preflight')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument('--catalog', type=Path, help='Local Catalog directory passed to the server')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--report', metavar='PATH')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)
    if not 5 <= args.timeout <= 300:
        parser.error('--timeout must be between 5 and 300 seconds')
    try:
        summary = run_trial(timeout=args.timeout, catalog=args.catalog)
    except (OSError, ValueError, TimeoutError) as exc:
        summary = {
            'schema': REPORT_SCHEMA,
            'phase': '6.4-preflight',
            'date': date.today().isoformat(),
            'status': 'failed',
            'error_type': type(exc).__name__,
            'error': _safe_error(exc),
            'real_client_trial_complete': False,
            'release_ready': False,
        }
    if args.write:
        run_date = str(summary['date']).replace('-', '')
        report = Path(args.report) if args.report else (
            ROOT / 'docs' / ('PHASE6_4_LOCAL_CONFORMANCE_' + run_date + '.json'))
        write_report(summary, report)
        summary['report_file'] = _display_path(report)
        write_report(summary, report)
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('phase6_client_conformance=' + summary['status'])
        print('real_client_trial=' + str(
            summary.get('real_client_trial_complete', False)).lower())
        for check in summary.get('checks', ()):
            print(check['check'] + '=' + check['status'])
        if summary.get('error'):
            print('error=' + summary['error'])
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
