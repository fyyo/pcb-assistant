'''Offline tests for the isolated Codex app-server preflight.'''
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

import phase6_codex_preflight as preflight


def _envelope(tool_name):
    answer_result = {}
    if tool_name == 'pcb_answer':
        answer_result = {
            'answer_mode': 'evidence_only',
            'generated_by_model': False,
        }
    return {
        'contract_version': 'pcb-agent-v1',
        'request_id': 'REQ_test',
        'status': 'answered' if tool_name == 'pcb_answer' else 'ok',
        'read_only': True,
        'external_services': False,
        'data_egress': 'none',
        'catalog_version': 'VER_' + 'A' * 24,
        'warnings': [],
        'errors': [],
        'result': answer_result,
    }


def _call_result(tool_name):
    envelope = _envelope(tool_name)
    return {
        'content': [{
            'type': 'text',
            'text': json.dumps(envelope, ensure_ascii=False),
        }],
        'structuredContent': envelope,
        'isError': False,
    }


def _status_result(tool_names):
    return {
        'data': [{
            'name': 'pcb-preflight',
            'runtimeStatus': 'connected',
            'serverInfo': {
                'name': 'pcb-knowledge',
                'version': preflight.SERVER_VERSION,
            },
            'tools': {name: {'name': name} for name in tool_names},
            'toolsError': None,
        }],
    }


def _thread_result():
    return {
        'thread': {'id': 'thread-test', 'ephemeral': True},
        'sandbox': {'type': 'readOnly'},
        'activePermissionProfile': {'id': ':read-only'},
    }


def _valid_session():
    names = sorted(preflight.EXPECTED_TOOL_NAMES)
    return preflight.evaluate_session(
        {'platformFamily': 'windows'},
        _thread_result(),
        _status_result(names),
        {name: _call_result(name) for name in names},
        codex_version='codex-cli 0.164.0',
        auth_status='not_logged_in',
        server_stderr_present=False,
    )


class Phase6CodexPreflightTests(unittest.TestCase):
    def test_expected_tools_match_contract(self):
        self.assertEqual(preflight.EXPECTED_TOOL_NAMES, {
            'pcb_answer',
        })
        self.assertEqual(
            {name for name, _arguments in preflight._tool_call_specs()},
            preflight.EXPECTED_TOOL_NAMES,
        )

    def test_valid_session_passes_discovery_and_calls(self):
        summary = _valid_session()
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(len(summary['calls']), 1)
        self.assertTrue(all(
            call['status'] == 'passed' for call in summary['calls']))
        self.assertTrue(summary['client']['real_vendor_client'])
        self.assertTrue(summary['codex_discovery_complete'])
        self.assertTrue(summary['codex_tool_call_complete'])
        self.assertFalse(summary['model_turn_complete'])
        self.assertFalse(summary['real_client_trial_complete'])
        self.assertFalse(summary['release_ready'])
        self.assertIn('codex_auth_missing', summary['release_blockers'])

    def test_report_is_local_only_and_path_free(self):
        summary = _valid_session()
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertFalse(summary['external_services'])
        self.assertEqual(summary['data_egress'], 'none')
        self.assertFalse(summary['global_codex_config_modified'])
        self.assertNotRegex(encoded, r'(?i)\b[A-Z]:\\')
        self.assertNotRegex(encoded, r'sk-[A-Za-z0-9]{20,}')
        self.assertFalse(preflight._contains_forbidden_key(summary))

    def test_missing_tool_call_fails_closed(self):
        names = sorted(preflight.EXPECTED_TOOL_NAMES)
        calls = {name: _call_result(name) for name in names}
        calls.pop('pcb_answer')
        summary = preflight.evaluate_session(
            {'platformFamily': 'windows'},
            _thread_result(),
            _status_result(names),
            calls,
            codex_version='codex-cli 0.164.0',
            auth_status='logged_in',
        )
        self.assertEqual(summary['status'], 'failed')
        checks = {item['check']: item['status'] for item in summary['checks']}
        self.assertEqual(checks['all_calls_returned'], 'failed')
        self.assertEqual(checks['all_calls_ok'], 'failed')
        self.assertIn('codex_model_turn_not_run', summary['release_blockers'])

    def test_managed_answer_does_not_satisfy_evidence_only_gate(self):
        names = sorted(preflight.EXPECTED_TOOL_NAMES)
        calls = {name: _call_result(name) for name in names}
        calls['pcb_answer']['structuredContent']['result'] = {
            'answer_mode': 'managed',
            'generated_by_model': True,
        }
        summary = preflight.evaluate_session(
            {'platformFamily': 'windows'},
            _thread_result(),
            _status_result(names),
            calls,
            codex_version='codex-cli 0.164.0',
            auth_status='logged_in',
        )
        self.assertEqual(summary['status'], 'failed')
        checks = {item['check']: item['status'] for item in summary['checks']}
        self.assertEqual(
            checks['answer_defaults_to_evidence_only'], 'failed')

    def test_invalid_envelope_and_wrong_tool_set_fail(self):
        names = sorted(preflight.EXPECTED_TOOL_NAMES)
        calls = {name: _call_result(name) for name in names}
        calls['pcb_answer']['structuredContent'] = {'read_only': True}
        summary = preflight.evaluate_session(
            {'platformFamily': 'windows'},
            _thread_result(),
            _status_result(list(preflight.EXPECTED_TOOL_NAMES)),
            calls,
            codex_version='codex-cli 0.164.0',
            auth_status='logged_in',
        )
        self.assertEqual(summary['status'], 'failed')
        checks = {item['check']: item['status'] for item in summary['checks']}
        self.assertEqual(checks['all_envelopes_valid'], 'failed')
        self.assertEqual(checks['tools_list_count'], 'passed')

    def test_temporary_config_is_valid_toml_and_isolated(self):
        config = preflight.build_config_toml(
            python=sys.executable,
            server=Path(sys.executable).with_name('phase6_mcp.py'),
        )
        parsed = tomllib.loads(config)
        server = parsed['mcp_servers']['pcb-preflight']
        self.assertEqual(server['command'], sys.executable)
        self.assertEqual(server['args'][0:2], ['-X', 'utf8'])
        self.assertIn('phase6_mcp.py', server['args'][-1])

    def test_safe_error_redacts_paths_and_secrets(self):
        text = preflight._safe_error(
            RuntimeError('C:\\secret\\codex.exe sk-' + 'a' * 24))
        self.assertNotIn('C:\\secret', text)
        self.assertNotIn('sk-' + 'a' * 24, text)

    def test_unresolvable_codex_is_reported_without_path_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / 'missing-codex.exe')
            with self.assertRaises(FileNotFoundError):
                preflight.resolve_codex(missing)

    @unittest.skipUnless(
        os.environ.get('PCB_RUN_CODEX_PREFLIGHT') == '1',
        'set PCB_RUN_CODEX_PREFLIGHT=1 to run the live Codex preflight',
    )
    def test_live_codex_appserver_tool_bridge(self):
        summary = preflight.run_preflight(timeout=180)
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(len(summary['tools']), 1)
        self.assertTrue(summary['codex_tool_call_complete'])
        self.assertFalse(summary['real_client_trial_complete'])


if __name__ == '__main__':
    unittest.main()
