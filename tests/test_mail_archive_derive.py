import collections
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import scripts.data.derive_mail_candidates as derive


def message_id(value):
    return "mail_" + format(value, "024x")


def candidate(ident, subject, text, topics=None):
    return {
        "id": ident,
        "subject": subject,
        "text": text,
        "topics": topics or [],
        "excluded_attachment_count": 0,
        "review_required": True,
        "training_allowed": False,
        "source_kind": "untrusted_email",
    }


def write_jsonl(path, records):
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def create_archive(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "create table messages ("
        "message_pk integer primary key, stable_id text, subject text, "
        "body_latest text, body_text text, sent_at text, attachment_count integer)"
    )
    connection.executemany(
        "insert into messages (stable_id, subject, body_latest, body_text, sent_at, "
        "attachment_count) values (?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()


class StubSanitizer:
    def __init__(self):
        self.counts = collections.Counter()
        self.identity_terms = 2

    def clean(self, text):
        return text.replace("Alice", "[identity]").replace("Bob", "[identity]")

    def residuals(self, text):
        return {"known_identity": 1} if "SECRET" in text else {}


class MailArchiveDeriveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.archive = self.root / "archive.sqlite3"
        self.base = self.root / "base.jsonl"
        self.output = self.root / "derived"
        self.base_record = candidate(message_id(1), "existing", "existing body")
        write_jsonl(self.base, [self.base_record])

    def tearDown(self):
        self.temporary.cleanup()

    def run_derivation(self, rows, **kwargs):
        create_archive(self.archive, rows)
        return derive.run(
            self.archive,
            [self.base],
            self.output,
            sanitizer=StubSanitizer(),
            **kwargs,
        )

    def read_lines(self, name):
        return (self.output / name).read_text(encoding="utf-8").splitlines()

    def test_adds_missing_candidates_and_keeps_base_bytes(self):
        rows = [
            (message_id(1), "existing", "existing body", "existing body", "2026-01-01", 2),
            (message_id(2), "lamination white spot", "Alice lamination temperature 180 C", "old", "2026-01-02", 3),
            (message_id(3), "lamination white spot", "Alice lamination temperature 180 C", "", "2026-01-03", 0),
            (message_id(4), "empty", "", "", "2026-01-04", 0),
            (message_id(5), "residual", "SECRET lamination", "", "2026-01-05", 0),
        ]
        report = self.run_derivation(rows)

        candidates = self.read_lines("candidates.jsonl")
        self.assertEqual(candidates[0], json.dumps(self.base_record, ensure_ascii=False))
        added = [json.loads(line) for line in candidates[1:]]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["text"], "[identity] lamination temperature 180 C")
        self.assertEqual(added[0]["excluded_attachment_count"], 3)
        self.assertTrue(added[0]["review_required"])
        self.assertFalse(added[0]["training_allowed"])
        self.assertTrue(added[0]["topics"])
        self.assertEqual(len(self.read_lines("engineering_candidates.jsonl")), 1)

        statuses = [json.loads(line) for line in self.read_lines("message_status.jsonl")]
        self.assertEqual(
            [(row["id"], row["status"]) for row in statuses],
            [
                (message_id(2), "candidate"),
                (message_id(3), "duplicate"),
                (message_id(4), "no_text_body"),
                (message_id(5), "residual_quarantine"),
            ],
        )
        self.assertEqual(statuses[1]["duplicate_of"], message_id(2))
        self.assertEqual(report["counts"]["unique_candidates"], 1)
        self.assertEqual(report["counts"]["duplicate_candidates"], 1)
        self.assertEqual(report["output_audit"], {"records_read_back": 1})
        self.assertEqual(report["release"], derive.RELEASE)
        for name, expected in report["artifacts"].items():
            self.assertEqual(expected["sha256"], derive.hash_file(self.output / name))
        self.assertEqual(report["message_status"]["rows"], 4)
        self.assertEqual(report["message_status"]["counts"]["candidate"], 1)

    def test_duplicate_stable_ids_prefer_row_with_body(self):
        rows = [
            (message_id(2), "empty copy", "", "", "2026-01-01", 0),
            (message_id(2), "with body", "Alice lamination", "", "2026-01-02", 1),
        ]
        self.run_derivation(rows)
        added = [json.loads(line) for line in self.read_lines("candidates.jsonl")[1:]]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["subject"], "with body")
        self.assertEqual(added[0]["text"], "[identity] lamination")

    def test_missing_set_is_empty_when_base_covers_archive(self):
        rows = [(message_id(1), "existing", "existing body", "", "2026-01-01", 0)]
        report = self.run_derivation(rows)
        self.assertEqual(report["scan"]["missing_ids"], 0)
        self.assertEqual(self.read_lines("message_status.jsonl"), [])
        self.assertEqual(len(self.read_lines("candidates.jsonl")), 1)

    def test_dry_run_does_not_create_output(self):
        rows = [(message_id(2), "missing", "Alice lamination", "", "2026-01-02", 0)]
        create_archive(self.archive, rows)
        report = derive.run(
            self.archive,
            [self.base],
            self.output,
            sanitizer=StubSanitizer(),
            dry_run=True,
        )
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["missing_ids"], 1)
        self.assertFalse(self.output.exists())

    def test_new_only_omits_base_records(self):
        rows = [(message_id(2), "missing", "Alice lamination", "", "2026-01-02", 0)]
        report = self.run_derivation(rows, new_only=True)
        candidates = [json.loads(line) for line in self.read_lines("candidates.jsonl")]
        self.assertEqual([record["id"] for record in candidates], [message_id(2)])
        self.assertEqual(report["output_mode"], "new_only")
        self.assertEqual(report["scan"]["base_candidates"], 1)

    def test_existing_output_is_rejected(self):
        self.output.mkdir()
        create_archive(self.archive, [])
        with self.assertRaises(ValueError):
            derive.run(self.archive, [self.base], self.output, sanitizer=StubSanitizer())

    def test_oversize_body_is_rejected_before_redaction(self):
        messages = [{
            "id": message_id(2),
            "subject": "large",
            "body": "x" * 11,
            "attachment_count": 0,
        }]
        result = derive.derive_new(messages, StubSanitizer(), max_body_chars=10)
        self.assertEqual(result["stats"]["oversize_body"], 1)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["rejects"], [{"id": message_id(2), "reason": "oversize_body"}])


if __name__ == "__main__":
    unittest.main()
