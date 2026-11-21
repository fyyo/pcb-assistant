"""Read-only tests for relationship paths and explainable case similarity."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import catalog_insights as insights
from catalog_build import PRIMARY_INPUTS, build, source_snapshots
from catalog_common import CatalogError, digest_file, stable_id
from catalog_insights import (_relationship_graph, relationship_paths, similar_cases,
    warm_insights, warm_relationship_graph, warm_similar_cases)
from .test_catalog import fixture, write_json, write_rows


def insight_fixture(root: Path) -> Path:
    """Add a few synthetic cases after the base fixture is complete."""
    fixture(root)
    cases_path = root / "data/knowledge_v1/cases.jsonl"
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]

    def case(source_id, subject, body, topics, quote):
        start = body.index(quote)
        return (
            {"source_id": source_id, "subject": subject, "topics": topics,
             "fields": {"phenomenon": [{"text": quote, "start": start,
                                        "end": start + len(quote)}]},
             "evidence_status": "unverified_email_discussion", "local_use_allowed": True,
             "manual_review_required": False, "independently_verified": False},
            (source_id, body),
        )

    extra = [
        case("mail_b", "\u5c42\u95f4\u5206\u5c42\u8bb0\u5f55",
             "\u5c42\u95f4\u5206\u5c42 0.5 MPa \u8bd5\u9a8c\u8bb0\u5f55",
             ["\u538b\u5408"], "\u5c42\u95f4\u5206\u5c42"),
        case("mail_c", "\u6e29\u5ea6\u8bb0\u5f55",
             "\u6e29\u5ea6 170 \u2103 \u4fdd\u6e29 0.8 MPa \u8bd5\u9a8c",
             ["\u6e29\u5ea6\u4e0e\u56fa\u5316"], "\u6e29\u5ea6"),
        case("mail_d", "a", "a", [], "a"),
    ]
    cases.extend(item[0] for item in extra)
    write_rows(root, "data/knowledge_v1/cases.jsonl", cases)
    with closing(sqlite3.connect(root / "data/knowledge_v1/knowledge.sqlite3")) as connection:
        connection.executemany("INSERT INTO cases VALUES (?,?)", [item[1] for item in extra])
        connection.commit()

    # The base fixture writes checksums before these additions. Refresh the
    # knowledge report so catalog_build can verify the unchanged synthetic inputs.
    report_path = root / "data/knowledge_v1/report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for relative in PRIMARY_INPUTS:
        if Path(relative).parent == Path("data/knowledge_v1"):
            report["artifacts"][Path(relative).name] = {"sha256": digest_file(root / relative)}
    write_json(report_path, report)

    output = root / "data/phase3_catalog_insights_test"
    with redirect_stdout(io.StringIO()):
        build(root, output)
    return output


class CatalogInsightsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcb-catalog-insights-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.output = insight_fixture(cls.root)
        cls.before_sources = source_snapshots(cls.root)
        cls.before_catalog = {path.name: (digest_file(path), path.stat().st_mtime_ns)
                              for path in cls.output.iterdir() if path.is_file()}

    def _clear_insight_caches(self, *, remove_persistent=False):
        insights._relationship_graph.cache_clear()
        insights._case_profiles.cache_clear()
        insights._case_similarity_index.cache_clear()
        insights._persistent_payload.cache_clear()
        insights._term_set_for_text.cache_clear()
        if remove_persistent:
            insights.insight_cache_path(self.output).unlink(missing_ok=True)

    def test_relationship_paths_find_one_two_and_three_hops(self):
        knowledge = stable_id("KNOWLEDGE", "attachment", "unit_a")
        attachment = stable_id("ATTACHMENT", "att_a")
        mail = stable_id("MAIL", "mail_a")
        case = stable_id("CASE", "mail_a")

        one = relationship_paths(self.output, knowledge, attachment)
        self.assertEqual(one["total"], 1)
        self.assertEqual(one["paths"][0]["length"], 1)
        self.assertEqual(one["paths"][0]["node_ids"], [knowledge, attachment])
        self.assertEqual(one["paths"][0]["edges"][0]["direction"], "forward")

        two = relationship_paths(self.output, knowledge, mail)
        self.assertEqual(two["paths"][0]["length"], 2)
        self.assertEqual(two["paths"][0]["node_ids"], [knowledge, attachment, mail])
        self.assertEqual([edge["relation"] for edge in two["paths"][0]["edges"]],
                         ["EXTRACTED_FROM", "ATTACHED_TO"])
        self.assertEqual([edge["direction"] for edge in two["paths"][0]["edges"]],
                         ["forward", "forward"])

        three = relationship_paths(self.output, knowledge, case, max_depth=3)
        self.assertEqual(three["total"], 1)
        self.assertEqual(three["paths"][0]["length"], 3)
        self.assertEqual(three["paths"][0]["node_ids"], [knowledge, attachment, mail, case])
        self.assertEqual([edge["relation"] for edge in three["paths"][0]["edges"]],
                         ["EXTRACTED_FROM", "ATTACHED_TO", "DERIVED_FROM"])
        self.assertEqual([edge["direction"] for edge in three["paths"][0]["edges"]],
                         ["forward", "forward", "reverse"])
        self.assertTrue(three["read_only"])
        self.assertFalse(three["generated_relationships"])
        self.assertFalse(three["unresolved_references_included"])
        self.assertEqual(three["traversal"], "undirected_registered_relationships")

    def test_path_search_respects_depth_target_and_self(self):
        knowledge = stable_id("KNOWLEDGE", "attachment", "unit_a")
        case = stable_id("CASE", "mail_a")
        disconnected = stable_id("MAIL", "mail_b")

        too_short = relationship_paths(self.output, knowledge, case, max_depth=2)
        self.assertEqual(too_short["total"], 0)
        self.assertEqual(too_short["paths"], [])
        self.assertIsNotNone(too_short["to_document"])
        self.assertEqual(too_short["to_document"]["doc_id"], case)

        unreachable = relationship_paths(self.output, knowledge, disconnected, max_depth=5)
        self.assertEqual(unreachable["total"], 0)
        self.assertEqual(unreachable["paths"], [])

        same = relationship_paths(self.output, case, case)
        self.assertEqual(same["total"], 1)
        self.assertEqual(same["paths"][0]["length"], 0)
        self.assertEqual(same["paths"][0]["node_ids"], [case])
        self.assertEqual(same["paths"][0]["edges"], [])

        reachable = relationship_paths(self.output, knowledge, max_depth=3, limit=100)
        self.assertGreater(reachable["total"], 1)
        self.assertTrue(all(path["node_ids"][0] == knowledge for path in reachable["paths"]))
        self.assertTrue(all(path["length"] <= 3 for path in reachable["paths"]))

    def test_relationship_graph_warm_cache_is_reused(self):
        _relationship_graph.cache_clear()
        relationship_count = warm_relationship_graph(self.output)
        warmed = _relationship_graph.cache_info()
        self.assertGreater(relationship_count, 0)
        self.assertEqual(warmed.misses, 1)
        knowledge = stable_id("KNOWLEDGE", "attachment", "unit_a")
        case = stable_id("CASE", "mail_a")
        result = relationship_paths(self.output, knowledge, case, max_depth=3)
        self.assertEqual(result["paths"][0]["length"], 3)
        reused = _relationship_graph.cache_info()
        self.assertEqual(reused.misses, warmed.misses)
        self.assertGreater(reused.hits, warmed.hits)

    def test_path_validation_and_bounds(self):
        knowledge = stable_id("KNOWLEDGE", "attachment", "unit_a")
        missing = stable_id("MAIL", "missing_path_target")
        for kwargs in ({"max_depth": 0}, {"max_depth": 6}, {"max_depth": True},
                       {"limit": 0}, {"limit": 201}, {"limit": 1.5}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CatalogError):
                    relationship_paths(self.output, knowledge, **kwargs)
        with self.assertRaises(CatalogError):
            relationship_paths(self.output, "not-a-document")
        with self.assertRaises(CatalogError):
            relationship_paths(self.output, knowledge, "not-a-document")
        with self.assertRaises(CatalogError):
            relationship_paths(self.output, missing)

    def test_similar_cases_are_explainable_ranked_and_paginated(self):
        case = stable_id("CASE", "mail_a")
        self.assertGreater(warm_similar_cases(self.output), 0)
        result = similar_cases(self.output, case, limit=100)
        self.assertTrue(result["eligible"])
        self.assertTrue(result["derived_recommendation"])
        self.assertTrue(result["not_source_evidence"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["writes_catalog"])
        self.assertNotIn(case, [item["doc_id"] for item in result["results"]])

        same_source = next(item for item in result["results"]
                           if item["doc_id"] == stable_id("KNOWLEDGE", "case", "mail_a"))
        self.assertTrue(same_source["same_source"])
        self.assertEqual(same_source["score_components"]["same_source"], 3)
        self.assertEqual(same_source["score_components"]["shared_tags"], 4)
        self.assertGreaterEqual(same_source["score_components"]["shared_terms"], 1)
        self.assertEqual(same_source["score"], sum(same_source["score_components"].values()))
        self.assertIn("lamination", [tag["tag_id"] for tag in same_source["shared_tags"]])
        self.assertTrue(same_source["shared_terms"])

        other_case = next(item for item in result["results"]
                          if item["doc_id"] == stable_id("CASE", "mail_b"))
        self.assertFalse(other_case["same_source"])
        self.assertEqual(other_case["score_components"]["same_source"], 0)
        self.assertGreaterEqual(other_case["score_components"]["shared_tags"], 4)
        self.assertEqual(other_case["score"], sum(other_case["score_components"].values()))

        database = self.output / "catalog.sqlite3"
        stat = database.stat()
        index = insights._case_similarity_index(
            str(self.output.resolve()), stat.st_size, stat.st_mtime_ns)
        source = next(profile for profile in index.profiles if profile.doc_id == case)
        source_tags = dict(source.tags)
        expected = []
        for candidate in index.profiles:
            if candidate.doc_id == source.doc_id:
                continue
            shared_tags = tuple(
                (tag_id, label) for tag_id, label in candidate.tags
                if tag_id in source_tags)
            shared_terms = candidate.terms & source.terms
            same_source = (candidate.source_type == source.source_type
                           and candidate.source_id == source.source_id)
            score = (len(shared_tags) * insights._TAG_POINTS
                     + len(shared_terms) * insights._TERM_POINTS
                     + (insights._SAME_SOURCE_POINTS if same_source else 0))
            if score > 0:
                expected.append((
                    -score, -len(shared_tags), -len(shared_terms), candidate.doc_id))
        expected.sort()
        self.assertLessEqual(result["total"], 100)
        self.assertEqual(result["total"], len(expected))
        self.assertEqual([item["doc_id"] for item in result["results"]],
                         [item[3] for item in expected])

        first = similar_cases(self.output, case, limit=1, offset=0)
        second = similar_cases(self.output, case, limit=1, offset=1)
        self.assertEqual(first["total"], second["total"])
        self.assertEqual(len(first["results"]), 1)
        self.assertEqual(len(second["results"]), 1)
        self.assertNotEqual(first["results"][0]["doc_id"], second["results"][0]["doc_id"])
        self.assertEqual(first["results"][0], result["results"][0])
        self.assertEqual(second["results"][0], result["results"][1])

    def test_similar_cases_require_a_case_and_safe_empty_profile(self):
        empty_case = stable_id("CASE", "mail_d")
        empty = similar_cases(self.output, empty_case, limit=100)
        self.assertTrue(empty["eligible"])
        self.assertEqual(empty["total"], 1)
        self.assertEqual(len(empty["results"]), 1)
        derived = empty["results"][0]
        self.assertTrue(derived["same_source"])
        self.assertEqual(derived["shared_tags"], [])
        self.assertEqual(derived["shared_terms"], [])
        self.assertEqual(derived["score"], 3)

        with self.assertRaises(CatalogError) as context:
            similar_cases(self.output, stable_id("MAIL", "mail_a"))
        self.assertEqual(str(context.exception), "similar_cases_require_case")
        with self.assertRaises(CatalogError):
            similar_cases(self.output, stable_id("MAIL", "not_retained"))
        with self.assertRaises(CatalogError):
            similar_cases(self.output, "not-a-document")
        for kwargs in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"offset": 10_000_001}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CatalogError):
                    similar_cases(self.output, empty_case, **kwargs)

    def test_async_warmup_is_single_flight_and_waiters_share_it(self):
        self._clear_insight_caches(remove_persistent=True)
        started = threading.Event()
        release = threading.Event()
        calls = []
        original = insights.warm_insights

        def delayed_warmup(directory):
            calls.append(Path(directory).resolve())
            started.set()
            release.wait(5)
            return original(directory)

        with patch("catalog_insights.warm_insights", side_effect=delayed_warmup):
            first = insights.warm_insights_async(self.output)
            self.assertTrue(started.wait(5))
            second = insights.warm_insights_async(self.output)
            self.assertIs(first, second)

            self.assertFalse(insights.wait_for_insights(self.output, timeout=0.05))
            self.assertTrue(first.is_alive())
            self.assertIs(insights.warm_insights_async(self.output), first)

            waiter_result = []
            waiter = threading.Thread(
                target=lambda: waiter_result.append(
                    insights.wait_for_insights(self.output, timeout=5)))
            waiter.start()
            self.assertTrue(waiter.is_alive())
            release.set()
            waiter.join(5)
            first.join(5)

        self.assertEqual(calls, [self.output.resolve()])
        self.assertFalse(waiter.is_alive())
        self.assertFalse(first.is_alive())
        self.assertEqual(waiter_result, [True])
    def test_persistent_insights_cache_is_outside_catalog_and_complete(self):
        self._clear_insight_caches(remove_persistent=True)
        counts = warm_insights(self.output)
        cache = insights.insight_cache_path(self.output)
        self.assertTrue(cache.is_file())
        self.assertFalse(cache.is_relative_to(self.output))
        self.assertEqual(cache.parent.name, insights._CACHE_DIRECTORY)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        database = self.output / "catalog.sqlite3"
        stat = database.stat()
        self.assertEqual(payload["schema"], insights._CACHE_SCHEMA)
        self.assertEqual(payload["catalog_path"], str(self.output.resolve()))
        self.assertEqual(payload["catalog_size"], stat.st_size)
        self.assertEqual(payload["catalog_mtime_ns"], stat.st_mtime_ns)
        self.assertEqual(payload["relationship_graph"]["relationship_count"],
                         counts["relationship_count"])
        self.assertEqual(len(payload["relationship_graph"]["edges"]),
                         counts["relationship_count"])
        self.assertEqual(len(payload["case_profiles"]), counts["case_count"])
        self.assertGreater(counts["relationship_count"], 0)
        self.assertGreater(counts["case_count"], 0)

    def test_persistent_insights_cache_loads_after_memory_cache_clear(self):
        self._clear_insight_caches(remove_persistent=True)
        counts = warm_insights(self.output)
        self._clear_insight_caches()
        with patch("catalog_insights.connect_readonly",
                   side_effect=AssertionError("database should not be opened")):
            loaded = warm_insights(self.output)
        self.assertEqual(loaded, counts)

    def test_corrupt_insights_cache_rebuilds_both_sections(self):
        self._clear_insight_caches(remove_persistent=True)
        counts = warm_insights(self.output)
        cache = insights.insight_cache_path(self.output)
        cache.write_text("{broken json", encoding="utf-8")
        self._clear_insight_caches()
        with patch("catalog_insights.connect_readonly",
                   wraps=insights.connect_readonly) as connect:
            rebuilt = warm_insights(self.output)
        self.assertEqual(rebuilt, counts)
        self.assertEqual(connect.call_count, 2)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        self.assertIn("relationship_graph", payload)
        self.assertIn("case_profiles", payload)

    def test_insights_cache_invalidates_on_catalog_snapshot_change(self):
        self._clear_insight_caches(remove_persistent=True)
        counts = warm_insights(self.output)
        cache = insights.insight_cache_path(self.output)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["catalog_size"] -= 1
        cache.write_text(json.dumps(payload), encoding="utf-8")
        self._clear_insight_caches()
        with patch("catalog_insights.connect_readonly",
                   wraps=insights.connect_readonly) as connect:
            rebuilt = warm_insights(self.output)
        self.assertEqual(rebuilt, counts)
        self.assertEqual(connect.call_count, 2)
        refreshed = json.loads(cache.read_text(encoding="utf-8"))
        stat = (self.output / "catalog.sqlite3").stat()
        self.assertEqual(refreshed["catalog_size"], stat.st_size)
        self.assertEqual(refreshed["catalog_mtime_ns"], stat.st_mtime_ns)

    def test_persistent_cache_rebuild_does_not_change_catalog_or_sources(self):
        self._clear_insight_caches(remove_persistent=True)
        warm_insights(self.output)
        insights.insight_cache_path(self.output).write_text("{broken json", encoding="utf-8")
        self._clear_insight_caches()
        warm_insights(self.output)
        self.assertEqual(source_snapshots(self.root), self.before_sources)
        after = {path.name: (digest_file(path), path.stat().st_mtime_ns)
                 for path in self.output.iterdir() if path.is_file()}
        self.assertEqual(after, self.before_catalog)

    def test_analysis_never_changes_sources_or_catalog(self):
        knowledge = stable_id("KNOWLEDGE", "attachment", "unit_a")
        case = stable_id("CASE", "mail_a")
        warm_relationship_graph(self.output)
        relationship_paths(self.output, knowledge, case, max_depth=5, limit=200)
        similar_cases(self.output, case, limit=200)
        self.assertEqual(source_snapshots(self.root), self.before_sources)
        after = {path.name: (digest_file(path), path.stat().st_mtime_ns)
                 for path in self.output.iterdir() if path.is_file()}
        self.assertEqual(after, self.before_catalog)


if __name__ == "__main__":
    unittest.main()
