"""Read-only evidence-agent checks over the synthetic unified catalog."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_agent import (
    AGENT_FINGERPRINT_ALGORITHM,
    AGENT_SCHEMA,
    BOUNDARIES,
    ask_agent,
    export_agent,
    fingerprint_agent_result,
)
from catalog_build import build, source_snapshots
from catalog_common import CatalogError, digest_file
from catalog_evidence import evidence
from knowledge_catalog import main as catalog_main
from .test_catalog import fixture


class CatalogAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcb-catalog-agent-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        cls.source_before = source_snapshots(cls.root)
        cls.output = cls.root / "data/phase3_catalog_agent_test"
        with redirect_stdout(io.StringIO()):
            build(cls.root, cls.output)

    def catalog_snapshot(self):
        return {
            path.relative_to(self.output).as_posix(): (
                digest_file(path), path.stat().st_mtime_ns)
            for path in self.output.rglob("*") if path.is_file()
        }

    def test_summary_is_exact_extractive_text_with_valid_citations(self):
        result = ask_agent(self.output, "\u5206\u5c42", limit=10)
        self.assertEqual(result["schema"], AGENT_SCHEMA)
        self.assertEqual(result["status"], "evidence_found")
        self.assertEqual(result["answer_mode"], "extractive_only")
        self.assertNotIn("answer", result)
        self.assertTrue(result["summary"])

        excerpts = {
            excerpt["excerpt_id"]: excerpt
            for group in result["evidence_groups"]
            for excerpt in group["excerpts"]
        }
        for item in result["summary"]:
            self.assertEqual(len(item["citations"]), 1)
            citation = item["citations"][0]
            self.assertIn(citation, excerpts)
            self.assertEqual(item["text"], excerpts[citation]["text"])
            self.assertEqual(item["sha256"], excerpts[citation]["sha256"])
            self.assertEqual(item["start"], excerpts[citation]["start"])
            self.assertEqual(item["end"], excerpts[citation]["end"])

    def test_groups_preserve_primary_source_and_same_source_representations(self):
        result = ask_agent(self.output, "\u5206\u5c42", limit=10)
        self.assertEqual(len(result["evidence_groups"]), 1)
        group = result["evidence_groups"][0]
        self.assertEqual(group["source_id"], "mail_a")
        self.assertEqual(group["citation"]["source_id"], "mail_a")
        self.assertIn("line", group["citation"]["locator"])
        self.assertEqual({item["type"] for item in group["representations"]},
                         {"MAIL", "CASE", "KNOWLEDGE"})
        self.assertEqual(sum(item["is_representative"]
                             for item in group["representations"]), 1)

    def test_no_evidence_is_explicit_and_not_page_out_of_range(self):
        result = ask_agent(self.output, "zzzz_unmatched_zzzz", limit=10)
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(result["summary"], [])
        self.assertEqual(result["evidence_groups"], [])
        self.assertEqual(result["missing_information"],
                         ["no_matching_catalog_records"])
        self.assertNotIn("page_outside", result["missing_information"])
        self.assertIsNone(result["retrieval"]["next_offset"])

    def test_page_out_of_range_is_distinct_from_no_evidence(self):
        result = ask_agent(self.output, "170", limit=2, offset=3)
        self.assertEqual(result["status"], "page_out_of_range")
        self.assertEqual(result["summary"], [])
        self.assertEqual(result["evidence_groups"], [])
        self.assertEqual(result["missing_information"],
                         ["requested_page_outside_bounded_candidate_window"])
        self.assertNotIn("no_evidence_reason", result)
        self.assertFalse(result["retrieval"]["has_more_in_window"])
        self.assertIsNone(result["retrieval"]["next_offset"])

    def test_retrieval_offset_is_exposed_and_deterministic(self):
        first = ask_agent(self.output, "170", limit=1, offset=0)
        second = ask_agent(self.output, "170", limit=1, offset=1)
        self.assertEqual(first["status"], "evidence_found")
        self.assertEqual(second["status"], "evidence_found")
        self.assertEqual(first["retrieval"]["offset"], 0)
        self.assertEqual(second["retrieval"]["offset"], 1)
        self.assertEqual(first["retrieval"]["next_offset"], 1)
        self.assertEqual(second["retrieval"]["next_offset"], 2)
        self.assertNotEqual(first["evidence_groups"][0]["source_id"],
                            second["evidence_groups"][0]["source_id"])
        self.assertEqual(first["result_fingerprint"], ask_agent(
            self.output, "170", limit=1, offset=0)["result_fingerprint"])
        self.assertNotEqual(first["result_fingerprint"],
                            second["result_fingerprint"])

    def test_fixed_boundaries_are_top_level_and_nested(self):
        result = ask_agent(self.output, "\u5206\u5c42", limit=1)
        self.assertEqual(result["boundaries"], BOUNDARIES)
        for name, expected in BOUNDARIES.items():
            self.assertEqual(result[name], expected)
        self.assertFalse(result["generated_by_model"])
        self.assertTrue(result["source_text_is_untrusted_data"])
        self.assertFalse(result["engineering_conclusion_supported"])
        self.assertFalse(result["human_confirmation_required"])

    def test_field_coverage_matches_the_evidence_layer(self):
        result = ask_agent(self.output, "\u5206\u5c42", limit=10)
        evidence_result = evidence(self.output, "\u5206\u5c42", limit=10)

        self.assertEqual(result["evidence_field_coverage"],
                         evidence_result["evidence_field_coverage"])
        self.assertEqual(result["evidence_field_coverage"]["basis"],
                         "field_patterns_in_returned_excerpts")
        self.assertIn("phenomenon", result["evidence_field_coverage"]["covered"])
        self.assertEqual(
            [item["label"]
             for item in result["evidence_field_coverage"]["recommended_inputs"]],
            result["evidence_field_coverage"]["missing_labels"])
        self.assertEqual(result["result_fingerprint"],
                         fingerprint_agent_result(result))

    def test_fingerprint_covers_response_and_excludes_own_fields(self):
        result = ask_agent(self.output, "\u5206\u5c42", limit=1)
        self.assertEqual(result["fingerprint_algorithm"],
                         AGENT_FINGERPRINT_ALGORITHM)
        self.assertEqual(result["result_fingerprint"],
                         fingerprint_agent_result(result))
        changed = json.loads(json.dumps(result, ensure_ascii=False))
        changed["summary"] = []
        self.assertNotEqual(fingerprint_agent_result(changed),
                            result["result_fingerprint"])

    def test_question_limit_offset_and_max_sources_are_validated(self):
        for question in (None, "", "   ", "x" * 1001):
            with self.assertRaisesRegex(CatalogError, "invalid_agent_question"):
                ask_agent(self.output, question)
        for limit, offset in ((0, 0), (51, 0), (1, -1), (1, 401), (True, 0)):
            with self.assertRaisesRegex(CatalogError, "invalid_agent_pagination"):
                ask_agent(self.output, "\u5206\u5c42", limit=limit, offset=offset)
        for max_sources in (0, 51, True):
            with self.assertRaisesRegex(CatalogError, "invalid_agent_max_sources"):
                ask_agent(self.output, "\u5206\u5c42", max_sources=max_sources)
        capped = ask_agent(self.output, "170", limit=10, max_sources=1)
        self.assertEqual(capped["retrieval"]["limit"], 1)
        self.assertEqual(len(capped["evidence_groups"]), 1)

    def test_agent_calls_evidence_once_without_writing_catalog_or_sources(self):
        catalog_before = self.catalog_snapshot()
        with patch("catalog_agent.evidence", wraps=evidence) as wrapped:
            result = ask_agent(self.output, "\u5206\u5c42", limit=2)
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(wrapped.call_args.args[:2], (self.output, "\u5206\u5c42"))
        self.assertEqual(wrapped.call_args.kwargs["limit"], 2)
        self.assertEqual(result["status"], "evidence_found")
        self.assertEqual(self.catalog_snapshot(), catalog_before)
        self.assertEqual(source_snapshots(self.root), self.source_before)

    def test_agent_passes_ranking_terms_and_exposes_ranking_basis(self):
        ranking_terms = (("分层", "分層", "delamination"),)
        with patch("catalog_agent.evidence", wraps=evidence) as wrapped:
            result = ask_agent(
                self.output, "分层", limit=1, ranking_terms=ranking_terms)
        self.assertEqual(wrapped.call_args.kwargs["ranking_terms"], ranking_terms)
        self.assertEqual(result["retrieval"]["ranking"], {
            "basis": "core_term_coverage_then_bm25",
            "term_groups": [["分层", "分層", "delamination"]],
        })
        self.assertEqual(
            result["evidence_groups"][0]["citation"]["source_id"], "mail_a")

    def test_agent_reports_converged_retrieval_query_and_relevance(self):
        result = ask_agent(self.output, "分层 的不足会造成什么缺陷", limit=3)

        self.assertEqual(result["question"], "分层 的不足会造成什么缺陷")
        self.assertEqual(result["retrieval_query"], "分层")
        self.assertEqual(result["retrieval"]["queries"], ["分层"])
        self.assertEqual(result["evidence_relevance"], {
            "basis": "query_term_group_coverage",
            "term_groups": 1,
            "covered_groups": 1,
            "label": "strong",
        })

    def test_agent_derives_ranking_groups_from_the_question(self):
        with patch("catalog_agent.evidence", wraps=evidence) as wrapped:
            result = ask_agent(self.output, "分层 的不足会造成什么缺陷", limit=1)
        passed = wrapped.call_args.kwargs["ranking_terms"]
        self.assertIn(("分层", "分層", "层间分离", "層間分離", "delamination"), passed)
        self.assertNotIn(("不足",), passed)
        self.assertEqual(result["retrieval"]["ranking"]["term_groups"],
                         [list(group) for group in passed])

    def test_relevance_label_distinguishes_no_hits_from_unrated_questions(self):
        no_hits = ask_agent(self.output, "alpha 的不足造成什么缺陷", limit=3)
        self.assertEqual(no_hits["status"], "no_evidence")
        self.assertEqual(no_hits["retrieval_query"], "alpha")
        self.assertEqual(no_hits["evidence_relevance"]["covered_groups"], 0)
        self.assertEqual(no_hits["evidence_relevance"]["label"], "none")

        unrated = ask_agent(self.output, "不足", limit=3)
        self.assertEqual(unrated["retrieval_query"], "不足")
        self.assertEqual(unrated["evidence_relevance"]["term_groups"], 0)
        self.assertEqual(unrated["evidence_relevance"]["label"], "unrated")

    def test_relevance_label_marks_partially_covered_questions(self):
        result = ask_agent(self.output, "真空度 分层", limit=3)
        relevance = result["evidence_relevance"]
        self.assertGreater(relevance["term_groups"], 1)
        self.assertLess(relevance["covered_groups"], relevance["term_groups"])
        self.assertEqual(relevance["label"], "weak")

    def test_export_writes_verified_json_without_changing_catalog(self):
        result = ask_agent(self.output, "\u5206\u5c42", limit=1)
        before = self.catalog_snapshot()
        target = self.root / "agent-export.json"
        receipt = export_agent(result, target, catalog_directory=self.output)

        payload = target.read_bytes()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(json.loads(payload), result)
        self.assertEqual(receipt["status"], "exported")
        self.assertEqual(receipt["bytes"], len(payload))
        self.assertEqual(receipt["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(receipt["result_fingerprint"],
                         result["result_fingerprint"])
        self.assertTrue(receipt["read_only"])
        self.assertFalse(receipt["human_confirmation_required"])
        self.assertFalse(receipt["generated_by_model"])
        self.assertTrue(receipt["source_text_is_untrusted_data"])
        self.assertEqual(self.catalog_snapshot(), before)

    def test_export_rejects_unsafe_paths_and_stale_fingerprints(self):
        result = ask_agent(self.output, "\u5206\u5c42", limit=1)
        target = self.root / "existing-agent.json"
        target.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "agent_export_exists"):
            export_agent(result, target, catalog_directory=self.output)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

        export_agent(result, target, overwrite=True,
                     catalog_directory=self.output)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), result)
        with self.assertRaisesRegex(CatalogError,
                                    "agent_export_inside_catalog_forbidden"):
            export_agent(result, self.output / "agent.json",
                         catalog_directory=self.output)
        with self.assertRaisesRegex(CatalogError, "agent_export_requires_json"):
            export_agent(result, self.root / "agent.txt",
                         catalog_directory=self.output)
        stale = json.loads(json.dumps(result, ensure_ascii=False))
        stale["summary"] = []
        with self.assertRaisesRegex(CatalogError, "invalid_agent_fingerprint"):
            export_agent(stale, self.root / "stale-agent.json",
                         catalog_directory=self.output)

    def test_cli_agent_returns_json_and_local_export_receipt(self):
        output = io.StringIO()
        argv = ["knowledge_catalog.py", "--catalog", str(self.output),
                "agent", "170", "--limit", "1", "--offset", "1"]
        with patch("sys.argv", argv), redirect_stdout(output):
            self.assertEqual(catalog_main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schema"], AGENT_SCHEMA)
        self.assertEqual(result["status"], "evidence_found")
        self.assertEqual(result["retrieval"]["offset"], 1)
        self.assertFalse(result["human_confirmation_required"])
        self.assertFalse(result["generated_by_model"])

        target = self.root / "cli-agent.json"
        output = io.StringIO()
        argv = ["knowledge_catalog.py", "--catalog", str(self.output),
                "agent", "\u5206\u5c42", "--limit", "1",
                "--export", str(target)]
        with patch("sys.argv", argv), redirect_stdout(output):
            self.assertEqual(catalog_main(), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "exported")
        self.assertNotIn("summary", receipt)
        exported = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(exported["schema"], AGENT_SCHEMA)
        self.assertEqual(exported["result_fingerprint"],
                         receipt["result_fingerprint"])


if __name__ == "__main__":
    unittest.main()
