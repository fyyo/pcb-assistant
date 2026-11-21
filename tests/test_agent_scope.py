"""Domain restrictions must apply before diagnostic scoring and citations."""
import json
from pathlib import Path
import tempfile
import unittest

from agent.models import fingerprint_result
from agent.providers import ExternalSearchAdapter
from agent.public_pack import PublicPackRetriever, build_pack
from agent.retriever import normalize_evidence
from agent.workflow import diagnose
from tests.test_agent_public_pack import archives
from tests.test_agent_workflow import FakeRetriever, evidence_item


REVIEWED_HASH = "b383ba2a9adbd0cdbd2e8a29aa38c5d5125972a1532143d5f167e38bccac1897"


def public_item(citation_id, text, *, title="CFRP patch repair study"):
    return {**evidence_item(citation_id, text, title=title), "source_type": "public_reference"}


class AgentScopeTests(unittest.TestCase):
    def test_cfrp_repair_cannot_supply_diagnosis_citations_or_scores(self):
        for question in ("压合后白斑怎么排查？", "压合后分层怎么排查？"):
            with self.subTest(question=question):
                item = public_item("EXC_cfrp", "固化不足导致层间分层，保温 130 min 后改善。")
                result = diagnose({"question": question}, retriever=FakeRetriever((("固化", [item]),)))
                self.assertEqual(result["citations"], [])
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertTrue(all(h["evidence_score"] == 0 for h in result["hypotheses"]))
                self.assertNotIn("130 min", result["answer"])
                background = result["retrieval"]["excluded_background_evidence"]
                self.assertEqual(len(background), 1)  # Deduplicate repeated queries.
                self.assertEqual(background[0]["scope"], "background_only")
                self.assertNotIn("text", background[0])
                self.assertIn("仅作背景", result["answer"])
                self.assertEqual(result["result_fingerprint"], fingerprint_result(result))

    def test_reviewed_source_scope_survives_a_neutral_section_title(self):
        item = public_item("EXC_section", "固化不足导致层间分层。", title="5. Conclusions")
        item["provenance"] = {"sha256": REVIEWED_HASH}
        item["independently_verified"] = True
        item.update(source_tier="A", tier_weight=3)
        result = diagnose({"question": "压合后分层"}, retriever=FakeRetriever((("固化", [item]),)))
        self.assertEqual(result["citations"], [])
        excluded = result["retrieval"]["excluded_background_evidence"][0]
        self.assertEqual(excluded["basis"], "reviewed_source_sha256")
        self.assertEqual(item["source_tier"], "A")  # Scope is separate from provenance grade.

    def test_catalog_source_hash_is_not_confused_with_excerpt_hash(self):
        item = normalize_evidence({"results": [{
            "doc_id": "DOC_section", "source_id": "study:4", "type": "KNOWLEDGE",
            "source_type": "public_reference", "title": "4. Results",
            "public_source": {"sha256": REVIEWED_HASH, "publisher": "Synthetic journal", "kind": "open_access_article"},
            "excerpts": [{"excerpt_id": "EXC_section", "text": "固化不足导致层间分层。", "sha256": "0" * 64}],
        }]})[0]
        self.assertEqual(item["sha256"], "0" * 64)
        self.assertEqual(item["provenance"]["sha256"], REVIEWED_HASH)
        result = diagnose({"question": "压合后分层"}, retriever=FakeRetriever((("固化", [item]),)))
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["retrieval"]["excluded_background_evidence"][0]["basis"], "reviewed_source_sha256")

    def test_explicit_quoted_repair_context_applies_without_descriptive_title(self):
        for material in ("CFRP patch repair", "carbon fibre-reinforced polymer repair", "碳纖維修復"):
            with self.subTest(material=material):
                item = public_item("EXC_body", material + "：固化不足导致分层。", title="Curing study")
                result = diagnose({"question": "压合后分层"}, retriever=FakeRetriever((("固化", [item]),)))
                self.assertEqual(result["citations"], [])
                self.assertEqual(result["retrieval"]["excluded_background_evidence"][0]["basis"], "quoted_study_context")

    def test_glass_epoxy_pcb_sources_remain_usable_with_background_present(self):
        pcb = public_item("EXC_pcb", "PCB 玻纤环氧 PP 受潮造成白斑。", title="Glass-fibre epoxy PCB lamination guide")
        # Carbon-related wording alone does not establish a repair-study scope.
        pcb["text"] += " Carbon fillers were also considered."
        background = public_item("EXC_cfrp", "PP 受潮造成白斑。")
        base = diagnose({"question": "压合后白斑"}, retriever=FakeRetriever((("受潮", [pcb]),)))
        mixed = diagnose({"question": "压合后白斑"}, retriever=FakeRetriever((("受潮", [background, pcb]),)))
        self.assertEqual(base["citations"], mixed["citations"])
        self.assertEqual(base["hypotheses"], mixed["hypotheses"])
        self.assertEqual(mixed["citations"][0]["citation_id"], "EXC_pcb")
        self.assertNotIn("excluded_background_evidence", base["retrieval"])

    def test_background_local_results_do_not_suppress_opted_in_external_fallback(self):
        calls = []

        def search(query, limit):
            calls.append(query)
            return [{"title": "PCB moisture guidance", "snippet": "Prepreg moisture causes white spots during PCB lamination.",
                     "url": "https://example.test/pcb"}]

        background = public_item("EXC_local", "PP 受潮造成白斑。")
        result = diagnose({"question": "压合后白斑，批次 PRIVATE-42"},
                          retriever=FakeRetriever((("受潮", [background]),)),
                          external_search=ExternalSearchAdapter(search), allow_external_search=True)
        self.assertTrue(any("受潮" in query for query in calls))
        self.assertTrue(result["citations"])
        self.assertNotIn("EXC_local", [c["citation_id"] for c in result["citations"]])
        self.assertTrue(all("PRIVATE-42" not in query for query in calls))

    def test_external_cfrp_research_cannot_return_as_direct_evidence(self):
        def search(query, limit):
            return [{"title": "CFRP patch repair study", "snippet": "Prepreg moisture causes white spots.",
                     "url": "https://example.test/cfrp"}]

        result = diagnose({"question": "压合后白斑"}, retriever=FakeRetriever(),
                          external_search=ExternalSearchAdapter(search), allow_external_search=True)
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertTrue(result["retrieval"]["external_queries_sent"])
        self.assertEqual(len(result["retrieval"]["excluded_background_evidence"]), 1)

    def test_public_pack_preserves_reference_retrieval_but_restricts_diagnosis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives(root)
            for version in ("v1", "v2"):
                path = root / f"data/public_lamination_{version}/manifest.json"
                manifest = json.loads(path.read_text(encoding="utf-8"))
                next(row for row in manifest if row["id"] == "vendor")["title"] = "CFRP patch repair study"
                path.write_text(json.dumps(manifest), encoding="utf-8")
            output = root / "pack.json"
            build_pack(root=root, output=output)
            original = output.read_bytes()
            retriever = PublicPackRetriever(output)
            hits = retriever.search("空洞 真空 vacuum pressure", limit=20)
            self.assertTrue(any("CFRP" in hit["title"] for hit in hits))
            result = diagnose({"question": "压合后空洞"}, retriever=retriever, search_limit=20)
            self.assertTrue(result["citations"])
            self.assertFalse(any("CFRP" in c["title"] for c in result["citations"]))
            self.assertTrue(result["retrieval"]["excluded_background_evidence"])
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
