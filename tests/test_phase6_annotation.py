'''Offline tests for versioned Phase 6.3 expert annotations.'''
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import phase6_annotation as annotation
from phase6_contract import load_cases, validate_eval_cases


DOC_ID = 'MAIL_' + '1' * 32
SECOND_DOC_ID = 'CASE_' + '2' * 32
EXCERPT_ID = 'EXC_' + '3' * 24
SECOND_EXCERPT_ID = 'EXC_' + '4' * 24


def complete_record(case):
    record = annotation.template_record(case)
    record['expected'].update({
        'status': 'answered',
        'should_answer': True,
        'relevant_doc_ids': [DOC_ID, SECOND_DOC_ID],
        'required_evidence_ids': [EXCERPT_ID, SECOND_EXCERPT_ID],
        'required_fields': ['phenomenon', 'conditions'],
        'acceptable_points': ['先核对材料和压合曲线，再安排切片复核。'],
        'forbidden_claims': ['不得在未有切片证据时断言唯一根因。'],
        'notes': 'Test-only expert packet.',
    })
    record['annotation'] = {
        'status': 'complete',
        'reviewer': 'test-reviewer',
        'reviewed_at': '2026-10-27T10:00:00+08:00',
    }
    return record


class Phase6AnnotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()
        cls.normal_case = cls.cases[0]
        cls.injection_case = next(
            case for case in cls.cases
            if case['category'] == 'prompt_injection')

    def test_template_is_pending_and_fingerprinted(self):
        records = annotation.build_template(self.cases[:3])
        self.assertEqual(len(records), 3)
        self.assertTrue(all(
            record['annotation']['status'] == 'pending' for record in records))
        for record, case in zip(records, self.cases):
            self.assertEqual(
                record['case_fingerprint'], annotation.case_fingerprint(case))
            self.assertEqual(record['expected'], case['expected'])
        self.assertEqual(annotation.validate_records(records, self.cases), [])

    def test_partial_complete_annotation_is_merged_only_when_complete(self):
        record = complete_record(self.normal_case)
        pending = annotation.template_record(self.cases[1])
        records = [record, pending]
        self.assertEqual(
            annotation.validate_records(records, self.cases), [])
        merged = annotation.merge_annotations(self.cases[:2], records)
        self.assertEqual(
            merged[0]['annotation']['reviewer'], 'test-reviewer')
        self.assertEqual(
            merged[0]['expected']['required_evidence_ids'], [EXCERPT_ID, SECOND_EXCERPT_ID])
        self.assertEqual(
            merged[1]['annotation']['status'], 'pending')
        self.assertEqual(merged[1]['expected'], self.cases[1]['expected'])

    def test_require_complete_blocks_partial_gold(self):
        records = [complete_record(self.normal_case)]
        issues = annotation.validate_records(
            records, self.cases, require_complete=True)
        self.assertTrue(any('incomplete gold' in issue for issue in issues))

    def test_stale_fingerprint_is_rejected(self):
        record = complete_record(self.normal_case)
        record['case_fingerprint'] = '0' * 64
        issues = annotation.validate_records([record], self.cases)
        self.assertTrue(any('stale' in issue for issue in issues))

    def test_complete_answered_requires_reviewer_ids_points_and_claims(self):
        record = complete_record(self.normal_case)
        record['annotation']['reviewer'] = None
        record['expected']['required_evidence_ids'] = []
        record['expected']['acceptable_points'] = []
        record['expected']['forbidden_claims'] = []
        issues = annotation.validate_records([record], self.cases)
        self.assertTrue(any('reviewer' in issue for issue in issues))
        self.assertTrue(any('required_evidence_ids' in issue for issue in issues))
        self.assertTrue(any('acceptable_points' in issue for issue in issues))
        self.assertTrue(any('forbidden_claims' in issue for issue in issues))

    def test_complete_requires_timezone_aware_timestamp(self):
        record = complete_record(self.normal_case)
        record['annotation']['reviewed_at'] = '2026-10-27T10:00:00'
        issues = annotation.validate_records([record], self.cases)
        self.assertTrue(any('ISO 8601 with timezone' in issue for issue in issues))

    def test_prompt_injection_gold_cannot_be_reclassified_as_answered(self):
        record = annotation.template_record(self.injection_case)
        record['expected'].update({
            'status': 'answered',
            'should_answer': True,
            'required_fields': ['analysis'],
            'acceptable_points': ['不得泄露。'],
        })
        record['annotation'] = {
            'status': 'complete',
            'reviewer': 'test-reviewer',
            'reviewed_at': '2026-10-27T10:00:00+08:00',
        }
        issues = annotation.validate_records([record], self.cases)
        self.assertTrue(any('prompt-injection' in issue for issue in issues))

    def test_non_answered_gold_cannot_carry_evidence_ids(self):
        record = annotation.template_record(self.injection_case)
        record['expected']['required_evidence_ids'] = [EXCERPT_ID]
        record['annotation'] = {
            'status': 'complete',
            'reviewer': 'test-reviewer',
            'reviewed_at': '2026-10-27T10:00:00+08:00',
        }
        issues = annotation.validate_records([record], self.cases)
        self.assertTrue(any('must be empty' in issue for issue in issues))

    def test_unknown_and_duplicate_case_ids_are_rejected(self):
        first = complete_record(self.normal_case)
        duplicate = copy.deepcopy(first)
        unknown = copy.deepcopy(first)
        unknown['case_id'] = 'EVAL_9999'
        issues = annotation.validate_records(
            [first, duplicate, unknown], self.cases)
        self.assertTrue(any('duplicate case_id' in issue for issue in issues))
        self.assertTrue(any('unknown case_id' in issue for issue in issues))

    def test_load_annotated_cases_preserves_seed_when_file_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'annotations.jsonl'
            cases, state = annotation.load_annotated_cases(missing)
        self.assertEqual(cases, self.cases)
        self.assertFalse(state['exists'])
        self.assertFalse(state.get('complete', 0))

    def test_jsonl_round_trip_and_publish_is_atomic(self):
        record = complete_record(self.normal_case)
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / 'draft.jsonl'
            target = Path(directory) / 'published.jsonl'
            annotation.write_records([record], draft)
            self.assertEqual(annotation.load_records(draft), [record])
            annotation.publish_records([record], path=target)
            self.assertEqual(annotation.load_records(target), [record])
            with self.assertRaises(FileExistsError):
                annotation.publish_records([record], path=target)
            self.assertIn(
                'Test-only expert packet.',
                json.dumps(annotation.load_records(target), ensure_ascii=False))

    def test_validation_of_merged_annotated_cases_stays_schema_valid(self):
        record = complete_record(self.normal_case)
        merged = annotation.merge_annotations(self.cases, [record])
        self.assertEqual(
            validate_eval_cases(merged, require_pending=False), [])

    def test_cli_require_complete_fails_when_sidecar_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'annotations.jsonl'
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = annotation.main([
                    '--status', str(missing), '--require-complete', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('annotations file is missing', output.getvalue())

    def test_cli_template_and_validate_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / 'annotations.jsonl'
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    annotation.main(['--template', str(draft)]), 0)
                self.assertEqual(
                    annotation.main(['--validate', str(draft)]), 0)
            self.assertEqual(len(annotation.load_records(draft)), 150)

    def test_cli_reports_malformed_jsonl_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / 'broken.jsonl'
            draft.write_text('{not-json}\n', encoding='utf-8')
            output = io.StringIO()
            with redirect_stdout(output):
                code = annotation.main(['--validate', str(draft), '--json'])
        self.assertEqual(code, 1)
        self.assertIn('"status": "failed"', output.getvalue())


if __name__ == '__main__':
    unittest.main()
