"""Read-only tests for recommendation quality evaluation aggregates."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from catalog_build import source_snapshots
from catalog_common import CatalogError, digest_file
from catalog_evaluation import evaluate_quality_gate, evaluate_similar_cases
from .test_catalog_insights import insight_fixture


class CatalogEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcb-catalog-evaluation-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.output = insight_fixture(cls.root)
        cls.before_sources = source_snapshots(cls.root)
        cls.before_catalog = {
            path.name: (digest_file(path), path.stat().st_mtime_ns)
            for path in cls.output.iterdir() if path.is_file()
        }

    def test_aggregate_metrics_are_complete_and_read_only(self):
        report = evaluate_similar_cases(self.output, sample_size=2, limit=5, seed=17)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["evaluation_kind"],
                         "internal_signal_consistency_not_human_relevance")
        self.assertEqual(report["sample"]["evaluated"], 2)
        self.assertGreaterEqual(report["sample"]["eligible_cases"], 2)
        self.assertEqual(report["sample"]["limit"], 5)
        self.assertGreater(report["warmup"]["case_profile_count"], 0)
        self.assertGreater(report["warmup"]["relationship_count"], 0)
        self.assertGreater(report["warmup"]["case_profile_count"], 0)
        self.assertGreaterEqual(report["signal_consistency"]["slots_returned"], 1)
        self.assertGreaterEqual(report["coverage"]["queries_with_results"], 1)
        self.assertIsNotNone(report["latency_ms"]["p50"])
        self.assertIsNotNone(report["latency_ms"]["p95"])
        self.assertEqual(report["invariants"]["failures"], 0)
        self.assertTrue(report["read_only"])
        self.assertFalse(report["catalog_writes"])
        self.assertFalse(report["contains_source_prose"])
        self.assertTrue(report["derived_recommendation"])
        self.assertTrue(report["not_source_evidence"])

    def test_sampling_is_deterministic_except_for_timing(self):
        first = evaluate_similar_cases(self.output, sample_size=3, limit=4, seed=23)
        second = evaluate_similar_cases(self.output, sample_size=3, limit=4, seed=23)
        stable_keys = ("evaluation_kind", "sample", "coverage", "signal_consistency",
                       "source_identity", "invariants", "read_only", "catalog_writes",
                       "contains_source_prose", "derived_recommendation",
                       "not_source_evidence")
        self.assertEqual({key: first[key] for key in stable_keys},
                         {key: second[key] for key in stable_keys})

    def test_invalid_parameters_are_rejected(self):
        for kwargs in ({"sample_size": 0}, {"sample_size": 1001},
                       {"sample_size": True}, {"limit": 0}, {"limit": 201},
                       {"limit": 1.5}, {"seed": -1}, {"seed": 2**32},
                       {"seed": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CatalogError):
                    evaluate_similar_cases(self.output, **kwargs)

    def test_evaluation_does_not_change_sources_or_catalog(self):
        evaluate_similar_cases(self.output, sample_size=2, limit=5, seed=31)
        self.assertEqual(source_snapshots(self.root), self.before_sources)
        after = {
            path.name: (digest_file(path), path.stat().st_mtime_ns)
            for path in self.output.iterdir() if path.is_file()
        }
        self.assertEqual(after, self.before_catalog)


    def test_automated_quality_gate_does_not_require_human_labels(self):
        report = evaluate_quality_gate(self.output, sample_size=2, limit=5, seed=17)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["evaluation_kind"],
                         "automated_proxy_not_human_validation")
        self.assertFalse(report["human_confirmation_required"])
        self.assertFalse(report["human_labels_used"])
        self.assertFalse(report["review_queue_required"])
        self.assertEqual(report["failures"], [])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["review_data_writes"])
        self.assertFalse(report["catalog_writes"])
        self.assertFalse(report["contains_source_prose"])
        self.assertFalse(report["contains_document_level_results"])
        self.assertFalse(report["labels_generated_by_model"])
        self.assertFalse(report["training_allowed"])

    def test_automated_quality_gate_fails_closed_on_regression(self):
        evaluation = evaluate_similar_cases(
            self.output, sample_size=2, limit=5, seed=17)
        regressed = copy.deepcopy(evaluation)
        regressed["invariants"]["failures"] = 1
        with patch("catalog_evaluation.evaluate_similar_cases",
                   return_value=regressed):
            report = evaluate_quality_gate(
                self.output, sample_size=2, limit=5, seed=17)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(item["check"] == "invariant_failures"
                            for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
