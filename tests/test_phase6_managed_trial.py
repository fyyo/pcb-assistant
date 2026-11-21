'''Offline tests for the explicit Phase 6.2 managed trial runner.'''
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import pcb_assistant.interfaces.managed as phase6_managed
import pcb_assistant.evaluation.managed_trial as trial
from tests.phase6_test_fixtures import SyntheticCatalogTestCase


ENV = {
    'PCB_ALLOW_CLOUD_EGRESS': 'true',
    'PCB_LLM_PROVIDER': 'openai_responses',
    'PCB_LLM_MODEL': 'test-model',
    'OPENAI_API_KEY': 'test-secret-key-which-must-not-be-logged',
}


class FakeProvider:
    def __init__(self, config):
        self.config = config

    def answer(self, prompt_data):
        citation = prompt_data['evidence'][0]['excerpt_id']
        return {
            'structured_answer': {
                'status': 'answered',
                'assessment': 'hypothesis',
                'summary': '现有摘录只能提供排查线索，不能确认根因。',
                'summary_citation_ids': [citation],
                'possible_causes': [{
                    'cause': '材料或过程条件需要结合本批记录核对。',
                    'citation_ids': [citation],
                }],
                'checks': [{
                    'step': '对照本批材料与压合记录。',
                    'purpose': '确认现象是否与已知线索相符。',
                    'citation_ids': [citation],
                }],
                'missing_information': ['材料批次与压合曲线'],
                'limitations': ['摘录不构成现场验证结果。'],
            },
            'usage': {
                'input_tokens': 10,
                'output_tokens': 5,
                'total_tokens': 15,
            },
        }


def _pass_row(latency_ms=250):
    return {
        'trial_id': 'T01',
        'status': 'answered',
        'provider_request_attempted': True,
        'generated_by_model': True,
        'citation_validation': 'passed',
        'fallback_reason': None,
        'external_services': True,
        'data_egress': 'sanitized_evidence',
        'latency_ms': latency_ms,
        'passed': True,
    }


class Phase6ManagedTrialTests(SyntheticCatalogTestCase):
    def test_preflight_is_local_and_fail_closed(self):
        def forbidden_loader():
            raise AssertionError('privacy models must not load before opt-in')

        report = trial.preflight(
            environ={}, sanitizer_loader=forbidden_loader)
        self.assertFalse(report['ready'])
        self.assertFalse(report['external_services'])
        self.assertEqual(report['data_egress'], 'none')
        self.assertEqual(
            [item['reason'] for item in report['checks'][:2]],
            ['cloud_disabled', 'cloud_not_configured'])

    def test_run_trial_stops_before_provider_without_opt_in(self):
        def forbidden_provider(_config):
            raise AssertionError('provider must not be constructed')

        report = trial.run_trial(
            [trial.DEFAULT_QUESTIONS[0]],
            environ={},
            provider_factory=forbidden_provider,
            sanitizer_loader=lambda: None,
        )
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['provider_requests'], 0)
        self.assertFalse(report['external_services'])
        self.assertEqual(report['data_egress'], 'none')
        self.assertEqual(report['blocked_reason'], 'cloud_disabled')

    def test_report_passes_only_for_complete_cited_requests(self):
        report = trial.build_report(
            [_pass_row()], provider='openai_responses', model='test-model')
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['provider_requests'], 1)
        self.assertEqual(report['successful_requests'], 1)
        self.assertEqual(report['data_egress'], 'sanitized_evidence')
        self.assertEqual(report['p95_seconds'], 0.25)
        self.assertIsNone(report['release_blocker'])

    def test_fallback_and_slow_trials_are_blocked(self):
        fallback = _pass_row()
        fallback.update({
            'status': 'fallback',
            'generated_by_model': False,
            'citation_validation': None,
            'fallback_reason': 'provider_unavailable',
            'passed': False,
        })
        report = trial.build_report([fallback])
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['fallback_requests'], 1)
        self.assertEqual(report['release_blocker'], 'managed_p95_not_measured')

        slow = trial.build_report([_pass_row(30001)])
        self.assertEqual(slow['status'], 'blocked')
        self.assertGreater(slow['p95_seconds'], trial.MAX_P95_SECONDS)

        empty = trial.build_report([])
        self.assertEqual(empty['status'], 'blocked')
        self.assertEqual(empty['provider_requests'], 0)

    def test_fake_managed_run_is_cited_and_content_free(self):
        ticks = iter([10.0, 10.25])
        with redirect_stderr(io.StringIO()):
            report = trial.run_trial(
                [trial.DEFAULT_QUESTIONS[0]],
                environ=ENV,
                provider_factory=FakeProvider,
                sanitizer_loader=lambda: None,
                clock=lambda: next(ticks),
            )
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['provider_requests'], 1)
        self.assertEqual(report['successful_requests'], 1)
        self.assertEqual(report['fallback_requests'], 0)
        self.assertEqual(report['data_egress'], 'sanitized_evidence')
        self.assertEqual(report['results'][0]['citation_validation'], 'passed')
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(ENV['OPENAI_API_KEY'], encoded)
        self.assertNotIn(trial.DEFAULT_QUESTIONS[0], encoded)
        self.assertNotIn('压合', encoded)
        self.assertNotIn('C:\\', encoded)

    def test_read_questions_skips_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'questions.txt'
            source.write_text(
                '# comment\n\n真空度如何影响压合？\n温度曲线如何核查？\n',
                encoding='utf-8')
            self.assertEqual(trial.read_questions(source), [
                '真空度如何影响压合？',
                '温度曲线如何核查？',
            ])

    def test_write_report_is_utf8_json(self):
        report = trial.build_report([_pass_row()])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            written = trial.write_report(report, target)
            self.assertEqual(written, target)
            self.assertEqual(
                json.loads(target.read_text(encoding='utf-8'))['schema'],
                trial.SCHEMA)

    def test_cli_write_requires_execute(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                trial.main(['--write'])

    def test_cli_preflight_returns_zero_without_execute(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = trial.main(['--json'])
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value['requested_action'], 'preflight_only')
        self.assertFalse(value['external_services'])


if __name__ == '__main__':
    unittest.main()
