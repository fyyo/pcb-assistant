"""Synthetic tests for the anonymized per-message status ledger."""
from __future__ import annotations

from collections import Counter
import json
import unittest

from mail_status import (
    MESSAGE_STATUS_ROW_FIELDS,
    STATUS_STAT_FIELDS,
    MessageStatusError,
    MessageStatusLedger,
    validate_message_status_rows,
)


def message_id(value: int) -> str:
    return f"mail_{value:024x}"


def stats_for(rows: list[dict[str, object]]) -> dict[str, int]:
    counts = Counter(row["status"] for row in rows)
    stats = {"messages_visited": len(rows)}
    for status, field in STATUS_STAT_FIELDS.items():
        stats[field] = counts[status]
    return stats


class MessageStatusLedgerTests(unittest.TestCase):
    def test_every_status_is_recorded_with_strict_schema(self):
        ledger = MessageStatusLedger()
        candidate, duplicate = ledger.record_candidate(1, message_id(1), b"same")
        self.assertFalse(duplicate)
        duplicate, is_duplicate = ledger.record_candidate(2, message_id(2), b"same")
        self.assertTrue(is_duplicate)
        reject_statuses = (
            "no_text_body",
            "empty_after_redaction",
            "residual_quarantine",
            "oversize_body",
            "contact_read_error",
            "body_read_error",
            "processing_error",
        )
        for sequence, status in enumerate(reject_statuses, 3):
            ledger.record_reject(sequence, message_id(sequence), status)

        rows = ledger.rows()
        self.assertEqual(len(rows), 9)
        self.assertEqual({row["status"] for row in rows}, {
            "candidate",
            "duplicate",
            "no_text_body",
            "empty_after_redaction",
            "residual_quarantine",
            "oversize_body",
            "contact_read_error",
            "body_read_error",
            "processing_error",
        })
        self.assertTrue(all(set(row) == MESSAGE_STATUS_ROW_FIELDS for row in rows))
        self.assertEqual(candidate["duplicate_of"], None)
        self.assertEqual(duplicate["duplicate_of"], message_id(1))
        self.assertEqual(validate_message_status_rows(rows, stats_for(rows)), ledger.counts())

    def test_rows_are_sorted_by_scan_sequence(self):
        ledger = MessageStatusLedger()
        ledger.record_reject(3, message_id(3), "no_text_body")
        ledger.record_candidate(1, message_id(1), b"first")
        ledger.record_reject(2, message_id(2), "processing_error")
        self.assertEqual(
            [row["id"] for row in ledger.rows()],
            [message_id(1), message_id(2), message_id(3)],
        )

    def test_duplicate_points_to_first_retained_candidate(self):
        ledger = MessageStatusLedger()
        first, first_duplicate = ledger.record_candidate(1, message_id(1), b"shared")
        second, second_duplicate = ledger.record_candidate(2, message_id(2), b"shared")
        third, third_duplicate = ledger.record_candidate(3, message_id(3), b"different")
        self.assertFalse(first_duplicate)
        self.assertTrue(second_duplicate)
        self.assertFalse(third_duplicate)
        self.assertEqual(first["status"], "candidate")
        self.assertEqual(second["duplicate_of"], message_id(1))
        self.assertEqual(third["status"], "candidate")
        self.assertIsNone(third["duplicate_of"])

    def test_duplicate_target_must_be_a_candidate(self):
        rows = [
            {"id": message_id(1), "status": "candidate", "duplicate_of": None},
            {"id": message_id(2), "status": "duplicate", "duplicate_of": message_id(3)},
            {"id": message_id(3), "status": "no_text_body", "duplicate_of": None},
        ]
        with self.assertRaises(MessageStatusError):
            validate_message_status_rows(rows, stats_for(rows))

    def test_candidate_status_cannot_be_recorded_as_reject(self):
        ledger = MessageStatusLedger()
        with self.assertRaises(MessageStatusError):
            ledger.record_reject(1, message_id(1), "candidate")
        with self.assertRaises(MessageStatusError):
            ledger.record_reject(1, message_id(1), "duplicate")

    def test_count_mismatch_fails_closed(self):
        ledger = MessageStatusLedger()
        ledger.record_candidate(1, message_id(1), b"one")
        ledger.record_reject(2, message_id(2), "no_text_body")
        rows = ledger.rows()
        stats = stats_for(rows)
        stats["messages_visited"] += 1
        with self.assertRaises(MessageStatusError):
            validate_message_status_rows(rows, stats)

        stats = stats_for(rows)
        stats["no_text_body"] = 0
        with self.assertRaises(MessageStatusError):
            validate_message_status_rows(rows, stats)

    def test_extra_fields_and_prose_are_rejected(self):
        rows = [
            {
                "id": message_id(1),
                "status": "candidate",
                "duplicate_of": None,
                "subject": "must not be accepted",
                "text": "must not be accepted",
                "fingerprint": "must not be accepted",
            }
        ]
        with self.assertRaises(MessageStatusError):
            validate_message_status_rows(rows, stats_for(rows))

    def test_duplicate_ids_and_sequences_are_rejected(self):
        ledger = MessageStatusLedger()
        ledger.record_candidate(1, message_id(1), b"one")
        with self.assertRaises(MessageStatusError):
            ledger.record_reject(1, message_id(2), "no_text_body")
        with self.assertRaises(MessageStatusError):
            ledger.record_reject(2, message_id(1), "no_text_body")

    def test_serialized_ledger_excludes_content_and_fingerprint(self):
        ledger = MessageStatusLedger()
        fingerprint = b"SECRET_CONTENT_FINGERPRINT"
        ledger.record_candidate(1, message_id(1), fingerprint)
        ledger.record_candidate(2, message_id(2), fingerprint)
        serialized = json.dumps(ledger.rows(), ensure_ascii=False)
        self.assertNotIn("SECRET_CONTENT_FINGERPRINT", serialized)
        self.assertNotIn("subject", serialized)
        self.assertNotIn("text", serialized)
        self.assertNotIn("body", serialized)
        self.assertTrue(all(
            set(json.loads(line)) == MESSAGE_STATUS_ROW_FIELDS
            for line in (json.dumps(row) for row in ledger.rows())
        ))

    def test_empty_or_non_bytes_fingerprint_is_rejected(self):
        ledger = MessageStatusLedger()
        for fingerprint in (b"", "", None, "text"):
            with self.subTest(fingerprint=fingerprint):
                with self.assertRaises(MessageStatusError):
                    ledger.record_candidate(1, message_id(1), fingerprint)


if __name__ == "__main__":
    unittest.main()
