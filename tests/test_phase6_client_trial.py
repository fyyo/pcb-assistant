'''Offline Phase 6.4 stdio client preflight tests.'''
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import pcb_assistant.evaluation.stdio_trial as trial
from tests.phase6_test_fixtures import SyntheticCatalogTestCase


class Phase6ClientTrialTests(SyntheticCatalogTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.summary = trial.run_trial(catalog=cls.catalog)

    def test_local_stdio_preflight_covers_all_tools(self):
        self.assertEqual(self.summary['status'], 'passed')
        self.assertEqual(self.summary['response_count'], 4)
        self.assertEqual(len(self.summary['calls']), 1)
        self.assertTrue(all(
            call['status'] == 'passed' for call in self.summary['calls']))
        self.assertEqual(
            {call['tool'] for call in self.summary['calls']},
            {
                'pcb_answer',
            },
        )

    def test_preflight_is_not_a_real_vendor_client_trial(self):
        self.assertFalse(self.summary['client']['real_vendor_client'])
        self.assertFalse(self.summary['real_client_trial_complete'])
        self.assertFalse(self.summary['release_ready'])
        self.assertIn(
            'codex_client_trial_incomplete', self.summary['release_blockers'])

    def test_preflight_is_local_only_and_path_free(self):
        encoded = json.dumps(self.summary, ensure_ascii=False)
        self.assertFalse(self.summary['external_services'])
        self.assertNotRegex(encoded, r'(?i)\b[A-Z]:\\')
        self.assertNotRegex(encoded, r'sk-[A-Za-z0-9]{20,}')
        self.assertFalse(
            trial._contains_forbidden_key(self.summary))

    def test_missing_or_failed_responses_fail_closed(self):
        summary = trial.evaluate_responses([], [], server_exit_code=1)
        self.assertEqual(summary['status'], 'failed')
        checks = {item['check']: item['status'] for item in summary['checks']}
        self.assertEqual(checks['server_exit_code'], 'failed')
        self.assertEqual(checks['all_calls_returned'], 'failed')
        self.assertEqual(checks['answer_defaults_to_evidence_only'], 'failed')
        self.assertFalse(summary['release_ready'])

    def test_report_writer_uses_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            trial.write_report(self.summary, path)
            loaded = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(loaded['schema'], trial.REPORT_SCHEMA)
        self.assertEqual(loaded['status'], 'passed')

    def test_failure_details_do_not_echo_local_paths_or_secrets(self):
        text = trial._safe_error(
            RuntimeError('C:\\secret\\server.py sk-' + 'a' * 24))
        self.assertNotIn('C:\\secret', text)
        self.assertNotIn('sk-' + 'a' * 24, text)


if __name__ == '__main__':
    unittest.main()
