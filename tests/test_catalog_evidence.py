"""Read-only evidence package checks over the synthetic unified catalog."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_build import build
from catalog_common import CatalogError, connect_readonly, digest_file
from catalog_evidence import (
    FINGERPRINT_ALGORITHM,
    EXCERPT_OFFSET_UNIT,
    MAX_EXCERPT_CHARS,
    MAX_EXCERPTS_PER_SOURCE,
    _excerpt_status,
    _excerpts_for_text,
    evidence,
    export_evidence,
    fingerprint_result,
)
from knowledge_catalog import main as catalog_main, status
from pcb_knowledge import field_coverage
from .test_catalog import fixture


class CatalogEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcb-catalog-evidence-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        cls.output = cls.root / "data/phase3_catalog_evidence_test"
        with redirect_stdout(io.StringIO()):
            build(cls.root, cls.output)

    def body_text(self, doc_id):
        with closing(connect_readonly(self.output)) as connection:
            row = connection.execute("""SELECT COALESCE(c.normalized_text,c.original_text)
                FROM documents d JOIN contents c USING(content_id) WHERE d.doc_id=?""",
                (doc_id,)).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_same_source_representations_are_aggregated_and_traceable(self):
        result = evidence(self.output, "\u5206\u5c42", limit=10)
        self.assertEqual(result["status"], "evidence_found")
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["source_count_in_window"], 1)
        self.assertEqual(result["candidate_representations"], 3)
        self.assertFalse(result["candidate_window_truncated"])
        self.assertFalse(result["generated_by_model"])
        self.assertFalse(result["human_confirmation_required"])
        self.assertTrue(result["source_text_is_untrusted_data"])
        self.assertTrue(result["retrieval_only"])
        self.assertFalse(result["engineering_conclusion_supported"])

        item = result["results"][0]
        self.assertEqual(item["source_id"], "mail_a")
        self.assertEqual(item["representation_count_in_window"], 3)
        self.assertEqual({row["type"] for row in item["representations"]},
                         {"MAIL", "CASE", "KNOWLEDGE"})
        self.assertEqual(sum(row["is_representative"] for row in item["representations"]), 1)
        self.assertEqual(item["citation"]["source_id"], "mail_a")
        self.assertIn("line", item["citation"]["locator"])
        self.assertIn("\u5206\u5c42", item["preview"])
        self.assertIn("\u5206\u5c42", item["matched_terms"])
        self.assertFalse(item["independently_verified"])

    def test_retrieval_query_isolates_discriminative_terms_for_retrieval(self):
        converged = evidence(self.output, "分层 的不足会造成什么缺陷", limit=3)
        plain = evidence(self.output, "分层", limit=3)

        self.assertEqual(converged["query"], "分层 的不足会造成什么缺陷")
        self.assertEqual(converged["retrieval_query"], "分层")
        self.assertEqual(converged["status"], "evidence_found")
        self.assertEqual(converged["match_terms"], plain["match_terms"])
        self.assertEqual(converged["results"], plain["results"])
        self.assertNotIn("不足", converged["match_terms"])

    def test_retrieval_query_keeps_the_literal_query_when_no_term_remains(self):
        result = evidence(self.output, "不足", limit=3)
        self.assertEqual(result["retrieval_query"], "不足")
        self.assertEqual(result["match_terms"], ["不足"])
        self.assertEqual(result["status"], "no_evidence")

    def test_retrieval_query_is_reported_for_simple_and_empty_retrievals(self):
        simple = evidence(self.output, "170", limit=3)
        self.assertEqual(simple["retrieval_query"], "170")
        empty = evidence(self.output, "alpha", limit=3)
        self.assertEqual(empty["status"], "no_evidence")
        self.assertEqual(empty["retrieval_query"], "alpha")

    def test_optional_core_term_reranking_prefers_concept_coverage(self):
        root = Path(self.temp.name) / "ranking"
        fixture(root)
        candidates_path = root / "data/sanitized_v3/candidates.jsonl"
        rows = [json.loads(line) for line in candidates_path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        rows.extend([
            {
                "id": "rank_a", "subject": "alpha beta", "text": "alpha beta 压合记录",
                "topics": ["压合"], "source_kind": "untrusted_email",
                "review_required": True, "training_allowed": False,
            },
            {
                "id": "rank_b", "subject": "alpha", "text": "alpha " * 30,
                "topics": ["压合"], "source_kind": "untrusted_email",
                "review_required": True, "training_allowed": False,
            },
        ])
        candidates_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8", newline="\n")
        report_path = candidates_path.parent / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifacts"]["candidates.jsonl"]["sha256"] = digest_file(candidates_path)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8", newline="\n")
        output = root / "data/phase3_catalog_evidence_ranking"
        with redirect_stdout(io.StringIO()):
            build(root, output)

        baseline = evidence(output, "alpha", limit=5)
        ranked = evidence(
            output, "alpha", limit=5,
            ranking_terms=(("alpha",), ("beta",)))

        self.assertNotIn("ranking", baseline)
        self.assertNotIn("ranking_coverage", baseline["results"][0])
        self.assertEqual(ranked["ranking"], {
            "basis": "core_term_coverage_then_bm25",
            "term_groups": [["alpha"], ["beta"]],
        })
        self.assertEqual(ranked["results"][0]["title"], "alpha beta")
        self.assertEqual(ranked["results"][0]["ranking_coverage"], 2)
        self.assertEqual(
            ranked["results"][0]["ranking_basis"],
            "core_term_coverage_then_fts5_bm25_best_representation_then_source_id")

    def test_ranking_terms_are_validated(self):
        invalid_values = (
            "alpha",
            (("alpha", 1),),
            (("",),),
            (("x" * 41,),),
            tuple((f"term{i}",) for i in range(13)),
        )
        for ranking_terms in invalid_values:
            with self.subTest(ranking_terms=ranking_terms):
                with self.assertRaisesRegex(
                        CatalogError, "invalid_evidence_ranking_terms"):
                    evidence(self.output, "分层", ranking_terms=ranking_terms)

    def test_ranking_pool_falls_back_when_group_has_no_fts_match(self):
        result = evidence(
            self.output, "\u5206\u5c42", limit=5,
            ranking_terms=(("term_not_in_catalog",),))

        self.assertEqual(result["status"], "evidence_found")
        self.assertGreater(result["returned"], 0)
        self.assertEqual(result["results"][0]["ranking_coverage"], 0)

    def test_ranking_pool_honors_document_filters(self):
        result = evidence(
            self.output, "\u5206\u5c42", doc_type="CASE", limit=5,
            ranking_terms=(("\u5206\u5c42", "\u5206\u5c64"),))

        self.assertEqual(result["status"], "evidence_found")
        self.assertEqual(result["filters"]["doc_type"], "CASE")
        self.assertTrue(result["results"])
        self.assertTrue(all(
            representation["type"] == "CASE"
            for item in result["results"]
            for representation in item["representations"]))

    def test_excerpts_are_exact_hashed_line_located_and_deterministic(self):
        first = evidence(self.output, "\u5206\u5c42", limit=10)
        second = evidence(self.output, "\u5206\u5c42", limit=10)
        item = first["results"][0]
        body = self.body_text(item["doc_id"])

        self.assertEqual(first["schema"], "catalog-evidence-v2")
        self.assertEqual(first["excerpt_policy"], {
            "max_excerpts_per_source": MAX_EXCERPTS_PER_SOURCE,
            "max_characters": MAX_EXCERPT_CHARS,
            "offset_unit": EXCERPT_OFFSET_UNIT,
            "selection_basis": "literal_match_terms_sentence_window",
        })
        self.assertEqual(item["excerpt_status"], "excerpts_found")
        self.assertGreater(item["excerpt_count"], 0)
        self.assertLessEqual(item["excerpt_count"], MAX_EXCERPTS_PER_SOURCE)
        self.assertEqual(item["excerpt_count"], len(item["excerpts"]))
        self.assertEqual(item["excerpt_text_sha256"],
                         hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(item["excerpts"], second["results"][0]["excerpts"])

        previous_end = -1
        for excerpt in item["excerpts"]:
            start, end = excerpt["start"], excerpt["end"]
            self.assertEqual(excerpt["offset_unit"], EXCERPT_OFFSET_UNIT)
            self.assertRegex(excerpt["excerpt_id"], r"^EXC_[0-9a-f]{24}$")
            self.assertRegex(excerpt["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(excerpt["text"], body[start:end])
            self.assertEqual(excerpt["sha256"],
                             hashlib.sha256(excerpt["text"].encode("utf-8")).hexdigest())
            self.assertLessEqual(end - start, MAX_EXCERPT_CHARS)
            self.assertGreaterEqual(start, previous_end)
            self.assertEqual(excerpt["line_start"],
                             body.count("\n", 0, start) + 1)
            self.assertEqual(excerpt["line_end"],
                             body.count("\n", 0, max(start, end - 1)) + 1)
            for term in excerpt["matched_terms"]:
                self.assertIn(term.casefold(), excerpt["text"].casefold())
            previous_end = end
        self.assertEqual(first["excerpt_count"],
                         sum(row["excerpt_count"] for row in first["results"]))
        self.assertEqual(first["sources_with_excerpts"],
                         sum(bool(row["excerpts"]) for row in first["results"]))

    def test_original_text_variant_hashes_the_actual_original_body(self):
        result = evidence(self.output, "\u5c42\u504f", limit=10)
        self.assertEqual(result["status"], "evidence_found")
        item = result["results"][0]
        with closing(connect_readonly(self.output)) as connection:
            row = connection.execute("""SELECT c.original_text,c.normalized_text
                FROM documents d JOIN contents c USING(content_id) WHERE d.doc_id=?""",
                (item["doc_id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["normalized_text"])
        self.assertEqual(item["excerpt_text_variant"], "original")
        self.assertEqual(item["excerpt_text_sha256"],
                         hashlib.sha256(row["original_text"].encode("utf-8")).hexdigest())
        for excerpt in item["excerpts"]:
            self.assertEqual(excerpt["text"],
                             row["original_text"][excerpt["start"]:excerpt["end"]])

    def test_excerpt_bounds_are_bounded_non_overlapping_and_status_is_explicit(self):
        text = "甲" * 80 + "分层" + "乙" * 100 + "。" + "丙" * 80 + \
            "分层" + "丁" * 100 + "。" + "戊" * 80 + "分层" + "己" * 100
        excerpts = _excerpts_for_text(text, ["分层"], "synthetic-content")
        self.assertEqual(len(excerpts), MAX_EXCERPTS_PER_SOURCE)
        self.assertTrue(all(item["end"] - item["start"] <= MAX_EXCERPT_CHARS
                            for item in excerpts))
        self.assertTrue(all(left["end"] <= right["start"]
                            for left, right in zip(excerpts, excerpts[1:])))
        self.assertEqual(_excerpt_status(text, excerpts), "excerpts_found")
        self.assertEqual(_excerpt_status("", []), "no_body_text")
        self.assertEqual(_excerpt_status(" \r\n\t", []), "no_body_text")
        self.assertEqual(_excerpt_status("正文没有字面命中", []),
                         "no_literal_excerpt_match")

    def test_no_evidence_is_explicit(self):
        result = evidence(self.output, "???", limit=10)
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(result["no_evidence_reason"], "no_matching_catalog_records")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["candidate_representations"], 0)
        self.assertEqual(result["source_count_in_window"], 0)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["page_source_count"], 0)
        self.assertIsNone(result["next_offset"])
        self.assertFalse(result["has_more_in_window"])
        self.assertEqual(result["pagination_scope"],
                         "bounded_candidate_window_not_full_catalog")

    def test_limit_and_filter_boundaries_are_validated(self):
        for limit in (0, 51, True):
            with self.assertRaises(CatalogError):
                evidence(self.output, "\u5206\u5c42", limit=limit)
        for offset in (-1, 401, True, "1", 1.5):
            with self.assertRaises(CatalogError):
                evidence(self.output, "\u5206\u5c42", offset=offset)
        with self.assertRaises(CatalogError):
            evidence(self.output, "   ")
        with self.assertRaises(CatalogError):
            evidence(self.output, "\u5206\u5c42", doc_type="INVALID")
        with self.assertRaises(CatalogError):
            evidence(self.output, "\u5206\u5c42", source_type="invalid")
        with self.assertRaises(CatalogError):
            evidence(self.output, "\u5206\u5c42", content_status="invalid")
        with self.assertRaises(CatalogError):
            evidence(self.output, "\u5206\u5c42", tags=["x"] * 21)

    def test_query_is_data_and_catalog_remains_unchanged(self):
        database = self.output / "catalog.sqlite3"
        before = digest_file(database)
        result = evidence(self.output, "') OR DROP TABLE documents; --", limit=3)
        self.assertIn(result["status"], {"evidence_found", "no_evidence"})
        self.assertEqual(digest_file(database), before)
        self.assertEqual(status(self.output)["totals"]["documents"], 12)

    def test_results_are_deterministic_and_window_is_bounded(self):
        first = evidence(self.output, "170", limit=2)
        second = evidence(self.output, "170", limit=2)
        self.assertEqual(first, second)
        self.assertEqual(first["returned"], 2)
        self.assertEqual(first["candidate_window_limit"], 200)
        self.assertEqual(first["source_count_in_window"], 3)
        self.assertEqual(first["candidate_representations"], 4)
        self.assertEqual(first["source_count_scope"],
                         "bounded_candidate_window_not_full_catalog")
        self.assertEqual(first["offset"], 0)
        self.assertEqual(first["page_source_count"], 2)
        self.assertEqual(first["next_offset"], 2)
        self.assertTrue(first["has_more_in_window"])
        self.assertEqual(first["pagination_scope"],
                         "bounded_candidate_window_not_full_catalog")

    def test_offset_paginates_aggregated_sources_and_distinguishes_out_of_range(self):
        first = evidence(self.output, "170", limit=1, offset=0)
        second = evidence(self.output, "170", limit=1, offset=1)
        last = evidence(self.output, "170", limit=2, offset=2)
        beyond = evidence(self.output, "170", limit=2, offset=3)

        self.assertEqual(first["status"], "evidence_found")
        self.assertEqual(second["status"], "evidence_found")
        self.assertEqual(first["source_count_in_window"], 3)
        self.assertEqual(second["source_count_in_window"], 3)
        self.assertEqual(first["page_source_count"], 1)
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(second["next_offset"], 2)
        self.assertNotEqual(first["results"][0]["source_id"],
                            second["results"][0]["source_id"])
        self.assertEqual(last["returned"], 1)
        self.assertEqual(last["page_source_count"], 1)
        self.assertIsNone(last["next_offset"])
        self.assertFalse(last["has_more_in_window"])

        self.assertEqual(beyond["status"], "page_out_of_range")
        self.assertEqual(beyond["returned"], 0)
        self.assertEqual(beyond["page_source_count"], 0)
        self.assertEqual(beyond["results"], [])
        self.assertFalse(beyond["has_more_in_window"])
        self.assertIsNone(beyond["next_offset"])
        self.assertEqual(beyond["source_count_in_window"], 3)
        self.assertEqual(beyond["candidate_representations"], 4)
        self.assertNotIn("no_evidence_reason", beyond)

        no_match = evidence(self.output, "???", limit=1, offset=2)
        self.assertEqual(no_match["status"], "no_evidence")
        self.assertEqual(no_match["offset"], 2)
        self.assertIsNone(no_match["next_offset"])
        self.assertFalse(no_match["has_more_in_window"])

    def test_result_fingerprint_is_deterministic_and_excludes_own_fields(self):
        first = evidence(self.output, "\u5206\u5c42", limit=10)
        second = evidence(self.output, "\u5206\u5c42", limit=10)
        self.assertEqual(first["result_fingerprint"], second["result_fingerprint"])
        self.assertEqual(first["fingerprint_algorithm"], FINGERPRINT_ALGORITHM)
        self.assertRegex(first["result_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertTrue(first["read_only"])

        altered = dict(first)
        altered["result_fingerprint"] = "0" * 64
        altered["fingerprint_algorithm"] = "different-algorithm"
        self.assertEqual(fingerprint_result(altered), first["result_fingerprint"])

        self.assertNotEqual(
            evidence(self.output, "170", limit=2)["result_fingerprint"],
            evidence(self.output, "170", limit=3)["result_fingerprint"])
        self.assertNotEqual(
            evidence(self.output, "170", limit=1, offset=0)["result_fingerprint"],
            evidence(self.output, "170", limit=1, offset=1)["result_fingerprint"])
        self.assertNotEqual(
            evidence(self.output, "\u5206\u5c42", limit=10)["result_fingerprint"],
            evidence(self.output, "\u5206\u5c42", doc_type="CASE", limit=10)["result_fingerprint"])

    def test_field_coverage_describes_the_returned_page_only(self):
        result = evidence(self.output, "\u5206\u5c42", limit=10)
        coverage = result["evidence_field_coverage"]

        self.assertEqual(coverage["basis"], "field_patterns_in_returned_excerpts")
        self.assertIn("phenomenon", coverage["covered"])
        self.assertFalse(set(coverage["labels"]) & set(coverage["missing_labels"]))
        self.assertEqual(coverage, field_coverage(
            [row["title"] + "\n" + excerpt["text"]
             for row in result["results"] for excerpt in row["excerpts"]]))
        self.assertEqual(fingerprint_result(result), result["result_fingerprint"])
        self.assertEqual(
            [item["label"] for item in coverage["recommended_inputs"]],
            coverage["missing_labels"])
        self.assertTrue(all(item["hint"]
                            for item in coverage["recommended_inputs"]))

        hot = evidence(self.output, "170", limit=10)
        self.assertIn("conditions", hot["evidence_field_coverage"]["covered"])
        self.assertEqual(hot["evidence_field_coverage"], field_coverage(
            [row["title"] + "\n" + excerpt["text"]
             for row in hot["results"] for excerpt in row["excerpts"]]))

        no_match = evidence(self.output, "???", limit=1)
        self.assertEqual(no_match["status"], "no_evidence")
        self.assertEqual(no_match["evidence_field_coverage"]["covered"], [])
        self.assertEqual(no_match["evidence_field_coverage"]["labels"], [])

        beyond = evidence(self.output, "\u5206\u5c42", limit=1, offset=10)
        self.assertEqual(beyond["status"], "page_out_of_range")
        self.assertEqual(beyond["evidence_field_coverage"]["covered"], [])
        self.assertTrue(beyond["evidence_field_coverage"]["missing_labels"])

    def test_export_writes_verified_local_json_without_changing_catalog(self):
        result = evidence(self.output, "\u5206\u5c42", limit=10)
        before = {path.name: (digest_file(path), path.stat().st_mtime_ns)
                  for path in self.output.iterdir() if path.is_file()}
        target = self.root / "evidence-export.json"
        receipt = export_evidence(result, target, catalog_directory=self.output)

        payload = target.read_bytes()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(receipt["status"], "exported")
        self.assertEqual(receipt["path"], str(target.resolve()))
        self.assertEqual(receipt["bytes"], len(payload))
        self.assertEqual(receipt["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(receipt["result_fingerprint"], result["result_fingerprint"])
        self.assertEqual(receipt["fingerprint_algorithm"], FINGERPRINT_ALGORITHM)
        self.assertTrue(receipt["read_only"])
        self.assertFalse(receipt["human_confirmation_required"])
        self.assertFalse(receipt["generated_by_model"])
        self.assertTrue(receipt["retrieval_only"])
        self.assertFalse(receipt["engineering_conclusion_supported"])
        self.assertEqual(json.loads(payload), result)

        after = {path.name: (digest_file(path), path.stat().st_mtime_ns)
                 for path in self.output.iterdir() if path.is_file()}
        self.assertEqual(after, before)

    def test_export_rejects_overwrite_unsafe_paths_and_stale_fingerprints(self):
        result = evidence(self.output, "\u5206\u5c42", limit=10)
        target = self.root / "existing-evidence.json"
        target.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "evidence_export_exists"):
            export_evidence(result, target, catalog_directory=self.output)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

        receipt = export_evidence(result, target, overwrite=True,
                                  catalog_directory=self.output)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         result)
        self.assertEqual(receipt["result_fingerprint"], result["result_fingerprint"])

        with self.assertRaisesRegex(CatalogError, "evidence_export_inside_catalog_forbidden"):
            export_evidence(result, self.output / "evidence.json",
                            catalog_directory=self.output)
        with self.assertRaisesRegex(CatalogError, "evidence_export_requires_json"):
            export_evidence(result, self.root / "evidence.txt",
                            catalog_directory=self.output)
        stale = dict(result)
        stale["returned"] = 999
        with self.assertRaisesRegex(CatalogError, "invalid_evidence_fingerprint"):
            export_evidence(stale, self.root / "stale.json",
                            catalog_directory=self.output)

    def test_cli_evidence_returns_json_without_human_gate(self):
        output = io.StringIO()
        argv = ["knowledge_catalog.py", "--catalog", str(self.output),
                "evidence", "170", "--limit", "1", "--offset", "1"]
        with patch("sys.argv", argv), redirect_stdout(output):
            self.assertEqual(catalog_main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "evidence_found")
        self.assertEqual(result["offset"], 1)
        self.assertEqual(result["next_offset"], 2)
        self.assertFalse(result["human_confirmation_required"])

    def test_cli_export_returns_local_receipt_without_source_preview(self):
        target = self.root / "cli-evidence.json"
        output = io.StringIO()
        argv = ["knowledge_catalog.py", "--catalog", str(self.output),
                "evidence", "\u5206\u5c42", "--limit", "1",
                "--export", str(target)]
        with patch("sys.argv", argv), redirect_stdout(output):
            self.assertEqual(catalog_main(), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "exported")
        self.assertNotIn("preview", output.getvalue())
        self.assertFalse(receipt["human_confirmation_required"])
        self.assertTrue(receipt["read_only"])
        exported = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(exported["result_fingerprint"], receipt["result_fingerprint"])
        self.assertFalse(exported["human_confirmation_required"])


if __name__ == "__main__":
    unittest.main()
