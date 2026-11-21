'''Repeatable Codex app-server preflight for Phase 6.4.

The runner uses an isolated ``CODEX_HOME`` and only touches the generated
temporary configuration. It starts the real Codex app-server, creates an
ephemeral read-only thread, discovers the configured MCP server, and calls every
read-only PCB tool through ``mcpServer/tool/call``.

This verifies the vendor tool bridge without a model turn. It is not a complete
Codex, Hermes, or DSH user trial and must not be treated as release acceptance.
'''
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from pcb_assistant.evaluation.stdio_trial import SAMPLE_DOC_ID, SAMPLE_EXCERPT_ID
from pcb_assistant.interfaces.contract import (
    CONTRACT_VERSION,
    ENVELOPE_SCHEMA,
    TOOL_SPECS,
    validate_instance,
)
from pcb_assistant.interfaces.mcp import SERVER_NAME, SERVER_VERSION


from pcb_assistant.paths import ROOT
SERVER = ROOT / 'phase6_mcp.py'
TMP_ROOT = ROOT / '.tmp_work'
REPORT_SCHEMA = 'pcb-phase6-codex-appserver-preflight-v1'
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_SERVER_NAME = 'pcb-preflight'
EXPECTED_TOOL_NAMES = frozenset({'pcb_answer'})
_LOCAL_PATH = re.compile(r'(?i)\b[A-Z]:\\[^\s"\']+|\\\\[^\\\s]+\\[^\s"\']+')
_SECRET = re.compile(r'\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b')
_FORBIDDEN_KEYS = {
    'source_path', 'path', 'file_path', 'filesystem_path', 'absolute_path',
    'local_path', 'artifact_path', 'storage_path', 'source_file', 'pst_path',
    'original_text', 'normalized_text',
}


class AppServerError(RuntimeError):
    '''A failed app-server request or protocol response.'''


def _check(name, passed, detail=None):
    return {
        'check': name,
        'status': 'passed' if passed else 'failed',
        'detail': detail,
    }


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


def resolve_codex(codex=None):
    '''Resolve the Codex executable without reading or changing its config.'''
    candidate = codex or os.environ.get('CODEX_BIN') or 'codex'
    path = Path(candidate).expanduser()
    if path.is_file():
        return path.resolve()
    found = shutil.which(candidate)
    if found:
        return Path(found).resolve()
    raise FileNotFoundError('Codex CLI executable was not found.')


def _run_codex(command, *, env, timeout=30):
    try:
        return subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError('Codex command timed out.') from exc


def _codex_version(codex, *, env):
    result = _run_codex([str(codex), '--version'], env=env)
    text = (result.stdout or result.stderr or '').strip()
    match = re.search(r'codex-cli\s+[0-9][^\s]*', text)
    return match.group(0) if match else 'unknown'


def _auth_status(codex, *, env):
    result = _run_codex([str(codex), 'login', 'status'], env=env)
    text = ((result.stdout or '') + '\n' + (result.stderr or '')).lower()
    if 'not logged in' in text:
        return 'not_logged_in'
    if 'logged in' in text:
        return 'logged_in'
    return 'unknown'


def _toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def build_config_toml(*, python, server, server_name=DEFAULT_SERVER_NAME):
    '''Build a temporary Codex config that points at the local MCP server.'''
    args = ['-X', 'utf8', '-B', str(server)]
    lines = [
        '[mcp_servers.' + server_name + ']',
        'command = ' + _toml_string(python),
        'args = [' + ', '.join(_toml_string(item) for item in args) + ']',
        '',
    ]
    return '\n'.join(lines)


def _tool_call_specs():
    return (('pcb_answer', {
        'question': '压合后板边出现白斑，应该怎么排查？', 'limit': 3,
    }),)


def _start_appserver(codex, *, env):
    process = subprocess.Popen(
        [str(codex), 'app-server', '--listen', 'stdio://'],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        bufsize=1,
    )
    stdout_queue = queue.Queue()
    stderr_lines = []

    def read_stdout():
        for line in process.stdout:
            stdout_queue.put(line)

    def read_stderr():
        for line in process.stderr:
            text = line.strip()
            if text:
                stderr_lines.append(text)
                if len(stderr_lines) > 200:
                    del stderr_lines[:100]

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()
    return process, stdout_queue, stderr_lines


def _send(process, payload):
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
    process.stdin.flush()


def _wait_for_response(process, stdout_queue, request_id, *, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = stdout_queue.get(timeout=1)
        except queue.Empty:
            if process.poll() is not None:
                raise AppServerError('Codex app-server exited before replying.')
            continue
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AppServerError('Codex app-server returned invalid JSON.') from exc
        if not isinstance(value, dict):
            continue
        if value.get('id') == request_id and 'method' not in value:
            return value
        if 'method' in value and 'id' in value:
            _send(process, {
                'jsonrpc': '2.0',
                'id': value['id'],
                'error': {
                    'code': -32601,
                    'message': 'Unsupported app-server request.',
                },
            })
    raise TimeoutError('Timed out waiting for Codex app-server response.')


def _request(process, stdout_queue, request_id, method, params, *, timeout):
    _send(process, {
        'jsonrpc': '2.0',
        'id': request_id,
        'method': method,
        'params': params,
    })
    response = _wait_for_response(
        process, stdout_queue, request_id, timeout=timeout)
    if response.get('error') is not None:
        message = response['error'].get('message')
        raise AppServerError('Codex app-server request failed: ' + str(message))
    result = response.get('result')
    return result if isinstance(result, dict) else {}


def _stop_appserver(process):
    if process is None:
        return
    if process.poll() is None:
        if os.name == 'nt':
            try:
                subprocess.run(
                    ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except (AttributeError, OSError, ValueError):
            pass
    time.sleep(0.2)


def _new_isolated_home():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(
        prefix='pcb-codex-preflight-', dir=str(TMP_ROOT)))


def _cleanup_isolated_home(path):
    for delay in (0, 0.5, 1.0, 2.0, 3.0):
        if not path.exists():
            return
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(path)
            return
        except OSError:
            continue
    shutil.rmtree(path, ignore_errors=True)


def _extract_envelope(call_result):
    if not isinstance(call_result, dict):
        return None
    structured = call_result.get('structuredContent')
    if isinstance(structured, dict):
        return structured
    content = call_result.get('content')
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                try:
                    value = json.loads(item.get('text', ''))
                except (TypeError, ValueError):
                    return None
                return value if isinstance(value, dict) else None
    return None


def evaluate_session(
        initialize_result, thread_start_result, status_result, call_results,
        *, codex_version, auth_status, server_stderr_present=False,
        isolated_codex_home=True):
    '''Evaluate one app-server discovery and tool-call session.'''
    status_data = (status_result.get('data') or [{}])[0]
    if not isinstance(status_data, dict):
        status_data = {}
    tools = status_data.get('tools')
    tools = tools if isinstance(tools, dict) else {}
    tool_names = sorted(tools)
    server_info = status_data.get('serverInfo')
    server_info = server_info if isinstance(server_info, dict) else {}
    thread = thread_start_result.get('thread')
    thread = thread if isinstance(thread, dict) else {}
    sandbox = thread_start_result.get('sandbox')
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    permission_profile = thread_start_result.get('activePermissionProfile')
    permission_profile = (
        permission_profile if isinstance(permission_profile, dict) else {}
    )

    calls = []
    all_calls_returned = True
    all_calls_ok = True
    all_envelopes_valid = True
    all_read_only = True
    all_local_only = True
    all_path_free = True
    answer_evidence_only = False
    returned_count = 0
    for tool_name in sorted(EXPECTED_TOOL_NAMES):
        call_result = call_results.get(tool_name)
        envelope = _extract_envelope(call_result)
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
        is_error = call_result.get('isError') if isinstance(call_result, dict) else None
        returned = isinstance(call_result, dict)
        call_ok = returned and is_error is False and envelope_valid and read_only
        call_ok = call_ok and local_only and path_free
        if tool_name == 'pcb_answer' and isinstance(envelope, dict):
            result = envelope.get('result')
            result = result if isinstance(result, dict) else {}
            answer_evidence_only = (
                result.get('answer_mode') != 'managed'
                and result.get('generated_by_model') is False
            )
        all_calls_returned = all_calls_returned and returned
        returned_count += int(returned)
        all_calls_ok = all_calls_ok and call_ok
        all_envelopes_valid = all_envelopes_valid and envelope_valid
        all_read_only = all_read_only and read_only
        all_local_only = all_local_only and local_only
        all_path_free = all_path_free and path_free
        calls.append({
            'tool': tool_name,
            'status': 'passed' if call_ok else 'failed',
            'envelope_status': (
                envelope.get('status') if isinstance(envelope, dict) else None
            ),
            'envelope_valid': envelope_valid,
            'read_only': read_only,
            'local_only': local_only,
            'path_free': path_free,
        })

    tools_match = set(tool_names) == EXPECTED_TOOL_NAMES
    server_identity = (
        server_info.get('name') == SERVER_NAME
        and server_info.get('version') == SERVER_VERSION
    )
    checks = [
        _check(
            'codex_app_server_initialize',
            bool(initialize_result),
            initialize_result.get('platformFamily') if initialize_result else None),
        _check('ephemeral_thread_created', thread.get('ephemeral') is True),
        _check(
            'thread_read_only',
            sandbox.get('type') == 'readOnly'
            and permission_profile.get('id') == ':read-only',
            {
                'sandbox': sandbox.get('type'),
                'permission_profile': permission_profile.get('id'),
            }),
        _check(
            'mcp_server_connected',
            status_data.get('runtimeStatus') == 'connected',
            status_data.get('runtimeStatus')),
        _check(
            'mcp_server_identity',
            server_identity,
            {
                'name': server_info.get('name'),
                'version': server_info.get('version'),
            }),
        _check('tools_error_absent', status_data.get('toolsError') is None),
        _check(
            'tools_list_count',
            len(tool_names) == len(EXPECTED_TOOL_NAMES),
            len(tool_names)),
        _check('tools_list_names_match', tools_match, tool_names),
        _check(
            'all_calls_returned',
            all_calls_returned,
            returned_count),
        _check('all_calls_ok', all_calls_ok),
        _check('all_envelopes_valid', all_envelopes_valid),
        _check('all_read_only', all_read_only),
        _check('all_local_only', all_local_only),
        _check('path_and_secret_free', all_path_free),
        _check(
            'answer_defaults_to_evidence_only',
            answer_evidence_only),
        _check('isolated_codex_home', isolated_codex_home is True),
    ]
    status = (
        'passed' if all(item['status'] == 'passed' for item in checks)
        else 'failed'
    )
    if auth_status == 'logged_in':
        codex_blocker = 'codex_model_turn_not_run'
    else:
        codex_blocker = 'codex_auth_missing'
    return {
        'schema': REPORT_SCHEMA,
        'phase': '6.4-codex-preflight',
        'date': date.today().isoformat(),
        'contract_version': CONTRACT_VERSION,
        'transport': 'stdio',
        'client': {
            'kind': 'codex_app_server',
            'real_vendor_client': True,
            'codex_version': codex_version,
            'auth_status': auth_status,
        },
        'server': {
            'name': server_info.get('name'),
            'version': server_info.get('version'),
        },
        'status': status,
        'checks': checks,
        'calls': calls,
        'tools': tool_names,
        'external_services': False,
        'data_egress': 'none',
        'isolated_codex_home': isolated_codex_home is True,
        'global_codex_config_modified': False,
        'server_stderr_present': bool(server_stderr_present),
        'codex_discovery_complete': status == 'passed',
        'codex_tool_call_complete': status == 'passed',
        'model_turn_complete': False,
        'real_client_trial_complete': False,
        'release_ready': False,
        'release_blockers': [
            'expert_gold_incomplete',
            'managed_p95_not_measured',
            'severe_unsupported_conclusions_not_reviewed',
            codex_blocker,
            'hermes_client_trial_not_run',
            'dsh_client_trial_not_run',
        ],
        'notes': [
            'This is a Codex app-server discovery and tool-call preflight.',
            'No model turn was run, and no Hermes or DSH client was exercised.',
            'Phase 6.3 expert gold and managed latency gates remain blockers.',
        ],
    }


def run_preflight(*, codex=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    '''Run the isolated Codex app-server preflight.'''
    executable = resolve_codex(codex)
    home_path = _new_isolated_home()
    try:
        config_path = home_path / 'config.toml'
        config_path.write_text(
            build_config_toml(
                python=sys.executable,
                server=SERVER,
                server_name=DEFAULT_SERVER_NAME,
            ),
            encoding='utf-8',
        )
        env = os.environ.copy()
        env['CODEX_HOME'] = str(home_path)
        version = _codex_version(executable, env=env)
        auth_status = _auth_status(executable, env=env)
        process = None
        stdout_queue = None
        stderr_lines = []
        try:
            process, stdout_queue, stderr_lines = _start_appserver(
                executable, env=env)
            initialize_result = _request(
                process, stdout_queue, 1, 'initialize', {
                    'clientInfo': {
                        'name': 'pcb-codex-preflight',
                        'version': '1.0.0',
                    },
                    'capabilities': {'experimentalApi': True},
                }, timeout=timeout)
            _send(process, {'jsonrpc': '2.0', 'method': 'initialized'})
            thread_start_result = _request(
                process, stdout_queue, 2, 'thread/start', {
                    'cwd': str(ROOT),
                    'ephemeral': True,
                }, timeout=timeout)
            thread = thread_start_result.get('thread')
            thread = thread if isinstance(thread, dict) else {}
            thread_id = thread.get('id')
            if not isinstance(thread_id, str) or not thread_id:
                raise AppServerError('Codex app-server did not return a thread ID.')
            status_result = _request(
                process, stdout_queue, 3, 'mcpServerStatus/list', {
                    'serverName': DEFAULT_SERVER_NAME,
                    'threadId': thread_id,
                    'detail': 'full',
                }, timeout=timeout)
            call_results = {}
            errors = {}
            for index, (tool_name, arguments) in enumerate(
                    _tool_call_specs(), start=101):
                try:
                    call_results[tool_name] = _request(
                        process, stdout_queue, index, 'mcpServer/tool/call', {
                            'server': DEFAULT_SERVER_NAME,
                            'threadId': thread_id,
                            'tool': tool_name,
                            'arguments': arguments,
                        }, timeout=timeout)
                except (AppServerError, TimeoutError) as exc:
                    errors[tool_name] = _safe_error(exc)
            summary = evaluate_session(
                initialize_result,
                thread_start_result,
                status_result,
                call_results,
                codex_version=version,
                auth_status=auth_status,
                server_stderr_present=bool(stderr_lines),
            )
            if errors:
                summary['call_errors'] = errors
            return summary
        finally:
            _stop_appserver(process)
    finally:
        _cleanup_isolated_home(home_path)


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


def _failure_summary(exc):
    return {
        'schema': REPORT_SCHEMA,
        'phase': '6.4-codex-preflight',
        'date': date.today().isoformat(),
        'status': 'failed',
        'error_type': type(exc).__name__,
        'error': _safe_error(exc),
        'client': {
            'kind': 'codex_app_server',
            'real_vendor_client': True,
        },
        'external_services': False,
        'data_egress': 'none',
        'real_client_trial_complete': False,
        'release_ready': False,
        'release_blockers': [
            'expert_gold_incomplete',
            'managed_p95_not_measured',
            'severe_unsupported_conclusions_not_reviewed',
            'codex_trial_failed_or_incomplete',
            'hermes_client_trial_not_run',
            'dsh_client_trial_not_run',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Phase 6.4 isolated Codex app-server preflight')
    parser.add_argument('--codex', metavar='PATH')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--report', metavar='PATH')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 600:
        parser.error('--timeout must be between 30 and 600 seconds')
    try:
        summary = run_preflight(codex=args.codex, timeout=args.timeout)
    except (OSError, ValueError, TimeoutError, AppServerError) as exc:
        summary = _failure_summary(exc)
    if args.write:
        run_date = str(summary['date']).replace('-', '')
        report = Path(args.report) if args.report else (
            ROOT / 'docs' / (
                'PHASE6_4_CODEX_APPSERVER_PREFLIGHT_' + run_date + '.json'))
        write_report(summary, report)
        summary['report_file'] = _display_path(report)
        write_report(summary, report)
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('phase6_codex_appserver_preflight=' + summary['status'])
        print('codex_tool_call_complete=' + str(
            summary.get('codex_tool_call_complete', False)).lower())
        print('real_client_trial=' + str(
            summary.get('real_client_trial_complete', False)).lower())
        for check in summary.get('checks', ()):
            print(check['check'] + '=' + check['status'])
        if summary.get('error'):
            print('error=' + summary['error'])
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
