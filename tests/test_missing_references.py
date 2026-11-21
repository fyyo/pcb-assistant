"""Synthetic tests for the read-only missing-reference audit."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from scripts.analyze_missing_references import (
    ASSET_SPECS,
    ASSET_SPECS_V3,
    PROFILE_DEFAULTS,
    MissingReferenceAudit,
)


FIXED_TIME = "2026-10-21T09:00:00+08:00"
SECRET_BODY = "SECRET_EMAIL_BODY_MUST_NOT_APPEAR"
SECRET_SUBJECT = "SECRET_EMAIL_SUBJECT_MUST_NOT_APPEAR"


class MissingReferenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for spec in ASSET_SPECS:
            path = self.root / spec.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        (self.root / "19924784-20240420.pst").write_bytes(b"synthetic-pst")
        self.references = [
            self.reference("mail_ref", "doc_a", "REL_1"),
            self.reference("mail_ref", "doc_b", "REL_2"),
            self.reference("mail_retained", "doc_a", "REL_3"),
            self.reference("mail_indexed", "doc_b", "REL_4"),
            self.reference("mail_missing", "doc_c", "REL_5"),
        ]
        self.write_references()
        self.write_jsonl("data/attachments_v2/documents.jsonl", [{
            "document_id": "att_1",
            "parent_source_ids": ["mail_ref"],
            "format": "txt",
            "status": "processed",
        }])
        self.write_jsonl("data/sanitized_v3/candidates.jsonl", [{
            "id": "mail_retained",
            "subject": SECRET_SUBJECT,
            "text": SECRET_BODY,
            "source_kind": "untrusted_email",
        }])
        self.write_jsonl("data/phase3_catalog_v1/catalog.jsonl", [{
            "doc_id": "CATALOG_1",
            "source_id": "mail_indexed",
            "source_type": "mail",
            "text": SECRET_BODY,
        }])

    @staticmethod
    def reference(target_id: str, from_id: str, reference_id: str) -> dict:
        return {
            "from_id": from_id,
            "locator": {"line": 1},
            "reason": "target_not_in_retained_complete_sources",
            "reference_id": reference_id,
            "relation": "ATTACHED_TO",
            "source_path": "data/attachments_v2/occurrences.jsonl",
            "target_source_id": target_id,
            "target_type": "MAIL",
        }

    def write_jsonl(self, relative: str, rows: list[dict]) -> None:
        path = self.root / relative
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def write_references(self) -> None:
        self.write_jsonl(
            "data/phase3_catalog_v1/unresolved_references.jsonl", self.references
        )

    def clear_assets(self) -> None:
        for spec in ASSET_SPECS:
            (self.root / spec.path).write_text("", encoding="utf-8")

    def run_audit(self):
        runner = MissingReferenceAudit(self.root)
        return runner, runner.run(generated_at=FIXED_TIME)

    def test_aggregation_and_all_classifications(self):
        _, report = self.run_audit()
        summary = report["summary"]
        self.assertEqual(summary["unresolved_references"], 5)
        self.assertEqual(summary["unique_target_ids"], 4)
        self.assertEqual(summary["affected_documents"], 3)
        self.assertEqual(summary["by_target_type"], {"MAIL": 5})
        self.assertEqual(summary["by_relation"], {"ATTACHED_TO": 5})
        self.assertEqual(
            summary["classification_counts"],
            {
                "indexed_document_present": 1,
                "not_found": 1,
                "reference_only": 1,
                "retained_document_present": 1,
            },
        )

        targets = {row["target_source_id"]: row for row in report["targets"]}
        self.assertEqual(targets["mail_ref"]["reference_count"], 2)
        self.assertEqual(targets["mail_ref"]["affected_document_count"], 2)
        self.assertEqual(targets["mail_ref"]["classification"], "reference_only")
        self.assertEqual(targets["mail_ref"]["hit_roles"], {"reference": 1})
        self.assertEqual(
            targets["mail_retained"]["classification"], "retained_document_present"
        )
        self.assertEqual(
            targets["mail_indexed"]["classification"], "indexed_document_present"
        )
        self.assertEqual(targets["mail_missing"]["classification"], "not_found")
        self.assertEqual(targets["mail_missing"]["hit_count"], 0)

    def test_exact_match_does_not_accept_shared_prefix(self):
        self.clear_assets()
        self.write_jsonl("data/sanitized_v3/candidates.jsonl", [{
            "id": "mail_exact_extra",
            "text": SECRET_BODY,
        }])
        self.references = [self.reference("mail_exact", "doc_a", "REL_1")]
        self.write_references()
        _, report = self.run_audit()
        target = report["targets"][0]
        self.assertEqual(target["classification"], "not_found")
        self.assertEqual(target["hit_count"], 0)

    def test_reference_only_is_not_promoted_to_independent_document(self):
        _, report = self.run_audit()
        target = next(
            row for row in report["targets"]
            if row["target_source_id"] == "mail_ref"
        )
        self.assertEqual(target["classification"], "reference_only")
        self.assertEqual(target["evidence_assets"], [
            "data/attachments_v2/documents.jsonl"
        ])
        self.assertEqual(target["hits"][0]["field"], "parent_source_ids[0]")

    def test_combined_source_parent_is_reference_evidence(self):
        self.write_jsonl(
            "data/combined_sources_v1/engineering_candidates.jsonl",
            [{
                "id": "att_1_unit_0",
                "document_id": "att_1",
                "parent_source_ids": ["mail_ref"],
                "text": SECRET_BODY,
            }],
        )
        _, report = self.run_audit()
        target = next(
            row for row in report["targets"]
            if row["target_source_id"] == "mail_ref"
        )
        self.assertEqual(target["classification"], "reference_only")
        self.assertEqual(target["hit_roles"], {"reference": 2})
        self.assertEqual(target["evidence_assets"], [
            "data/attachments_v2/documents.jsonl",
            "data/combined_sources_v1/engineering_candidates.jsonl",
        ])
    def test_reports_are_stably_sorted(self):
        _, first = self.run_audit()
        _, second = self.run_audit()
        self.assertEqual(first, second)
        target_ids = [row["target_source_id"] for row in first["targets"]]
        self.assertEqual(target_ids, sorted(target_ids))
        self.assertEqual(
            [row["target_source_id"] for row in first["targets"]],
            ["mail_indexed", "mail_missing", "mail_ref", "mail_retained"],
        )

    def test_outputs_exclude_email_subject_and_body(self):
        runner, report = self.run_audit()
        json_path, csv_path = runner.write_reports(
            report, "out/audit.json", "out/audit.csv"
        )
        json_text = json_path.read_text(encoding="utf-8")
        csv_text = csv_path.read_text(encoding="utf-8-sig")
        for text in (json_text, csv_text):
            self.assertNotIn(SECRET_BODY, text)
            self.assertNotIn(SECRET_SUBJECT, text)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 4)
        self.assertIn("mail_indexed", {row["target_source_id"] for row in rows})

    def test_existing_output_is_never_overwritten(self):
        runner, report = self.run_audit()
        json_path, csv_path = runner.write_reports(
            report, "out/audit.json", "out/audit.csv"
        )
        before = (json_path.read_bytes(), csv_path.read_bytes())
        with self.assertRaises(FileExistsError):
            runner.write_reports(report, "out/audit.json", "out/audit.csv")
        self.assertEqual(before, (json_path.read_bytes(), csv_path.read_bytes()))

    def test_inputs_and_original_mail_are_unchanged(self):
        input_paths = [
            self.root / "data/phase3_catalog_v1/unresolved_references.jsonl",
            self.root / "19924784-20240420.pst",
            *[self.root / spec.path for spec in ASSET_SPECS],
        ]
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in input_paths
        }
        runner, report = self.run_audit()
        runner.write_reports(report, "out/audit.json", "out/audit.csv")
        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in input_paths
        }
        self.assertEqual(before, after)
        integrity = report["integrity"]
        self.assertTrue(integrity["inputs_unchanged_during_audit"])
        self.assertFalse(integrity["catalog_modified"])
        self.assertTrue(integrity["original_mail"]["present"])
        self.assertFalse(integrity["original_mail"]["read"])
        self.assertTrue(integrity["original_mail"]["stat_unchanged_during_audit"])


class MissingReferenceProfileTests(unittest.TestCase):
    def test_v3_profile_uses_current_mail_and_catalog_sources(self):
        paths = {spec.path for spec in ASSET_SPECS_V3}
        self.assertIn("data/sanitized_v8/candidates.jsonl", paths)
        self.assertIn("data/sanitized_v9/candidates.jsonl", paths)
        self.assertIn("data/attachments_v2/occurrences.jsonl", paths)
        self.assertIn("data/phase3_catalog_v3/catalog.jsonl.gz", paths)
        self.assertIn("data/phase3_catalog_v3/source_map.jsonl.gz", paths)
        self.assertNotIn("data/sanitized_v3/candidates.jsonl", paths)
        self.assertEqual(
            PROFILE_DEFAULTS["v3"]["unresolved"],
            "data/phase3_catalog_v3/unresolved_references.jsonl.gz",
        )


if __name__ == "__main__":
    unittest.main()
