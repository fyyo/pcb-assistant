'''Offline tests for the private in-use feedback ledger.'''
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from catalog_common import CatalogError
from internal_pilot_feedback import (
    DETAIL_SCHEMA,
    FEEDBACK_SCHEMA,
    LIST_SCHEMA,
    STATUS_SCHEMA,
    feedback_status,
    list_feedback,
    record_feedback,
    resolve_feedback,
    show_feedback,
)


EXCERPT_A = 'EXC_aaaaaaaaaaaaaaaaaaaaaaaa'
EXCERPT_B = 'EXC_bbbbbbbbbbbbbbbbbbbbbbbb'
TURN_FINGERPRINT = 'a' * 64


def turn():
    return {
        'schema': 'catalog-chat-v1',
        'result_fingerprint': TURN_FINGERPRINT,
        'answer_mode': 'evidence_fallback',
        'evidence_status': 'answered',
        'citations': [
            {'excerpt_id': EXCERPT_A, 'doc_id': 'DOC_A'},
            {'excerpt_id': EXCERPT_B, 'doc_id': 'DOC_B'},
        ],
    }


class InternalPilotFeedbackTests(unittest.TestCase):
    def test_record_links_turn_and_citations_in_private_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / '.private'
            root = private / 'internal_pilot_feedback'
            receipt = record_feedback(
                turn(), '该引用上下文不支持结论',
                kind='wrong_citation', citation_ids=[EXCERPT_B], root=root)
            self.assertEqual(receipt['status'], 'recorded')
            self.assertEqual(receipt['turn_fingerprint'], TURN_FINGERPRINT)
            self.assertEqual(receipt['citation_ids'], [EXCERPT_B])
            self.assertFalse(receipt['catalog_write'])

            rows = [
                json.loads(line) for line in
                (root / 'feedback.jsonl').read_text(encoding='utf-8').splitlines()
            ]
            self.assertEqual(len(rows), 1)
            record = rows[0]
            self.assertEqual(record['schema'], FEEDBACK_SCHEMA)
            self.assertEqual(record['turn_fingerprint'], TURN_FINGERPRINT)
            self.assertEqual(record['result_fingerprint'], TURN_FINGERPRINT)
            self.assertEqual(record['citation_ids'], [EXCERPT_B])
            self.assertEqual(record['kind'], 'wrong_citation')
            self.assertEqual(record['status'], 'open')
            self.assertTrue(record['feedback_id'].startswith('FBK_'))
            written = [path for path in Path(directory).rglob('*') if path.is_file()]
            self.assertTrue(written)
            self.assertTrue(all(path.is_relative_to(private) for path in written))

    def test_record_defaults_to_all_turn_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = record_feedback(
                turn(), '结论不准确', kind='wrong_conclusion',
                root=Path(directory))
            self.assertEqual(receipt['citation_ids'], [EXCERPT_A, EXCERPT_B])

    def test_record_requires_a_turn_and_a_known_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CatalogError, 'feedback_requires_previous_turn'):
                record_feedback(None, '没有上一轮', root=root)
            with self.assertRaisesRegex(CatalogError, 'feedback_citation_not_in_turn'):
                record_feedback(
                    turn(), '引用不存在',
                    citation_ids=['EXC_cccccccccccccccccccccccc'], root=root)

    def test_status_reports_counts_without_feedback_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_feedback(
                turn(), '内部敏感纠错正文', kind='wrong_retrieval', root=root)
            summary = feedback_status(root)
            self.assertEqual(summary['schema'], STATUS_SCHEMA)
            self.assertEqual(summary['total_feedback'], 1)
            self.assertEqual(summary['open_feedback'], 1)
            self.assertEqual(summary['kinds'], {'wrong_retrieval': 1})
            encoded = json.dumps(summary, ensure_ascii=False)
            self.assertNotIn('内部敏感纠错正文', encoded)
            self.assertNotIn(str(root), encoded)
            self.assertFalse(summary['text_returned'])

    def test_status_keeps_malformed_lines_visible_without_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / 'feedback.jsonl'
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text('{not-json}\n', encoding='utf-8')
            summary = feedback_status(root)
            self.assertEqual(summary['status'], 'ok')
            self.assertEqual(summary['total_feedback'], 0)
            self.assertEqual(summary['invalid_records'], 1)

    def test_list_is_newest_first_and_hides_feedback_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = record_feedback(
                turn(), '第一条敏感正文', kind='wrong_citation',
                created_at='2026-10-29T09:00:00+08:00', root=root)
            second = record_feedback(
                turn(), '第二条敏感正文', kind='wrong_conclusion',
                created_at='2026-10-29T10:00:00+08:00', root=root)
            summary = list_feedback(root)
            self.assertEqual(summary['schema'], LIST_SCHEMA)
            self.assertEqual(summary['filter'], 'open')
            self.assertEqual(summary['total_matched'], 2)
            self.assertEqual(
                [item['feedback_id'] for item in summary['feedback']],
                [second['feedback_id'], first['feedback_id']])
            self.assertEqual(summary['feedback'][0]['kind'], 'wrong_conclusion')
            self.assertEqual(summary['feedback'][0]['turn_fingerprint_prefix'],
                             TURN_FINGERPRINT[:12])
            self.assertFalse(summary['text_returned'])
            encoded = json.dumps(summary, ensure_ascii=False)
            self.assertNotIn('第一条敏感正文', encoded)
            self.assertNotIn('第二条敏感正文', encoded)

    def test_show_returns_the_selected_record_and_rejects_unknown_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = record_feedback(
                turn(), '可查看的纠错详情', kind='missing_context', root=root)
            summary = show_feedback(receipt['feedback_id'], root)
            self.assertEqual(summary['schema'], DETAIL_SCHEMA)
            self.assertEqual(summary['status'], 'found')
            self.assertEqual(summary['feedback']['text'], '可查看的纠错详情')
            self.assertTrue(summary['text_returned'])
            with self.assertRaisesRegex(CatalogError, 'feedback_not_found'):
                show_feedback('FBK_cccccccccccccccccccccccc', root)
            with self.assertRaisesRegex(CatalogError, 'invalid_feedback_id'):
                show_feedback('not-an-id', root)

    def test_resolve_updates_status_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = record_feedback(
                turn(), '需要关闭的反馈', kind='wrong_retrieval', root=root)
            resolved = resolve_feedback(
                receipt['feedback_id'], '已补充分层边界回归',
                root=root, resolved_at='2026-10-29T11:00:00+08:00')
            self.assertEqual(resolved['status'], 'resolved')
            self.assertEqual(resolved['resolved_at'],
                             '2026-10-29T11:00:00+08:00')
            self.assertTrue(resolved['resolution_present'])

            summary = feedback_status(root)
            self.assertEqual(summary['open_feedback'], 0)
            self.assertEqual(summary['resolved_feedback'], 1)
            self.assertEqual(list_feedback(root)['total_matched'], 0)
            resolved_list = list_feedback(root, status='resolved')
            self.assertEqual(resolved_list['total_matched'], 1)
            detail = show_feedback(receipt['feedback_id'], root)
            self.assertEqual(detail['feedback']['status'], 'resolved')
            self.assertEqual(detail['feedback']['resolution'],
                             '已补充分层边界回归')

            again = resolve_feedback(receipt['feedback_id'], root=root)
            self.assertEqual(again['status'], 'already_resolved')
            self.assertEqual(again['resolved_at'],
                             '2026-10-29T11:00:00+08:00')

    def test_resolve_refuses_to_rewrite_a_malformed_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = record_feedback(
                turn(), '保留损坏行', kind='other', root=root)
            ledger = root / 'feedback.jsonl'
            with ledger.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write('{not-json}\n')
            before = ledger.read_bytes()
            with self.assertRaisesRegex(CatalogError, 'feedback_ledger_invalid'):
                resolve_feedback(receipt['feedback_id'], root=root)
            self.assertEqual(ledger.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
