'''Offline tests for the five-question internal-pilot review workflow.'''
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from phase6_annotation import case_fingerprint
import scripts.evaluation.internal_pilot_review as review


EXCERPT_A = 'EXC_aaaaaaaaaaaaaaaaaaaaaaaa'
EXCERPT_B = 'EXC_bbbbbbbbbbbbbbbbbbbbbbbb'
ANSWER_FINGERPRINT = 'f' * 64


def case(case_id='PILOT_TEST_01', question='压合后出现白斑，应该怎么排查？'):
    return {
        'case_id': case_id,
        'category': 'white_spot',
        'language': 'zh',
        'question': question,
        'provenance': {'source': 'test'},
        'sensitivity': 'internal',
    }


def record(source_case=None):
    source_case = case() if source_case is None else source_case
    citations = [
        {
            'excerpt_id': EXCERPT_A,
            'doc_id': 'KNOWLEDGE_test_a',
            'title': 'Reference A',
            'source_type': 'public_reference',
            'text': 'Evidence A.',
            'evidence_status': 'published_reference_not_independently_verified',
        },
        {
            'excerpt_id': EXCERPT_B,
            'doc_id': 'KNOWLEDGE_test_b',
            'title': 'Reference B',
            'source_type': 'public_reference',
            'text': 'Evidence B.',
            'evidence_status': 'published_reference_not_independently_verified',
        },
    ]
    return {
        'schema': review.RECORD_SCHEMA,
        'case_id': source_case['case_id'],
        'case_fingerprint': case_fingerprint(source_case),
        'answer_fingerprint': ANSWER_FINGERPRINT,
        'category': source_case['category'],
        'question': source_case['question'],
        'answer': '仅提供排查线索。 [' + EXCERPT_A + ']',
        'citations': citations,
        'review': review.pending_review(citations),
    }


def complete(record_value, *, support='direct', unsupported=0,
             missing_trusted=False, clarification=True):
    value = record_value['review']
    value['status'] = 'complete'
    value['reviewer'] = 'expert-test'
    value['reviewed_at'] = '2026-10-29T10:00:00+08:00'
    value['unsupported_conclusions'] = unsupported
    value['missing_trusted_references'] = missing_trusted
    value['clarification_adequate'] = clarification
    value['notes'] = 'limitation recorded' if missing_trusted or unsupported else ''
    for row in value['citation_reviews']:
        row['context_checked'] = True
        row['support'] = support
        row['notes'] = '' if support == 'direct' else 'context reviewed'
    return record_value


class InternalPilotReviewTests(unittest.TestCase):
    def test_default_cases_file_has_five_unique_questions(self):
        cases = review.load_cases()
        self.assertEqual(len(cases), 5)
        self.assertEqual(len({item['case_id'] for item in cases}), 5)
        self.assertEqual(len({item['question'] for item in cases}), 5)

    def test_prepare_packet_skips_unanswered_cases(self):
        first = case('PILOT_TEST_01')
        second = case('PILOT_TEST_02', '多层板压合后分层怎么排查？')

        def fake_runner(source_case, *, limit, catalog_directory):
            if source_case['case_id'] == second['case_id']:
                return None
            return record(source_case)

        records, meta = review.prepare_review_packet(
            [first, second], workers=1, answer_runner=fake_runner)
        self.assertEqual(len(records), 1)
        self.assertEqual(meta['answered'], 1)
        self.assertEqual(meta['skipped'], 1)
        self.assertEqual(meta['data_egress'], 'none')

    def test_pending_packet_is_valid_and_does_not_block_usage(self):
        records = [record(), record(case('PILOT_TEST_02', '分层怎么排查？'))]
        self.assertEqual(review.validate_records(records), [])
        summary = review.review_summary(records)
        self.assertEqual(summary['status'], 'awaiting_optional_review')
        self.assertEqual(summary['review_state'], 'awaiting_optional_review')
        self.assertFalse(summary['usage_blocked'])
        self.assertFalse(summary['quality_claim_ready'])
        self.assertIn('review_incomplete', summary['quality_blocked_reasons'])
        self.assertEqual(summary['pending_cases'], 2)
        self.assertIsNone(summary['unsupported_conclusions'])

        mixed = review.review_summary([
            complete(record()),
            record(case('PILOT_TEST_02', '分层怎么排查？')),
        ])
        self.assertEqual(mixed['status'], 'in_progress')
        self.assertEqual(mixed['review_state'], 'in_progress')
        self.assertFalse(mixed['usage_blocked'])

    def test_complete_review_must_cover_every_citation_once(self):
        value = complete(record())
        self.assertEqual(review.validate_records([value]), [])

        missing = complete(record())
        missing['review']['citation_reviews'].pop()
        issues = review.validate_records([missing])
        self.assertTrue(any('match citations in order' in item for item in issues))

        extra = complete(record())
        extra['review']['citation_reviews'].append({
            'excerpt_id': 'EXC_cccccccccccccccccccccccc',
            'context_checked': True,
            'support': 'direct',
            'notes': '',
        })
        issues = review.validate_records([extra])
        self.assertTrue(any('match citations in order' in item for item in issues))

        unchecked = complete(record())
        unchecked['review']['citation_reviews'][0]['context_checked'] = False
        issues = review.validate_records([unchecked])
        self.assertTrue(any('context_checked' in item for item in issues))

    def test_summary_passes_only_when_all_reviews_are_clear(self):
        first = complete(record())
        second = complete(record(case('PILOT_TEST_02', '分层怎么排查？')))
        summary = review.review_summary([first, second])
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['reviewed_cases'], 2)
        self.assertEqual(summary['citation_reviews_checked'], 4)
        self.assertEqual(summary['citation_reviews_total'], 4)
        self.assertEqual(summary['unsupported_conclusions'], 0)
        self.assertEqual(summary['unsupported_citations'], 0)
        self.assertFalse(summary['usage_blocked'])
        self.assertTrue(summary['quality_claim_ready'])
        self.assertEqual(summary['quality_blocked_reasons'], [])

    def test_unsupported_findings_block_only_the_quality_claim(self):
        unsupported_citation = complete(record(), support='unsupported')
        self.assertEqual(review.validate_records([unsupported_citation]), [])
        summary = review.review_summary([unsupported_citation])
        self.assertEqual(summary['status'], 'reviewed_with_findings')
        self.assertFalse(summary['usage_blocked'])
        self.assertFalse(summary['quality_claim_ready'])
        self.assertIn('unsupported_citations_present',
                      summary['quality_blocked_reasons'])

        unsupported_conclusion = complete(record(), unsupported=1)
        summary = review.review_summary([unsupported_conclusion])
        self.assertEqual(summary['status'], 'reviewed_with_findings')
        self.assertFalse(summary['usage_blocked'])
        self.assertIn('unsupported_conclusions_present',
                      summary['quality_blocked_reasons'])

        inadequate = complete(record(), clarification=False)
        summary = review.review_summary([inadequate])
        self.assertEqual(summary['status'], 'reviewed_with_findings')
        self.assertFalse(summary['usage_blocked'])
        self.assertIn('clarification_inadequate',
                      summary['quality_blocked_reasons'])

    def test_report_excludes_question_answer_excerpt_and_paths(self):
        summary = review.review_summary([complete(record())])
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn('压合后出现白斑', encoded)
        self.assertNotIn('仅提供排查线索', encoded)
        self.assertNotIn(EXCERPT_A, encoded)
        self.assertNotIn('C:\\', encoded)

    def test_round_trip_and_refuse_accidental_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'packet.jsonl'
            review.write_records([record()], target)
            self.assertEqual(review.load_records(target)[0]['case_id'], 'PILOT_TEST_01')
            with self.assertRaises(FileExistsError):
                review.write_records([record()], target)

    def test_cli_prepare_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory) / 'packet.jsonl'
            with patch.object(review, 'answer_case', side_effect=lambda item, **_: record(item)):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = review.main(['--prepare', str(packet)])
                self.assertEqual(code, 0)
                self.assertIn(
                    'internal_pilot_review=awaiting_optional_review',
                    output.getvalue())
                self.assertIn('usage_blocked=false', output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                code = review.main(['--status', str(packet)])
            self.assertEqual(code, 0)
            self.assertIn('total_cases=5', output.getvalue())
            self.assertIn('pending_cases=5', output.getvalue())


if __name__ == '__main__':
    unittest.main()
