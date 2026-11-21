'''Offline tests for the Phase 6 release-readiness audit.'''
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import pcb_assistant.evaluation.release as release


def _passed_eval_report():
    return {
        'schema': 'pcb-phase6-eval-run-v1',
        'dataset': {'cases': 150},
        'runtime': {'cases': 150},
        'thresholds': [
            {'metric': 'citation_validation_pass_rate',
             'value': 1.0, 'status': 'passed'},
            {'metric': 'invalid_exc_references',
             'value': 0, 'status': 'passed'},
            {'metric': 'no_evidence_precision',
             'value': 0.96, 'status': 'passed'},
            {'metric': 'evidence_recall_at_8',
             'value': 0.85, 'status': 'passed'},
            {'metric': 'required_field_coverage',
             'value': 0.83, 'status': 'passed'},
            {'metric': 'prompt_injection_pass_rate',
             'value': 1.0, 'status': 'passed'},
            {'metric': 'sensitive_egress_pass_rate',
             'value': 1.0, 'status': 'passed'},
            {'metric': 'managed_p95_seconds',
             'value': None, 'status': 'unavailable'},
            {'metric': 'evidence_p95_seconds',
             'value': 1.7, 'status': 'passed'},
            {'metric': 'severe_unsupported_conclusions',
             'value': None, 'status': 'unavailable'},
        ],
    }


def _codex_report():
    return {
        'schema': 'pcb-phase6-codex-appserver-preflight-v1',
        'status': 'passed',
        'client': {
            'real_vendor_client': True,
            'auth_status': 'logged_in',
        },
        'codex_tool_call_complete': True,
        'model_turn_complete': True,
        'tools': [{'name': 'pcb_answer'}],
        'calls': [
            {'tool': 'pcb_answer', 'status': 'passed'}
        ],
    }


def _inputs():
    return {
        'contract_result': {
            'status': 'passed',
            'counts': {'tools': 8, 'errors': 17, 'eval_cases': 150},
        },
        'annotation_result': {
            'status': 'passed',
            'exists': True,
            'cases': 150,
            'complete': 150,
        },
        'eval_report': _passed_eval_report(),
        'local_report': {
            'schema': 'pcb-phase6-client-conformance-v1',
            'status': 'passed',
        },
        'codex_report': _codex_report(),
        'managed_trial': {
            'schema': 'pcb-phase6-managed-trial-v1',
            'status': 'passed',
            'p95_seconds': 12.5,
            'provider_requests': 3,
            'data_egress': 'sanitized_evidence',
        },
        'severe_review': {
            'schema': 'pcb-phase6-severe-review-v1',
            'status': 'passed',
            'unsupported_conclusions': 0,
            'reviewed_answers': 120,
            'reviewer': 'expert-a',
            'reviewed_at': '2026-10-27T12:00:00+08:00',
        },
        'client_reports': {
            'hermes': {
                'schema': 'pcb-phase6-client-trial-v1',
                'status': 'passed',
                'real_client_trial_complete': True,
            },
            'dsh': {
                'schema': 'pcb-phase6-client-trial-v1',
                'status': 'passed',
                'real_client_trial_complete': True,
            },
        },
    }


class Phase6ReleaseTests(unittest.TestCase):
    def test_missing_evidence_is_fail_closed(self):
        summary = release.evaluate_readiness(
            contract_result={},
            annotation_result={},
            eval_report=None,
            local_report=None,
            codex_report=None,
        )
        self.assertFalse(summary['release_ready'])
        self.assertEqual(summary['status'], 'blocked')
        for blocker in (
                'contract_invalid',
                'local_eval_invalid',
                'expert_gold_incomplete',
                'managed_p95_not_measured',
                'severe_unsupported_conclusions_not_reviewed',
                'local_conformance_failed',
                'codex_tool_bridge_incomplete',
                'hermes_client_trial_not_run',
                'dsh_client_trial_not_run'):
            self.assertIn(blocker, summary['release_blockers'])

    def test_complete_synthetic_evidence_is_ready(self):
        summary = release.evaluate_readiness(**_inputs())
        self.assertTrue(summary['release_ready'], summary['checks'])
        self.assertEqual(summary['status'], 'ready')
        self.assertEqual(summary['release_blockers'], [])
        self.assertTrue(all(
            check['status'] == 'passed' for check in summary['checks']))

    def test_incomplete_gold_blocks_release(self):
        inputs = _inputs()
        inputs['annotation_result'] = {
            'status': 'in_progress',
            'exists': True,
            'cases': 150,
            'complete': 149,
        }
        summary = release.evaluate_readiness(**inputs)
        self.assertIn('expert_gold_incomplete', summary['release_blockers'])

    def test_codex_tool_bridge_does_not_imply_model_turn(self):
        inputs = _inputs()
        inputs['codex_report'] = copy.deepcopy(inputs['codex_report'])
        inputs['codex_report']['model_turn_complete'] = False
        inputs['codex_report']['client']['auth_status'] = 'not_logged_in'
        summary = release.evaluate_readiness(**inputs)
        self.assertIn('codex_model_turn_not_run', summary['release_blockers'])
        self.assertNotIn('codex_tool_bridge_incomplete',
                         summary['release_blockers'])

    def test_managed_p95_above_thirty_seconds_blocks_release(self):
        inputs = _inputs()
        inputs['managed_trial']['p95_seconds'] = 30.01
        summary = release.evaluate_readiness(**inputs)
        self.assertIn('managed_p95_not_measured', summary['release_blockers'])

    def test_malformed_counts_fail_closed_without_crashing(self):
        inputs = _inputs()
        inputs['annotation_result'].update({
            'cases': 'not-a-number',
            'complete': None,
        })
        inputs['managed_trial']['provider_requests'] = 'unknown'
        inputs['severe_review']['reviewed_answers'] = 'unknown'
        summary = release.evaluate_readiness(**inputs)
        self.assertFalse(summary['release_ready'])
        self.assertIn('expert_gold_incomplete', summary['release_blockers'])
        self.assertIn('managed_p95_not_measured', summary['release_blockers'])
        self.assertIn(
            'severe_unsupported_conclusions_not_reviewed',
            summary['release_blockers'])

    def test_severe_unsupported_conclusion_blocks_release(self):
        inputs = _inputs()
        inputs['severe_review']['unsupported_conclusions'] = 1
        summary = release.evaluate_readiness(**inputs)
        self.assertIn(
            'severe_unsupported_conclusions_not_reviewed',
            summary['release_blockers'])

    def test_local_metrics_cannot_substitute_for_independent_reviews(self):
        inputs = _inputs()
        inputs['managed_trial'] = None
        inputs['severe_review'] = None
        for item in inputs['eval_report']['thresholds']:
            if item['metric'] == 'managed_p95_seconds':
                item.update({'value': 5.0, 'status': 'passed'})
            if item['metric'] == 'severe_unsupported_conclusions':
                item.update({'value': 0, 'status': 'passed'})
        summary = release.evaluate_readiness(**inputs)
        self.assertIn('managed_p95_not_measured', summary['release_blockers'])
        self.assertIn(
            'severe_unsupported_conclusions_not_reviewed',
            summary['release_blockers'])

    def test_missing_hermes_or_dsh_trial_blocks_release(self):
        inputs = _inputs()
        inputs['client_reports'].pop('hermes')
        summary = release.evaluate_readiness(**inputs)
        self.assertIn('hermes_client_trial_not_run',
                      summary['release_blockers'])
        self.assertNotIn('dsh_client_trial_not_run',
                         summary['release_blockers'])

    def test_report_contains_no_local_path_or_secret(self):
        secret = 'sk-' + 'a' * 24
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'annotations.jsonl'
            summary = release.run_audit(
                annotations=missing,
                eval_report=missing,
                local_conformance=missing,
                codex_report=missing,
                managed_trial=missing,
                severe_review=missing,
                hermes_trial=missing,
                dsh_trial=missing,
            )
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotRegex(encoded, r'(?i)\b[A-Z]:\\')
        self.assertNotIn(secret, encoded)
        self.assertNotIn(directory, encoded)

    def test_require_ready_returns_nonzero_when_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'annotations.jsonl'
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = release.main([
                    '--annotations', str(missing),
                    '--eval-report', str(missing),
                    '--local-conformance', str(missing),
                    '--codex-report', str(missing),
                    '--managed-trial', str(missing),
                    '--severe-review', str(missing),
                    '--hermes-trial', str(missing),
                    '--dsh-trial', str(missing),
                    '--require-ready',
                ])
        self.assertEqual(code, 1)
        self.assertIn('release_ready=false', output.getvalue())

    def test_write_report_uses_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'readiness.json'
            with redirect_stdout(io.StringIO()):
                code = release.main([
                    '--write',
                    '--report', str(target),
                    '--annotations', str(target.with_name('missing.jsonl')),
                    '--eval-report', str(target.with_name('missing-eval.json')),
                ])
            self.assertEqual(code, 0)
            loaded = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(loaded['schema'], release.REPORT_SCHEMA)
        self.assertFalse(loaded['release_ready'])
        self.assertEqual(loaded['report_file'], 'readiness.json')


if __name__ == '__main__':
    unittest.main()
