"""Checks for the lamination agent's offline evaluation harness."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_common import CatalogError

from agent.cli import main
from agent.evaluation import (
    evaluate_file,
    load_suite,
    render_summary,
    write_report,
)


def _diagnosed_result():
    return {
        "status": "diagnosed",
        "defect": "white_spot",
        "gate": {"status": "accepted"},
        "hypotheses": [
            {
                "rank": 1,
                "cause_id": "prepreg_moisture",
                "supporting_evidence": [
                    {"citation_id": "EXC_1", "text": "PP 受潮会导致白斑。"}
                ],
            },
            {
                "rank": 2,
                "cause_id": "vacuum_pressure_loss",
                "supporting_evidence": [
                    {"citation_id": "EXC_2", "text": "真空不足会导致填胶不良。"}
                ],
            },
        ],
        "citations": [
            {"citation_id": "EXC_1", "text": "PP 受潮会导致白斑。"},
            {"citation_id": "EXC_2", "text": "真空不足会导致填胶不良。"},
        ],
        "missing_information": [{"code": "material", "prompt": "板材是什么？"}],
        "result_fingerprint": "fp-white",
        "answer": "候选方向",
    }


def _refused_result():
    return {
        "status": "refused",
        "defect": None,
        "gate": {"status": "refused"},
        "hypotheses": [],
        "citations": [],
        "missing_information": [],
        "result_fingerprint": "fp-refused",
        "answer": "不属于压合范围",
    }


def _fake_diagnose(case, **kwargs):
    if "白斑" in case.question:
        return _diagnosed_result()
    return _refused_result()


SUITE = [
    {
        "case_id": "white-1",
        "question": "压合后板边出现白斑，六层板。",
        "expected": {
            "gate_status": "accepted",
            "status": "diagnosed",
            "defect": "white_spot",
            "acceptable_causes": ["prepreg_moisture"],
            "required_missing_facts": ["material"],
            "require_citations": True,
        },
    },
    {
        "case_id": "out-1",
        "question": "明天上海天气怎么样？",
        "expected": {"gate_status": "refused", "defect": None},
    },
]


def _write_suite(path: Path, entries) -> None:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n",
        encoding="utf-8",
    )


class AgentEvaluationTests(unittest.TestCase):
    def test_suite_loads_both_flat_and_nested_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            _write_suite(path, [
                {"case_id": "flat", "question": "压合后白斑"},
                {"case_id": "nested", "case": {"question": "压合后分层", "defect": "分层"}},
            ])
            suite = load_suite(path)
        self.assertEqual([item.case_id for item in suite], ["flat", "nested"])
        self.assertEqual(suite[1].case.defect, "分层")

    def test_evaluation_reports_accuracy_and_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            _write_suite(path, SUITE)
            with patch("agent.evaluation.diagnose", side_effect=_fake_diagnose):
                report = evaluate_file(path, check_determinism=True)
        summary = report["summary"]
        self.assertEqual(summary["case_count"], 2)
        self.assertEqual(summary["labeled_case_count"], 2)
        self.assertEqual(summary["gate_status_accuracy"], 1.0)
        self.assertEqual(summary["status_accuracy"], 1.0)
        self.assertEqual(summary["defect_accuracy"], 1.0)
        self.assertEqual(summary["cause_top_1_accuracy"], 1.0)
        self.assertEqual(summary["cause_top_k_accuracy"], 1.0)
        self.assertEqual(summary["missing_fact_recall"], 1.0)
        self.assertEqual(summary["citation_requirement_pass_rate"], 1.0)
        self.assertEqual(summary["citation_integrity_pass_rate"], 1.0)
        self.assertEqual(summary["pass_rate"], 1.0)
        self.assertEqual(summary["determinism_pass_rate"], 1.0)
        self.assertEqual(summary["failed_case_ids"], [])
        self.assertFalse(report["boundaries"]["expert_gold"])
        self.assertFalse(report["boundaries"]["quality_claim_ready"])

    def test_unlabeled_suite_does_not_invent_accuracy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            _write_suite(path, [{"case_id": "plain", "question": "压合后白斑"}])
            with patch("agent.evaluation.diagnose", side_effect=_fake_diagnose):
                report = evaluate_file(path)
        summary = report["summary"]
        self.assertEqual(summary["labeled_case_count"], 0)
        self.assertIsNone(summary["pass_rate"])
        self.assertIsNone(summary["gate_status_accuracy"])
        self.assertIn("n/a", render_summary(report))

    def test_unlabeled_distribution_includes_cases_without_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            _write_suite(path, [
                {"case_id": "white-1", "question": "压合后白斑"},
                {"case_id": "white-2", "question": "压合后白斑"},
                {"case_id": "out", "question": "明天天气如何？"},
            ])
            with patch("agent.evaluation.diagnose", side_effect=_fake_diagnose):
                report = evaluate_file(path, check_determinism=True)
        summary = report["summary"]
        self.assertEqual(summary["top_cause_counts"], {"prepreg_moisture": 2})
        self.assertEqual(summary["top_cause_counts_by_defect"], {
            "white_spot": {"prepreg_moisture": 2}, "None": {},
        })
        self.assertEqual(summary["cases_without_hypotheses"], 1)
        self.assertEqual(summary["cases_with_citations"], 2)
        self.assertEqual(summary["determinism_pass_rate"], 1.0)
        self.assertIsNone(summary["cause_top_1_accuracy"])
        self.assertIsNone(summary["pass_rate"])
        self.assertNotIn("answer", report["cases"][0])
        self.assertNotIn("question", report["cases"][0])
        self.assertIn("首位候选分布", render_summary(report))

    def test_unknown_label_is_rejected_before_running(self):
        bad_entries = [
            {"case_id": "bad-field", "question": "压合后白斑", "expected": {"typo": True}},
            {"case_id": "bad-cause", "question": "压合后白斑", "expected": {"acceptable_causes": ["not_a_cause"]}},
            {"case_id": "empty-cause", "question": "压合后白斑", "expected": {"acceptable_causes": []}},
            {"case_id": "bad-match", "case": {"case_id": "other", "question": "压合后白斑"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, entry in enumerate(bad_entries):
                path = Path(directory) / f"bad-{index}.jsonl"
                _write_suite(path, [entry])
                with self.assertRaises(CatalogError):
                    load_suite(path)

    def test_duplicate_case_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            _write_suite(path, [
                {"case_id": "same", "question": "压合后白斑"},
                {"case_id": "same", "question": "压合后分层"},
            ])
            with self.assertRaises(CatalogError):
                load_suite(path)

    def test_report_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.json"
            write_report({"schema": "test", "summary": {"case_count": 1}}, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "test")
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_cli_validate_only_does_not_run_diagnosis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            _write_suite(path, SUITE)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                with patch("agent.evaluation.diagnose") as mocked:
                    code = main(["evaluate", "--cases", str(path), "--validate-only"])
        self.assertEqual(code, 0)
        self.assertIn("评测集有效", buffer.getvalue())
        self.assertIn("2 条用例", buffer.getvalue())
        mocked.assert_not_called()

    def test_cli_json_mode_returns_the_report(self):
        payload = {"schema": "lamination-agent-eval-v1", "summary": {"case_count": 0}}
        buffer = io.StringIO()
        with patch("agent.cli.evaluate_file", return_value=payload):
            with redirect_stdout(buffer):
                code = main(["evaluate", "--cases", "unused.jsonl", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue()), payload)

    def test_cli_invalid_suite_returns_two(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.jsonl"
            _write_suite(path, [{"case_id": "bad", "question": "压合后白斑", "expected": {"typo": True}}])
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(["evaluate", "--cases", str(path), "--validate-only"])
        self.assertEqual(code, 2)
        self.assertIn("unknown_expected_field", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
