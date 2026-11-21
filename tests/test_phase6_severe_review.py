'''Offline tests for the Phase 6.3 severe-conclusion review workflow.'''
from __future__ import annotations

import json
from contextlib import redirect_stdout
from datetime import timedelta
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_assistant.evaluation.annotation import case_fingerprint
import pcb_assistant.evaluation.review as review


BASE_CASE = {
    'case_id': 'EVAL_9001',
    'category': 'white_spot',
    'language': 'zh',
    'question': '压合后出现白斑，应该怎么排查？',
    'provenance': {},
    'sensitivity': 'internal',
}
FINGERPRINT = 'f' * 64
EXCERPT_ID = 'EXC_aaaaaaaaaaaaaaaaaaaaaaaa'


def pending_review():
    return {
        'status': 'pending',
        'reviewer': None,
        'reviewed_at': None,
        'unsupported_conclusions': None,
        'notes': '',
    }


def complete_review(unsupported=0, *, reviewer='expert-01',
                    reviewed_at='2026-10-27T12:00:00+08:00', notes=''):
    return {
        'status': 'complete',
        'reviewer': reviewer,
        'reviewed_at': reviewed_at,
        'unsupported_conclusions': unsupported,
        'notes': notes,
    }


def record(case_id='EVAL_9001', review_value=None):
    case = dict(BASE_CASE, case_id=case_id)
    return {
        'schema': review.RECORD_SCHEMA,
        'case_id': case_id,
        'case_fingerprint': case_fingerprint(case),
        'answer_fingerprint': FINGERPRINT,
        'category': case['category'],
        'question': case['question'],
        'answer': '现有证据只能提供排查线索。 [' + EXCERPT_ID + ']',
        'citations': [{
            'excerpt_id': EXCERPT_ID,
            'doc_id': 'KNOWLEDGE_test',
            'title': 'Public reference',
            'source_type': 'public_reference',
            'text': 'Evidence excerpt.',
            'evidence_status': 'published_reference_not_independently_verified',
        }],
        'review': pending_review() if review_value is None else review_value,
    }


class Phase6SevereReviewTests(unittest.TestCase):
    def test_prepare_packet_skips_unanswered_cases(self):
        first = dict(BASE_CASE)
        second = dict(BASE_CASE, case_id='EVAL_9002')

        def fake_runner(case, *, limit, catalog_directory):
            if case['case_id'] == second['case_id']:
                return None
            return record(case['case_id'])

        records, meta = review.prepare_packet(
            [first, second], workers=1, answer_runner=fake_runner)
        self.assertEqual(len(records), 1)
        self.assertEqual(meta['answered'], 1)
        self.assertEqual(meta['skipped'], 1)
        self.assertFalse(meta['external_services'])
        self.assertEqual(meta['data_egress'], 'none')

    def test_pending_record_is_valid_but_not_complete(self):
        records = [record()]
        self.assertEqual(review.validate_records(records), [])
        summary = review.review_summary(records)
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(summary['pending_answers'], 1)
        self.assertEqual(summary['reviewed_answers'], 0)
        self.assertIsNone(summary['unsupported_conclusions'])

    def test_complete_review_requires_timezone_and_notes_for_issues(self):
        bad_time = record(review_value=complete_review(
            reviewed_at='2026-10-27T12:00:00'))
        issues = review.validate_records([bad_time])
        self.assertTrue(any('timezone' in item for item in issues))

        missing_notes = record(review_value=complete_review(
            unsupported=1, notes=''))
        issues = review.validate_records([missing_notes])
        self.assertTrue(any('notes are required' in item for item in issues))

        bad_count = record(review_value=complete_review(unsupported=True))
        issues = review.validate_records([bad_count])
        self.assertTrue(any('non-negative integer' in item for item in issues))

    def test_summary_passes_only_when_all_records_are_good(self):
        first = record('EVAL_9001', complete_review(0))
        second = record('EVAL_9002', complete_review(0))
        summary = review.review_summary([first, second])
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['reviewed_answers'], 2)
        self.assertEqual(summary['unsupported_conclusions'], 0)
        self.assertEqual(summary['reviewer'], 'expert-01')

        pending = record('EVAL_9002')
        blocked = review.review_summary([first, pending])
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['pending_answers'], 1)
        self.assertIsNone(blocked['unsupported_conclusions'])

        unsupported = record(
            'EVAL_9002', complete_review(1, notes='Need stronger support.'))
        blocked = review.review_summary([first, unsupported])
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['blocked_reason'],
                         'unsupported_conclusions_present')
        self.assertEqual(blocked['unsupported_conclusions'], 1)

    def test_invalid_review_cannot_pass_even_with_zero_count(self):
        invalid = record('EVAL_9001', complete_review(0, reviewer=''))
        issues = review.validate_records([invalid])
        summary = review.review_summary(
            [invalid], validation_issues=issues)
        self.assertTrue(issues)
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(summary['blocked_reason'], 'review_record_invalid')

    def test_report_excludes_question_answer_and_local_paths(self):
        summary = review.review_summary([
            record('EVAL_9001', complete_review(0)),
            record('EVAL_9002', complete_review(0)),
        ])
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn('压合后出现白斑', encoded)
        self.assertNotIn('现有证据', encoded)
        self.assertNotIn('C:\\', encoded)
        self.assertNotIn(EXCERPT_ID, encoded)

    def test_records_round_trip_and_refuse_accidental_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'packet.jsonl'
            written = review.write_records([record()], target)
            self.assertEqual(written, target)
            self.assertEqual(review.load_records(target)[0]['case_id'], 'EVAL_9001')
            with self.assertRaises(FileExistsError):
                review.write_records([record()], target)

    def test_default_status_falls_back_to_latest_existing_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory)
            previous_date = review.date.today() - timedelta(days=1)
            previous_packet = (
                private_root / previous_date.strftime('%Y%m%d')
                / 'packet.jsonl')
            review.write_records([record()], previous_packet)
            with patch.object(review, 'PRIVATE_ROOT', private_root):
                self.assertEqual(
                    review._default_status_packet_path(), previous_packet)

                current_packet = review._default_packet_path()
                review.write_records([record()], current_packet)
                self.assertEqual(
                    review._default_status_packet_path(), current_packet)

    def test_status_cli_uses_latest_packet_when_today_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory)
            previous_date = review.date.today() - timedelta(days=1)
            packet = (
                private_root / previous_date.strftime('%Y%m%d')
                / 'packet.jsonl')
            review.write_records([record()], packet)
            output = io.StringIO()
            with patch.object(review, 'PRIVATE_ROOT', private_root), \
                    patch.object(review, 'load_cases', return_value=[BASE_CASE]), \
                    redirect_stdout(output):
                exit_code = review.main(['--status'])
            self.assertEqual(exit_code, 0)
            self.assertIn('phase6_severe_review=blocked', output.getvalue())
            self.assertIn('total_answers=1', output.getvalue())

    def test_stale_case_fingerprint_is_rejected_against_seed(self):
        stale = record()
        stale['case_fingerprint'] = '0' * 64
        case = dict(BASE_CASE)
        issues = review.validate_records([stale], [case])
        self.assertTrue(any('stale' in item for item in issues))


if __name__ == '__main__':
    unittest.main()
