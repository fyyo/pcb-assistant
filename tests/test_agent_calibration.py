"""Checks for real-case calibration export and import."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_common import CatalogError

from agent.calibration import (
    collect_candidates,
    export_reviews,
    export_unlabeled,
    import_reviews,
    select_candidates,
)
from agent.cli import main
from agent.evaluation import load_suite
from agent.taxonomy import DEFECT_MODES


def _field(items):
    return [{"text": text, "start": 0, "end": len(text)} for text in items]


def _record(source_id, subject, phenomenon, materials=(), conditions=(),
            analysis=(), actions=(), verification=(), topics=("压合",)):
    return {
        "source_id": source_id,
        "subject": subject,
        "fields": {
            "phenomenon": _field(phenomenon),
            "materials": _field(materials),
            "conditions": _field(conditions),
            "analysis": _field(analysis),
            "actions": _field(actions),
            "verification": _field(verification),
            "limitations": [],
        },
        "topics": list(topics),
        "evidence_status": "unverified_email_discussion",
        "independently_verified": False,
        "local_use_allowed": True,
        "manual_review_required": False,
    }


def _source_records():
    return [
        _record(
            "SRC_WHITE",
            "板邊白點異常",
            ["壓合後板邊出現白點異常，比例 2/20。"],
            materials=["FR-4，PP 1067"],
            conditions=["六層板，真空壓機"],
            analysis=["真空度不足導致白點。"],
            actions=["調整真空曲線並更換 PP。"],
            verification=["切片確認層間樹脂。"],
        ),
        _record(
            "SRC_DELAM",
            "板邊分層異常",
            ["壓合後板邊發現分層異常，比例 1/10。"],
            materials=["FR-4"],
            conditions=["六層板"],
            analysis=["PP 受潮導致分層。"],
            actions=["烘烤 PP 後重新壓合。"],
            verification=["切片確認分層界面。"],
        ),
        _record(
            "SRC_WEATHER",
            "明天天氣",
            ["明天上海天氣如何？"],
            topics=(),
        ),
        _record(
            "SRC_NO_REF",
            "空洞異常",
            ["壓合後板邊出現空洞異常。"],
        ),
    ]


def _write_jsonl(path: Path, entries) -> None:
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _complete(entry, **overrides):
    entry = json.loads(json.dumps(entry))
    review = entry["review"]
    review.update({
        "status": "complete",
        "reviewer": "expert-1",
        "reviewed_at": "2026-11-06T12:00:00+08:00",
    })
    review.update(overrides)
    return entry


class AgentCalibrationTests(unittest.TestCase):
    def test_unlabeled_export_needs_no_review_and_excludes_reference_and_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cases.jsonl"
            output = Path(directory) / "unlabeled.jsonl"
            _write_jsonl(source, _source_records()[:2])
            original = source.read_bytes()
            with patch("agent.calibration._lookup_doc_ids") as lookup:
                summary = export_unlabeled(source=source, limit=2, output=output)
            lookup.assert_not_called()
            exported = output.read_text(encoding="utf-8")
            entries = [json.loads(line) for line in exported.splitlines()]
            suite = load_suite(output)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(summary["labeled_case_count"], 0)
            self.assertEqual(len(suite), 2)
            self.assertTrue(all(not item.labeled for item in suite))
            self.assertTrue(all(set(item) == {"case_id", "case", "tags"} for item in entries))
            self.assertTrue(all("defect" not in item["case"] for item in entries))
            for marker in ("真空度不足", "調整真空", "切片確認", "acceptable_causes", "reviewer"):
                self.assertNotIn(marker, exported)
            with self.assertRaisesRegex(CatalogError, "calibration_output_exists"):
                export_unlabeled(source=source, limit=2, output=output)
            unsafe = Path.cwd() / "cases" / ".tmp_unlabeled_should_not_exist.jsonl"
            with self.assertRaisesRegex(CatalogError, "unsafe_calibration_output"):
                export_unlabeled(source=source, limit=2, output=unsafe)

    def test_cli_unlabeled_export_then_validate_without_import(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cases.jsonl"
            output = Path(directory) / "unlabeled.jsonl"
            _write_jsonl(source, _source_records()[:2])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main([
                    "calibrate", "export", "--unlabeled", "--source", str(source),
                    "--limit", "2", "--output", str(output), "--json",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["action"], "export_unlabeled")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["evaluate", "--cases", str(output), "--validate-only", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["valid"])
            self.assertEqual(payload["case_count"], 2)
            self.assertEqual(payload["labeled_case_count"], 0)

    def test_collect_candidates_filters_and_separates_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cases.jsonl"
            _write_jsonl(source, _source_records())
            candidates = collect_candidates(source)
            relaxed = collect_candidates(source, include_without_reference=True)

        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(relaxed), 3)
        candidate = next(item for item in candidates if item["review"]["defect"] == "white_spot")
        question = candidate["case"]["question"]
        self.assertIn("白點", question)
        self.assertIn("FR-4", question)
        self.assertNotIn("真空度不足", question)
        self.assertNotIn("調整真空", question)
        self.assertNotIn("切片確認", question)
        self.assertTrue(candidate["reference"]["analysis"])
        self.assertTrue(candidate["reference"]["actions"])
        self.assertGreaterEqual(candidate["quality"]["actual_hits"], 1)
        self.assertEqual(candidate["review"]["status"], "pending")
        self.assertEqual(candidate["prefill"]["defect"], "white_spot")
        self.assertIn("vacuum_pressure_loss", candidate["prefill"]["acceptable_causes"])
        self.assertEqual(len(candidate["source"]["content_sha256"]), 64)

    def test_select_candidates_keeps_defect_coverage(self):
        candidates = []
        for index, defect_id in enumerate(DEFECT_MODES):
            for variant in range(2):
                candidates.append({
                    "case_id": f"CAND_{index}_{variant}",
                    "review": {"defect": defect_id},
                    "quality": {
                        "score": 20 - variant,
                        "actual_hits": 1,
                        "admin_hits": 0,
                        "question_chars": 40,
                        "review_hint": "high",
                    },
                    "reference": {},
                })
        selected = select_candidates(candidates, 6)
        self.assertEqual(len(selected), 6)
        self.assertEqual(
            {item["review"]["defect"] for item in selected},
            set(DEFECT_MODES),
        )

    def test_export_refuses_unsafe_output_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cases.jsonl"
            output = Path(directory) / "review.jsonl"
            _write_jsonl(source, _source_records()[:1])
            summary = export_reviews(
                source=source, catalog=Path(directory) / "missing",
                limit=1, output=output,
            )
            self.assertEqual(summary["exported_count"], 1)
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 1)
            with self.assertRaises(CatalogError):
                export_reviews(
                    source=source, catalog=Path(directory) / "missing",
                    limit=1, output=output,
                )
            unsafe = Path.cwd() / "cases" / ".tmp_calibration_should_not_exist.jsonl"
            with self.assertRaises(CatalogError):
                export_reviews(
                    source=source, catalog=Path(directory) / "missing",
                    limit=1, output=unsafe,
                )
            self.assertFalse(unsafe.exists())

    def test_import_only_complete_reviews_and_drops_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cases.jsonl"
            review_path = Path(directory) / "review.jsonl"
            suite_path = Path(directory) / "suite.jsonl"
            _write_jsonl(source, _source_records()[:2])
            export_reviews(
                source=source, catalog=Path(directory) / "missing",
                limit=2, output=review_path,
            )
            entries = [
                json.loads(line)
                for line in review_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            completed = _complete(entries[0], defect="白點")
            pending = entries[1]
            _write_jsonl(review_path, [completed, pending])
            summary = import_reviews(review=review_path, output=suite_path)
            suite = load_suite(suite_path)

        self.assertEqual(summary["imported_count"], 1)
        self.assertEqual(summary["skipped_pending"], 1)
        self.assertEqual(len(suite), 1)
        self.assertEqual(suite[0].expected["defect"], "white_spot")
        self.assertNotIn("reference", suite[0].case.to_dict())
        self.assertNotIn("defect", suite[0].case.to_dict())
        self.assertIn("acceptable_causes", suite[0].expected)

    def test_import_rejects_incomplete_or_leaky_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cases.jsonl"
            review_path = Path(directory) / "review.jsonl"
            _write_jsonl(source, _source_records()[:1])
            export_reviews(
                source=source, catalog=Path(directory) / "missing",
                limit=1, output=review_path,
            )
            entry = json.loads(review_path.read_text(encoding="utf-8").splitlines()[0])

            _write_jsonl(review_path, [entry])
            with self.assertRaises(CatalogError):
                import_reviews(review=review_path, output=Path(directory) / "suite1.jsonl")

            missing_reviewer = _complete(entry)
            missing_reviewer["review"]["reviewer"] = ""
            _write_jsonl(review_path, [missing_reviewer])
            with self.assertRaises(CatalogError):
                import_reviews(review=review_path, output=Path(directory) / "suite2.jsonl")

            leaky = _complete(entry)
            leaky["case"]["notes"] = "这是分析结论"
            _write_jsonl(review_path, [leaky])
            with self.assertRaises(CatalogError):
                import_reviews(review=review_path, output=Path(directory) / "suite3.jsonl")

            bad_cause = _complete(entry, acceptable_causes=["not_a_cause"])
            _write_jsonl(review_path, [bad_cause])
            with self.assertRaises(CatalogError):
                import_reviews(review=review_path, output=Path(directory) / "suite4.jsonl")

            wrong_cause = _complete(entry, acceptable_causes=["warpage_cte_mismatch"])
            _write_jsonl(review_path, [wrong_cause])
            with self.assertRaises(CatalogError):
                import_reviews(review=review_path, output=Path(directory) / "suite5.jsonl")

    def test_cli_calibrate_export_and_import(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cases.jsonl"
            review_path = Path(directory) / "review.jsonl"
            suite_path = Path(directory) / "suite.jsonl"
            _write_jsonl(source, _source_records()[:2])

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main([
                    "calibrate", "export",
                    "--source", str(source),
                    "--catalog", str(Path(directory) / "missing"),
                    "--limit", "2",
                    "--output", str(review_path),
                    "--json",
                ])
            self.assertEqual(code, 0)
            export_payload = json.loads(buffer.getvalue())
            self.assertEqual(export_payload["exported_count"], 2)

            entries = [
                json.loads(line)
                for line in review_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            _write_jsonl(review_path, [_complete(entries[0]), entries[1]])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main([
                    "calibrate", "import",
                    "--review", str(review_path),
                    "--output", str(suite_path),
                    "--json",
                ])
            self.assertEqual(code, 0)
            import_payload = json.loads(buffer.getvalue())
            self.assertEqual(import_payload["imported_count"], 1)
            self.assertEqual(len(load_suite(suite_path)), 1)

    def test_cli_calibrate_import_error_returns_two(self):
        with tempfile.TemporaryDirectory() as directory:
            review_path = Path(directory) / "review.jsonl"
            _write_jsonl(review_path, [{"case_id": "x", "case": {"question": "白斑"}}])
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([
                    "calibrate", "import",
                    "--review", str(review_path),
                    "--output", str(Path(directory) / "suite.jsonl"),
                ])
        self.assertEqual(code, 2)
        self.assertIn("review_block_required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
