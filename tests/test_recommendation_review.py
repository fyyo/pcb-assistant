"""Tests for the isolated human review workflow."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from catalog_build import source_snapshots
from catalog_common import CatalogError, digest_file
from recommendation_review import (
    build_review_report, create_review_queue, review_evidence, review_next, submit_review,
)
from .test_catalog_insights import insight_fixture


class RecommendationReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcb-review-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.catalog = insight_fixture(cls.root)
        cls.before_sources = source_snapshots(cls.root)
        cls.before_catalog = {
            path.name: (digest_file(path), path.stat().st_mtime_ns)
            for path in cls.catalog.iterdir() if path.is_file()
        }

    def _root(self, name):
        return self.root / "data" / ("review_" + name)

    def _create(self, name="default", **kwargs):
        options = {"sample_size": 4, "limit": 2, "seed": 7,
                   "reviewers": ("R01", "R02")}
        options.update(kwargs)
        return create_review_queue(self.catalog, self._root(name), **options)

    def _first_item(self, root, queue_id, reviewer="R01"):
        return review_next(root, queue_id, reviewer, limit=1)["items"][0]

    def test_queue_is_deterministic_and_isolated(self):
        first_root = self._root("deterministic_a")
        second_root = self._root("deterministic_b")
        first = self._create("deterministic_a")
        second = self._create("deterministic_b")
        self.assertEqual(first["queue_id"], second["queue_id"])
        self.assertFalse(first_root.resolve().is_relative_to(self.catalog.resolve()))
        first_dir = first_root / first["queue_id"]
        second_dir = second_root / second["queue_id"]
        self.assertEqual((first_dir / "queue.jsonl").read_bytes(),
                         (second_dir / "queue.jsonl").read_bytes())
        self.assertEqual(json.loads((first_dir / "manifest.json").read_text(encoding="utf-8"))["queue_sha256"],
                         json.loads((second_dir / "manifest.json").read_text(encoding="utf-8"))["queue_sha256"])
        self.assertEqual(source_snapshots(self.root), self.before_sources)
        after = {path.name: (digest_file(path), path.stat().st_mtime_ns)
                 for path in self.catalog.iterdir() if path.is_file()}
        self.assertEqual(after, self.before_catalog)

    def test_report_is_awaiting_labels_and_has_no_document_level_data(self):
        created = self._create("awaiting")
        root = self._root("awaiting")
        report = build_review_report(root, created["queue_id"])
        self.assertEqual(report["status"], "awaiting_human_labels")
        self.assertEqual(report["coverage"]["submitted_slots"], 0)
        self.assertEqual(report["coverage"]["slot_coverage_rate"], 0.0)
        self.assertFalse(report["interpretation"]["human_labels_present"])
        self.assertEqual(report["interpretation"]["human_relevance_verdict"], "not_established")
        self.assertEqual(report["interpretation"]["engineering_validity_verdict"], "not_established")
        self.assertFalse(report["contains_source_prose"])
        self.assertFalse(report["contains_document_level_results"])
        serialized = json.dumps(report, ensure_ascii=False)
        queue_rows = [json.loads(line) for line in
                      (root / created["queue_id"] / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
        for row in queue_rows:
            self.assertNotIn(row["source_doc_id"], serialized)
            self.assertNotIn(row["candidate_doc_id"], serialized)

    def test_submit_duplicate_and_revision_are_audited(self):
        created = self._create("revision")
        root = self._root("revision")
        item = self._first_item(root, created["queue_id"])
        first = submit_review(
            root, created["queue_id"], item["review_id"], "R01",
            relevance_label="relevant", engineering_label="supported",
            reason_codes=["same_problem", "measured_values_present"])
        self.assertEqual(first["revision"], 1)
        with self.assertRaises(CatalogError) as duplicate:
            submit_review(
                root, created["queue_id"], item["review_id"], "R01",
                relevance_label="relevant", engineering_label="supported",
                reason_codes=["same_problem"])
        self.assertEqual(str(duplicate.exception), "duplicate_review_submission")
        revised = submit_review(
            root, created["queue_id"], item["review_id"], "R01",
            relevance_label="not_relevant", engineering_label="insufficient_evidence",
            reason_codes=["insufficient_context"], note="rechecked",
            expected_revision=1)
        self.assertEqual(revised["revision"], 2)
        report = build_review_report(root, created["queue_id"])
        self.assertEqual(report["coverage"]["submitted_slots"], 1)
        self.assertEqual(report["label_distribution"]["relevance"]["not_relevant"], 1)
        self.assertEqual(report["label_distribution"]["relevance"]["relevant"], 0)
        labels = [json.loads(line) for line in
                  (root / created["queue_id"] / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["revision"] for row in labels], [1, 2])

    def test_double_review_aggregation_has_agreement_and_consensus(self):
        created = self._create("agreement")
        root = self._root("agreement")
        items = review_next(root, created["queue_id"], "R01", limit=200)["items"]
        self.assertGreaterEqual(len(items), 2)
        for index, item in enumerate(items):
            if index == 1:
                labels = [
                    ("R01", "relevant", "supported"),
                    ("R02", "not_relevant", "insufficient_evidence"),
                ]
            else:
                labels = [
                    ("R01", "relevant", "supported"),
                    ("R02", "relevant", "supported"),
                ]
            for reviewer, relevance, engineering in labels:
                submit_review(
                    root, created["queue_id"], item["review_id"], reviewer,
                    relevance_label=relevance, engineering_label=engineering,
                    reason_codes=["same_problem"])
        report = build_review_report(root, created["queue_id"])
        self.assertEqual(report["status"], "review_complete_aggregate_only")
        self.assertEqual(report["coverage"]["slot_coverage_rate"], 1.0)
        self.assertEqual(report["coverage"]["fully_reviewed_items"], len(items))
        self.assertLess(report["agreement"]["relevance_exact_agreement_rate"], 1.0)
        self.assertIsNotNone(report["agreement"]["relevance_cohen_kappa"])
        self.assertGreaterEqual(report["consensus"]["split_relevance_items"], 1)
        self.assertEqual(report["agreement"]["relevance_pairs_compared"], len(items))

    def test_concurrent_duplicate_submission_is_blocked(self):
        created = self._create("concurrent")
        root = self._root("concurrent")
        item = self._first_item(root, created["queue_id"])

        def worker(_index):
            try:
                result = submit_review(
                    root, created["queue_id"], item["review_id"], "R01",
                    relevance_label="uncertain", engineering_label="plausible_unverified",
                    reason_codes=["insufficient_context"], expected_revision=0)
                return ("ok", result["revision"])
            except CatalogError as exc:
                return ("error", str(exc))

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(worker, range(4)))
        successes = [value for status, value in results if status == "ok"]
        errors = [value for status, value in results if status == "error"]
        self.assertEqual(successes, [1])
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(value in {"duplicate_review_submission", "review_store_busy"}
                            for value in errors))
        labels = (root / created["queue_id"] / "labels.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(labels), 1)

    def test_review_next_isolated_by_reviewer(self):
        created = self._create("next")
        root = self._root("next")
        item = self._first_item(root, created["queue_id"], "R01")
        submit_review(
            root, created["queue_id"], item["review_id"], "R01",
            relevance_label="partially_relevant", engineering_label="plausible_unverified",
            reason_codes=["generic_topic_only"])
        first = review_next(root, created["queue_id"], "R01", limit=200)
        second = review_next(root, created["queue_id"], "R02", limit=200)
        self.assertNotIn(item["review_id"], [value["review_id"] for value in first["items"]])
        self.assertIn(item["review_id"], [value["review_id"] for value in second["items"]])
        self.assertEqual(first["remaining"], first["total"] - 1)
        self.assertEqual(second["remaining"], second["total"])

    def test_review_evidence_returns_source_and_candidate(self):
        created = self._create("evidence")
        root = self._root("evidence")
        item = self._first_item(root, created["queue_id"])
        evidence = review_evidence(
            self.catalog, root, created["queue_id"], item["review_id"], "R01")
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["queue_id"], created["queue_id"])
        self.assertEqual(evidence["review_id"], item["review_id"])
        self.assertEqual(evidence["reviewer_id"], "R01")
        self.assertEqual(evidence["source_document"]["doc_id"], item["source_doc_id"])
        self.assertEqual(evidence["candidate_document"]["doc_id"], item["candidate_doc_id"])
        self.assertEqual(evidence["source_document"]["type"], "CASE")
        self.assertTrue(evidence["local_review_only"])
        self.assertTrue(evidence["read_only"])
        self.assertTrue(evidence["contains_source_prose"])
        self.assertFalse(evidence["catalog_writes"])
        self.assertFalse(evidence["review_data_writes"])
        self.assertFalse(evidence["labels_generated_by_model"])
        self.assertNotIn("source_path", json.dumps(evidence, ensure_ascii=False))

    def test_review_evidence_rejects_unassigned_reviewer(self):
        created = self._create("evidence_reviewer")
        root = self._root("evidence_reviewer")
        item = self._first_item(root, created["queue_id"])
        with self.assertRaises(CatalogError) as context:
            review_evidence(
                self.catalog, root, created["queue_id"], item["review_id"], "R03")
        self.assertEqual(str(context.exception), "reviewer_not_assigned")

    def test_review_evidence_rejects_missing_and_cross_queue_items(self):
        first = self._create("evidence_cross_a", seed=7)
        second = self._create("evidence_cross_b", seed=8)
        first_root = self._root("evidence_cross_a")
        second_root = self._root("evidence_cross_b")
        first_item = self._first_item(first_root, first["queue_id"])
        with self.assertRaises(CatalogError) as missing:
            review_evidence(
                self.catalog, second_root, second["queue_id"],
                first_item["review_id"], "R01")
        self.assertEqual(str(missing.exception), "review_item_not_found")
        with self.assertRaises(CatalogError) as invalid:
            review_evidence(
                self.catalog, first_root, first["queue_id"], "RITEM_not_an_id", "R01")
        self.assertEqual(str(invalid.exception), "invalid_review_id")

    def test_review_evidence_is_read_only_for_catalog_sources_and_labels(self):
        created = self._create("evidence_readonly")
        root = self._root("evidence_readonly")
        item = self._first_item(root, created["queue_id"])
        labels_path = root / created["queue_id"] / "labels.jsonl"
        before_labels = (labels_path.read_bytes(), labels_path.stat().st_mtime_ns)
        evidence = review_evidence(
            self.catalog, root, created["queue_id"], item["review_id"], "R01",
            max_chars=1000)
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(source_snapshots(self.root), self.before_sources)
        after_catalog = {path.name: (digest_file(path), path.stat().st_mtime_ns)
                         for path in self.catalog.iterdir() if path.is_file()}
        self.assertEqual(after_catalog, self.before_catalog)
        self.assertEqual((labels_path.read_bytes(), labels_path.stat().st_mtime_ns),
                         before_labels)

    def test_review_evidence_truncates_each_document_text(self):
        created = self._create("evidence_truncate")
        root = self._root("evidence_truncate")
        items = review_next(root, created["queue_id"], "R01", limit=200)["items"]
        evidence = None
        for item in items:
            candidate = review_evidence(
                self.catalog, root, created["queue_id"], item["review_id"], "R01",
                max_chars=5)
            if candidate["source_document"]["text_characters"] > 5:
                evidence = candidate
                break
        self.assertIsNotNone(evidence)
        for key in ("source_document", "candidate_document"):
            document = evidence[key]
            self.assertLessEqual(len(document["text"]), 5)
            self.assertEqual(document["returned_characters"], len(document["text"]))
            self.assertEqual(document["text_truncated"],
                             document["text_characters"] > 5)
        self.assertTrue(evidence["source_document"]["text_truncated"])
        self.assertTrue(evidence["text_truncated"])

    def test_review_evidence_does_not_return_other_reviewer_labels(self):
        created = self._create("evidence_labels")
        root = self._root("evidence_labels")
        item = self._first_item(root, created["queue_id"])
        submit_review(
            root, created["queue_id"], item["review_id"], "R02",
            relevance_label="not_relevant", engineering_label="insufficient_evidence",
            reason_codes=["insufficient_context"], note="R02_PRIVATE_REVIEW_NOTE")
        evidence = review_evidence(
            self.catalog, root, created["queue_id"], item["review_id"], "R01")
        serialized = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn("R02_PRIVATE_REVIEW_NOTE", serialized)
        self.assertNotIn("relevance_label", serialized)
        self.assertNotIn("engineering_label", serialized)
        self.assertNotIn("reviewer_labels", serialized)

    def test_review_evidence_does_not_change_aggregate_report_boundary(self):
        created = self._create("evidence_report")
        root = self._root("evidence_report")
        item = self._first_item(root, created["queue_id"])
        evidence = review_evidence(
            self.catalog, root, created["queue_id"], item["review_id"], "R01")
        report = build_review_report(root, created["queue_id"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(evidence["source_document"]["doc_id"], serialized)
        self.assertNotIn(evidence["candidate_document"]["doc_id"], serialized)
        self.assertFalse(report["contains_source_prose"])
        self.assertFalse(report["contains_document_level_results"])
        self.assertEqual(report["status"], "awaiting_human_labels")

    def test_invalid_inputs_and_catalog_overlap_are_rejected(self):
        created = self._create("invalid")
        root = self._root("invalid")
        item = self._first_item(root, created["queue_id"])
        cases = [
            {"reviewer_id": "bad reviewer"},
            {"relevance_label": "good"},
            {"engineering_label": "verified"},
            {"reason_codes": []},
            {"reason_codes": ["same_problem", "same_problem"]},
            {"note": "x" * 2001},
            {"expected_revision": True},
        ]
        base = dict(reviewer_id="R01", relevance_label="relevant",
                    engineering_label="supported", reason_codes=["same_problem"])
        for change in cases:
            values = dict(base)
            values.update(change)
            with self.subTest(change=change):
                with self.assertRaises(CatalogError):
                    submit_review(root, created["queue_id"], item["review_id"], **values)
        with self.assertRaises(CatalogError) as overlap:
            create_review_queue(self.catalog, self.catalog / "review",
                                sample_size=1, limit=1, reviewers=("R01",))
        self.assertEqual(str(overlap.exception), "review_directory_overlaps_catalog")


if __name__ == "__main__":
    unittest.main()
