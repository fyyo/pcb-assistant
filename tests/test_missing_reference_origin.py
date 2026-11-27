"""Synthetic and live coverage tests for missing-reference origin analysis."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scripts.analyze_missing_reference_origin import (
    MissingReferenceOriginAudit,
    MissingReferenceOriginError,
    PROFILE_DEFAULTS,
    ROOT,
)


FIXED_TIME = "2026-10-21T11:00:00+08:00"
LIVE_AUDITS = {}
for _audit_date in ('20261021', '20261105'):
    _audit_relative = f'docs/phase3_missing_reference_audit_{_audit_date}.json'
    _audit_path = ROOT / _audit_relative
    LIVE_AUDITS[_audit_date] = (_audit_path if _audit_path.is_file() else
                               ROOT / '.private/project_records' / _audit_relative)
SECRET_BODY = "SECRET_EMAIL_BODY_MUST_NOT_APPEAR"
SECRET_SUBJECT = "SECRET_EMAIL_SUBJECT_MUST_NOT_APPEAR"
TARGET_IDS = (
    "mail_no_text",
    "mail_empty",
    "mail_residual",
    "mail_shared",
    "mail_unexplained",
)


class MissingReferenceOriginAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audit_path = self.root / "docs/missing_audit.json"
        self.quarantine_path = self.root / "data/sanitized_v3/quarantine_manifest.jsonl"
        self.message_status_path = self.root / "data/sanitized_v3/message_status.jsonl"
        self.occurrences_path = self.root / "data/attachments_v2/occurrences.jsonl"
        self.documents_path = self.root / "data/attachments_v2/documents.jsonl"
        self.catalog_path = self.root / "data/phase3_catalog_v1/catalog.sqlite3"
        self.sanitized_report_path = self.root / "data/sanitized_v3/report.json"
        self.attachments_report_path = self.root / "data/attachments_v2/report.json"
        self.original_mail = self.root / "19924784-20240420.pst"
        self.original_mail.write_bytes(b"synthetic-pst-not-read")

        self.write_json(self.audit_path, {
            "report_version": 1,
            "summary": {"unique_target_ids": len(TARGET_IDS)},
            "targets": [
                {
                    "target_source_id": target_id,
                    "target_type": "MAIL",
                    "classification": "reference_only",
                    "reference_count": 1,
                    "affected_document_count": 1,
                }
                for target_id in TARGET_IDS
            ],
        })
        self.write_jsonl(self.quarantine_path, [
            {"id": "mail_no_text", "reason": "no_text_body"},
            {"id": "mail_empty", "reason": "empty_after_redaction"},
            {"id": "mail_residual", "reason": "residual_quarantine"},
            {"id": "mail_not_in_audit", "reason": "no_text_body"},
        ])
        self.write_jsonl(self.occurrences_path, [
            self.occurrence("mail_no_text", "att_no_text", 0),
            self.occurrence("mail_empty", "att_empty", 0),
            self.occurrence("mail_residual", "att_residual", 0),
            self.occurrence("mail_shared", "att_shared", 0),
            self.occurrence("mail_unexplained", "att_unexplained", 0),
            self.occurrence("mail_retained_a", "att_shared", 0),
        ])
        self.write_jsonl(self.documents_path, [
            self.document("att_no_text", ["mail_no_text"]),
            self.document("att_empty", ["mail_empty"]),
            self.document("att_residual", ["mail_residual"]),
            self.document("att_shared", ["mail_retained_a", "mail_shared"]),
            self.document("att_unexplained", ["mail_unexplained"]),
        ])
        self.create_catalog({"mail_retained_a", "mail_retained_b"})
        self.write_json(self.sanitized_report_path, {
            "status": "completed",
            "counts": {
                "messages_visited": 7,
                "unique_candidates": 2,
                "engineering_candidates": 1,
                "duplicate_candidates": 3,
                "contact_read_error": 0,
                "no_text_body": 1,
                "body_no_text": 0,
                "body_html": 0,
                "empty_after_redaction": 1,
                "residual_quarantine": 1,
            },
        })
        self.write_json(self.attachments_report_path, {
            "status": "completed",
            "counts": {
                "attachment_occurrences": 6,
                "unique_documents": 5,
                "parse_failed": 0,
                "duplicate_occurrences": 1,
                "attachment_list_errors": 0,
                "identity_errors": 0,
            },
        })
        self.write_jsonl(self.root / "data/sanitized_v3/candidates.jsonl", [{
            "id": "mail_retained_a",
            "subject": SECRET_SUBJECT,
            "text": SECRET_BODY,
        }])

    @staticmethod
    def occurrence(parent: str, document: str, ordinal: int) -> dict:
        return {
            "parent_source_id": parent,
            "attachment_ordinal": ordinal,
            "document_id": document,
            "status": "read",
        }

    @staticmethod
    def document(document_id: str, parents: list[str]) -> dict:
        return {
            "document_id": document_id,
            "parent_source_ids": parents,
            "format": "txt",
            "status": "processed",
            "exported_units": 0,
            "counts": {},
            "parser_stats": {},
        }

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def write_jsonl(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def create_catalog(self, retained_ids: set[str]) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.catalog_path)
        try:
            connection.execute(
                "CREATE TABLE documents (doc_id TEXT PRIMARY KEY, type TEXT, "
                "source_type TEXT, source_id TEXT)"
            )
            connection.executemany(
                "INSERT INTO documents VALUES (?, 'MAIL', 'mail', ?)",
                [(f"doc_{index}", source_id) for index, source_id in enumerate(sorted(retained_ids))],
            )
            connection.commit()
        finally:
            connection.close()

    def runner(self, **overrides) -> MissingReferenceOriginAudit:
        values = {
            "audit": self.audit_path,
            "quarantine": self.quarantine_path,
            "message_status": self.message_status_path,
            "occurrences": self.occurrences_path,
            "documents": self.documents_path,
            "catalog": self.catalog_path,
            "sanitized_report": self.sanitized_report_path,
            "attachments_report": self.attachments_report_path,
            "original_mail": self.original_mail,
            "output": "docs/origin.json",
        }
        values.update(overrides)
        return MissingReferenceOriginAudit(self.root, **values)

    def configure_duplicate_status(self, *, retained_candidate: bool) -> tuple[str, str]:
        target = "mail_" + "a" * 24
        candidate = "mail_" + "b" * 24
        catalog_candidate = candidate if retained_candidate else "mail_" + "c" * 24
        self.write_json(self.audit_path, {
            "report_version": 1,
            "summary": {"unique_target_ids": 1},
            "targets": [{
                "target_source_id": target,
                "target_type": "MAIL",
                "classification": "reference_only",
                "reference_count": 1,
                "affected_document_count": 1,
            }],
        })
        self.write_jsonl(self.quarantine_path, [])
        self.write_jsonl(self.occurrences_path, [
            self.occurrence(target, "att_duplicate", 0),
        ])
        self.write_jsonl(self.documents_path, [
            self.document("att_duplicate", [target]),
        ])
        self.catalog_path.unlink()
        self.create_catalog({catalog_candidate})
        self.write_json(self.sanitized_report_path, {
            "status": "completed",
            "counts": {
                "messages_visited": 2,
                "unique_candidates": 1,
                "engineering_candidates": 0,
                "duplicate_candidates": 1,
                "contact_read_error": 0,
                "no_text_body": 0,
                "body_no_text": 0,
                "body_html": 0,
                "empty_after_redaction": 0,
                "residual_quarantine": 0,
                "oversize_body": 0,
                "body_read_error": 0,
                "processing_error": 0,
            },
        })
        self.write_json(self.attachments_report_path, {
            "status": "completed",
            "counts": {
                "attachment_occurrences": 1,
                "unique_documents": 1,
                "parse_failed": 0,
                "duplicate_occurrences": 0,
                "attachment_list_errors": 0,
                "identity_errors": 0,
            },
        })
        self.write_jsonl(self.message_status_path, [
            {"id": candidate, "status": "candidate", "duplicate_of": None},
            {"id": target, "status": "duplicate", "duplicate_of": candidate},
        ])
        return target, candidate

    def test_classification_and_summary(self):
        report = self.runner().run(generated_at=FIXED_TIME)
        summary = report["summary"]
        self.assertEqual(summary["audited_targets"], 5)
        self.assertEqual(summary["audit_reference_count"], 5)
        self.assertEqual(summary["target_attachment_occurrence_count"], 5)
        self.assertEqual(summary["attachment_manifest_occurrence_count"], 6)
        self.assertEqual(summary["targets_with_attachment_occurrence"], 5)
        self.assertEqual(summary["unique_attachment_document_count"], 5)
        self.assertEqual(summary["shared_attachment_document_count"], 1)
        self.assertEqual(summary["quarantine_record_count"], 4)
        self.assertEqual(summary["quarantine_targets_matched"], 3)
        self.assertEqual(summary["directly_explained_by_quarantine"], 3)
        self.assertEqual(summary["not_directly_explained"], 2)
        self.assertEqual(summary["shared_attachment_only"], 1)
        self.assertEqual(summary["unexplained_by_retained_manifests"], 1)
        self.assertEqual(summary["quarantine_entries_outside_audited_targets"], 1)
        self.assertEqual(summary["classification_counts"], {
            "attachment_shared_with_retained_mail": 1,
            "explicit_quarantine_empty_after_redaction": 1,
            "explicit_quarantine_no_text_body": 1,
            "explicit_quarantine_residual": 1,
            "not_explained_by_retained_manifests": 1,
        })
        self.assertEqual(summary["quarantine_reason_counts"], {
            "empty_after_redaction": 1,
            "no_text_body": 1,
            "residual_quarantine": 1,
        })

    def test_shared_attachment_is_partial_evidence_only(self):
        report = self.runner().run(generated_at=FIXED_TIME)
        targets = {row["target_source_id"]: row for row in report["targets"]}
        shared = targets["mail_shared"]
        self.assertEqual(shared["classification"], "attachment_shared_with_retained_mail")
        self.assertEqual(shared["resolution"], "unresolved")
        self.assertFalse(shared["retained_primary_match"])
        self.assertEqual(shared["shared_attachment_document_count"], 1)
        self.assertEqual(shared["shared_retained_parent_count"], 1)
        self.assertEqual(shared["shared_retained_parent_preview"], ["mail_retained_a"])
        self.assertNotIn("mail_retained_a", targets)
        self.assertTrue(shared["evidence"]["documents"][0]["shared_with_retained_mail"])

    def test_explicit_duplicate_status_remains_unresolved(self):
        target_id, candidate_id = self.configure_duplicate_status(retained_candidate=True)
        report = self.runner().run(generated_at=FIXED_TIME)
        target = report["targets"][0]
        self.assertEqual(target["target_source_id"], target_id)
        self.assertEqual(
            target["classification"], "explicit_duplicate_of_retained_candidate"
        )
        self.assertEqual(target["message_status"], "duplicate")
        self.assertEqual(target["message_status_duplicate_of"], candidate_id)
        self.assertEqual(target["resolution"], "unresolved")
        self.assertEqual(report["summary"]["explicitly_attributed_to_duplicate"], 1)
        self.assertEqual(report["summary"]["message_status_record_count"], 2)
        self.assertEqual(report["summary"]["message_status_targets_matched"], 1)
        self.assertEqual(report["context"]["unlogged_duplicate_candidates"], 0)
        self.assertTrue(report["integrity"]["message_status_read"])
        input_paths = {row["path"] for row in report["integrity"]["inputs"]}
        self.assertIn("data/sanitized_v3/message_status.jsonl", input_paths)

    def test_duplicate_status_target_must_be_retained(self):
        self.configure_duplicate_status(retained_candidate=False)
        with self.assertRaises(MissingReferenceOriginError):
            self.runner().run(generated_at=FIXED_TIME)

    def test_targets_are_sorted_and_repeatable(self):
        first = self.runner().run(generated_at=FIXED_TIME)
        second = self.runner().run(generated_at=FIXED_TIME)
        self.assertEqual(first, second)
        target_ids = [row["target_source_id"] for row in first["targets"]]
        self.assertEqual(target_ids, sorted(TARGET_IDS))

    def test_output_excludes_prose_and_pst_is_not_opened(self):
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.resolve() == self.original_mail.resolve():
                raise AssertionError("PST content must not be opened")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            runner = self.runner()
            report = runner.run(generated_at=FIXED_TIME)
            output = runner.write_report(report)
        text = output.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_BODY, text)
        self.assertNotIn(SECRET_SUBJECT, text)
        self.assertFalse(report["integrity"]["source_prose_fields_read"])
        self.assertFalse(report["integrity"]["email_subject_or_body_emitted"])
        self.assertFalse(report["integrity"]["original_mail"]["read"])
        input_paths = {row["path"] for row in report["integrity"]["inputs"]}
        self.assertNotIn("data/sanitized_v3/candidates.jsonl", input_paths)

    def test_existing_output_is_never_overwritten(self):
        runner = self.runner()
        report = runner.run(generated_at=FIXED_TIME)
        output = runner.write_report(report)
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            runner.write_report(report)
        self.assertEqual(before, output.read_bytes())

    def test_output_under_data_is_rejected(self):
        runner = self.runner()
        report = runner.run(generated_at=FIXED_TIME)
        with self.assertRaises(ValueError):
            runner.write_report(report, self.root / "data/origin.json")
        self.assertFalse((self.root / "data/origin.json").exists())

    def test_inputs_and_original_mail_are_unchanged(self):
        paths = [
            self.audit_path,
            self.quarantine_path,
            self.occurrences_path,
            self.documents_path,
            self.catalog_path,
            self.sanitized_report_path,
            self.attachments_report_path,
            self.original_mail,
        ]
        before = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in paths
        }
        report = self.runner().run(generated_at=FIXED_TIME)
        after = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in paths
        }
        self.assertEqual(before, after)
        self.assertTrue(report["integrity"]["inputs_unchanged_during_audit"])
        self.assertTrue(report["integrity"]["original_mail"]["stat_unchanged_during_audit"])
        self.assertFalse(report["integrity"]["catalog_modified"])

    def test_manifest_count_mismatch_fails_closed(self):
        report = json.loads(self.attachments_report_path.read_text(encoding="utf-8"))
        report["counts"]["attachment_occurrences"] = 999
        self.write_json(self.attachments_report_path, report)
        with self.assertRaises(MissingReferenceOriginError):
            self.runner().run(generated_at=FIXED_TIME)

    def test_multiple_quarantine_manifests_are_merged(self):
        second_quarantine = (
            self.root / "data/sanitized_v9/quarantine_manifest.jsonl"
        )
        self.write_jsonl(second_quarantine, [
            {"id": "mail_unexplained", "reason": "no_text_body"},
        ])
        report = self.runner(
            quarantine=[self.quarantine_path, second_quarantine]
        ).run(generated_at=FIXED_TIME)
        summary = report["summary"]
        self.assertEqual(summary["quarantine_manifest_count"], 2)
        self.assertEqual(summary["quarantine_record_count"], 5)
        self.assertEqual(summary["directly_explained_by_quarantine"], 4)
        self.assertEqual(summary["shared_attachment_only"], 1)
        self.assertEqual(summary["unexplained_by_retained_manifests"], 0)


def _live_profile_available(profile: str, audit_date: str) -> bool:
    if not LIVE_AUDITS[audit_date].is_file():
        return False
    runner = MissingReferenceOriginAudit(
        ROOT,
        profile=profile,
        **PROFILE_DEFAULTS[profile],
    )
    return all(path.is_file() for path in runner._input_paths())


class LiveWorkspaceCoverageTests(unittest.TestCase):
    @unittest.skipUnless(
        _live_profile_available("v1", "20261021"),
        "live v1 audit assets are not available",
    )
    def test_live_workspace_covers_every_audited_target(self):
        runner = MissingReferenceOriginAudit(ROOT)
        report = runner.run(generated_at=FIXED_TIME)
        audit = json.loads(
            LIVE_AUDITS['20261021'].read_text(
                encoding="utf-8"
            )
        )
        expected_ids = sorted(
            row["target_source_id"] for row in audit["targets"]
        )
        actual_ids = [row["target_source_id"] for row in report["targets"]]
        self.assertEqual(len(expected_ids), 404)
        self.assertEqual(actual_ids, expected_ids)
        self.assertEqual(report["summary"]["audited_targets"], 404)
        self.assertEqual(report["summary"]["directly_explained_by_quarantine"], 189)
        self.assertEqual(report["summary"]["shared_attachment_only"], 114)
        self.assertEqual(report["summary"]["unexplained_by_retained_manifests"], 101)
        self.assertEqual(report["summary"]["remaining_unresolved"], 404)
        self.assertTrue(all(
            row["resolution"] == "unresolved" for row in report["targets"]
        ))
        self.assertFalse(report["integrity"]["original_mail"]["read"])


    @unittest.skipUnless(
        _live_profile_available("v3", "20261105"),
        "live v3 audit assets are not available",
    )
    def test_live_v3_workspace_uses_merged_sources(self):
        runner = MissingReferenceOriginAudit(
            ROOT,
            profile="v3",
            **PROFILE_DEFAULTS["v3"],
        )
        report = runner.run(generated_at=FIXED_TIME)
        summary = report["summary"]
        self.assertEqual(report["asset_profile"], "v3")
        self.assertEqual(summary["audited_targets"], 400)
        self.assertEqual(summary["audit_reference_count"], 890)
        self.assertEqual(summary["directly_explained_by_quarantine"], 188)
        self.assertEqual(summary["shared_attachment_only"], 111)
        self.assertEqual(summary["unexplained_by_retained_manifests"], 101)
        self.assertEqual(summary["remaining_unresolved"], 400)
        self.assertEqual(summary["retained_mail_document_count"], 14938)
        self.assertEqual(summary["retained_mail_unique_source_ids"], 14699)
        self.assertEqual(summary["retained_mail_duplicate_source_ids"], 239)
        self.assertEqual(report["context"]["unlogged_duplicate_candidates"], 431)
        self.assertEqual(report["context"]["message_status_manifest_count"], 1)
        self.assertTrue(all(
            row["resolution"] == "unresolved" for row in report["targets"]
        ))
        self.assertFalse(report["integrity"]["original_mail"]["read"])


if __name__ == "__main__":
    unittest.main()
