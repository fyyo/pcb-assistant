"""Offline tests for the Phase 6.3 evaluation runner."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from phase6_contract import load_cases
import phase6_quality as quality


class Phase6QualityTests(unittest.TestCase):
    def test_gate_only_subset_is_release_blocked_without_gold(self):
        cases = [case for case in load_cases()
                 if case["case_id"] in {"EVAL_0131", "EVAL_0141"}]
        summary = quality.evaluate_cases(cases, workers=1)
        self.assertFalse(summary["external_services"])
        self.assertEqual(summary["managed_requests"], 0)
        self.assertFalse(summary["dataset"]["gold_ready"])
        self.assertIsNone(summary["gold_metrics"])
        self.assertFalse(summary["release_ready"])
        self.assertIn("evidence_recall_at_8", summary["release_blockers"])
        self.assertEqual(summary["gate"]["status_counts"], {
            "no_evidence": 1,
            "policy_blocked": 1,
        })
        self.assertEqual(summary["gate"]["prompt_injection"]["rate"], 1.0)

    def test_gold_metrics_use_expert_ids_and_required_fields(self):
        cases = [copy.deepcopy(case) for case in load_cases()[:2]]
        for case in cases:
            case["annotation"].update({
                "status": "complete",
                "reviewer": "test-reviewer",
                "reviewed_at": "2026-10-26T00:00:00+08:00",
            })
            case["expected"].update({
                "should_answer": True,
                "required_evidence_ids": ["EXC_aaaaaaaaaaaaaaaaaaaaaaaa"],
                "required_fields": ["phenomenon", "conditions"],
            })
        cases.append({
            "case_id": "EVAL_9999",
            "category": "insufficient_evidence",
            "expected": {
                "status": "no_evidence",
                "should_answer": False,
                "required_evidence_ids": [],
                "required_fields": [],
            },
            "annotation": {"status": "complete", "reviewer": "test-reviewer"},
        })
        rows = [
            {
                "case_id": cases[0]["case_id"],
                "retrieved_excerpt_ids": ["EXC_aaaaaaaaaaaaaaaaaaaaaaaa"],
                "covered_fields": ["phenomenon", "conditions"],
                "predicted_status": "answered",
            },
            {
                "case_id": cases[1]["case_id"],
                "retrieved_excerpt_ids": ["EXC_bbbbbbbbbbbbbbbbbbbbbbbb"],
                "covered_fields": ["phenomenon"],
                "predicted_status": "answered",
            },
            {
                "case_id": "EVAL_9999",
                "retrieved_excerpt_ids": [],
                "covered_fields": [],
                "predicted_status": "no_evidence",
            },
        ]
        metrics = quality._gold_metrics(cases, rows)
        self.assertEqual(metrics["evidence_recall_at_8"]["value"], 0.5)
        self.assertEqual(metrics["required_field_coverage"]["value"], 0.75)
        self.assertEqual(metrics["no_evidence_precision"]["value"], 1.0)

    def test_p95_uses_nearest_rank(self):
        self.assertIsNone(quality._p95([]))
        self.assertEqual(quality._p95([1, 2, 3, 4, 100]), 100)
        self.assertEqual(quality._p95([1, 2, 3, 4]), 4)

    def test_evidence_p95_threshold_uses_seconds(self):
        case = next(
            case for case in load_cases() if case["case_id"] == "EVAL_0141")

        def summary_at(latency_ms):
            row = {
                "case_id": case["case_id"],
                "category": case["category"],
                "expected_status": case["expected"]["status"],
                "predicted_status": "policy_blocked",
                "gate_status": "policy_blocked",
                "retrieved_excerpt_ids": [],
                "covered_fields": [],
                "citation_validation": "passed",
                "invalid_citation_count": 0,
                "citation_count": 0,
                "generated_by_model": False,
                "external_services": False,
                "data_egress": "none",
                "latency_ms": latency_ms,
            }
            return quality._summarize([case], [row], limit=8, workers=1)

        passed = summary_at(1500)
        metric = next(
            item for item in passed["thresholds"]
            if item["metric"] == "evidence_p95_seconds")
        self.assertEqual(metric["value"], 1.5)
        self.assertEqual(metric["status"], "passed")

        failed = summary_at(2500)
        metric = next(
            item for item in failed["thresholds"]
            if item["metric"] == "evidence_p95_seconds")
        self.assertEqual(metric["value"], 2.5)
        self.assertEqual(metric["status"], "failed")

    def test_display_path_handles_relative_and_external_paths(self):
        self.assertEqual(
            quality._display_path(Path(".tmp_work/example.json")),
            ".tmp_work/example.json")
        with tempfile.TemporaryDirectory() as directory:
            rendered = quality._display_path(Path(directory) / "report.json")
            self.assertTrue(Path(rendered).is_absolute())


if __name__ == "__main__":
    unittest.main()
