from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.audits.mail_corpus_audit import audit


def make_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.executescript(
                    """
                    CREATE TABLE contents (
                        content_id TEXT PRIMARY KEY, original_text TEXT NOT NULL, normalized_text TEXT
                    );
                    CREATE TABLE documents (
                        doc_id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT NOT NULL,
                        source_created_at TEXT, duplicate_group_id TEXT NOT NULL,
                        metadata_json TEXT NOT NULL, content_id TEXT NOT NULL
                    );
                    """
                )
            samples = [
                ("BODY_SECRET_ALPHA\nline-a\nline-a\nline-b\nline-b\nline-c\nline-c\nline-d\nline-d\nline-e",
                 "RE: SUBJECT_SECRET", None, "group-a",
                 {"excluded_attachment_count": 2, "metadata": {"parent_source_ids": ["PRIVATE_ID"]}}),
                ("BODY_SECRET_BETA", "RE: ordinary", "2026-01-02", "group-a",
                 {"excluded_attachment_count": 0, "sent_at": "PRIVATE_DATE"}),
                ("BODY_SECRET_GAMMA", "FWD: SUBJECT_SECRET_2", None, "group-c", {}),
            ]
            for index, (body, title, created, group, metadata) in enumerate(samples, 1):
                ident = f"doc-{index}"
                connection.execute(
                    "INSERT INTO contents VALUES (?, ?, NULL)", (ident, body)
                )
                connection.execute(
                    "INSERT INTO documents VALUES (?, 'MAIL', ?, ?, ?, ?, ?)",
                    (ident, title, created, group, json.dumps(metadata), ident),
                )
            connection.execute(
                "INSERT INTO contents VALUES ('other', 'NOT_A_MAIL', NULL)"
            )
            connection.execute(
                "INSERT INTO documents VALUES ('other', 'CASE', 'CASE_TITLE', NULL, 'other', '{}', 'other')"
            )


def make_policy(path: Path, *, dates: bool = False, headers: bool = False) -> None:
    path.write_text(json.dumps({
        "export_exact_dates": dates,
        "export_headers": headers,
    }), encoding="utf-8")


class MailCorpusAuditTests(unittest.TestCase):
    def test_returns_aggregate_metrics_without_source_values(self):
        with tempfile.TemporaryDirectory(prefix="mail-audit-") as tmp:
            database = Path(tmp) / "catalog.sqlite3"
            policy = Path(tmp) / "privacy_policy.json"
            make_database(database)
            make_policy(policy)
            result = audit(database, policy)
            serialized = json.dumps(result, ensure_ascii=False)

        self.assertEqual(result["schema"], "pcb-mail-corpus-audit-v1")
        self.assertTrue(result["read_only"])
        self.assertTrue(result["aggregate_only"])
        self.assertEqual(result["mail_documents"], 3)
        self.assertEqual(result["reply_or_forward_titles"], 3)
        self.assertEqual(result["reply_title_count"], 2)
        self.assertEqual(result["forward_title_count"], 1)
        self.assertEqual(result["reply_titles_with_parent_link"], 1)
        self.assertEqual(result["reply_parent_link_coverage"], 0.5)
        self.assertEqual(result["source_created_at_present"], 1)
        self.assertEqual(result["metadata_date_fields_present"], 1)
        self.assertEqual(result["metadata_parent_source_ids_field_present"], 1)
        self.assertEqual(result["metadata_parent_source_ids_nonempty"], 1)
        self.assertEqual(result["source_created_at_coverage"], 0.3333)
        self.assertEqual(result["collection_policy_context"], {
            "exact_dates": "excluded_by_policy", "mail_headers": "excluded_by_policy",
        })
        self.assertEqual(result["duplicate_groups_with_multiple_mails"], 1)
        self.assertEqual(result["mail_documents_in_duplicate_groups"], 2)
        self.assertEqual(result["excluded_attachment_count_distribution"], {"0": 2, "2": 1})
        self.assertEqual(
            [signal["code"] for signal in result["signals"]],
            ["reply_forward_titles_common", "repeated_lines_common"],
        )
        self.assertEqual(result["repeated_line_ratio"]["over_30_percent"], 1)
        for private_value in (
            "BODY_SECRET_ALPHA", "BODY_SECRET_BETA", "BODY_SECRET_GAMMA",
            "SUBJECT_SECRET", "PRIVATE_ID", "PRIVATE_DATE", "doc-1",
        ):
            self.assertNotIn(private_value, serialized)

    def test_empty_mail_corpus_returns_well_formed_empty_metrics(self):
        with tempfile.TemporaryDirectory(prefix="mail-audit-empty-") as tmp:
            database = Path(tmp) / "catalog.sqlite3"
            policy = Path(tmp) / "privacy_policy.json"
            make_database(database)
            make_policy(policy)
            with closing(sqlite3.connect(database)) as connection:
                with connection:
                    connection.execute("DELETE FROM documents WHERE type='MAIL'")
            result = audit(database, policy)
        self.assertEqual(result["mail_documents"], 0)
        self.assertIsNone(result["body_length"]["median"])
        self.assertIsNone(result["source_created_at_coverage"])
        self.assertEqual(result["repeated_line_ratio"]["eligible_documents"], 0)
        self.assertEqual(result["signals"], [])

    def test_missing_date_and_thread_fields_are_actionable_only_if_policy_allows_export(self):
        with tempfile.TemporaryDirectory(prefix="mail-audit-policy-") as tmp:
            root = Path(tmp)
            database = root / "catalog.sqlite3"
            policy = root / "privacy_policy.json"
            make_database(database)
            make_policy(policy, dates=True, headers=True)
            result = audit(database, policy)
        codes = [signal["code"] for signal in result["signals"]]
        self.assertIn("source_timestamp_coverage_low", codes)
        self.assertIn("reply_parent_link_coverage_low", codes)
        self.assertEqual(result["collection_policy_context"], {
            "exact_dates": "collected", "mail_headers": "collected",
        })

    def test_collection_policy_must_have_explicit_boolean_flags(self):
        with tempfile.TemporaryDirectory(prefix="mail-audit-invalid-policy-") as tmp:
            root = Path(tmp)
            database = root / "catalog.sqlite3"
            policy = root / "privacy_policy.json"
            make_database(database)
            policy.write_text('{"export_exact_dates": "yes", "export_headers": false}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "privacy_policy_flags_invalid"):
                audit(database, policy)


if __name__ == "__main__":
    unittest.main()
