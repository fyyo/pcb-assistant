"""Deterministic checks for the lamination diagnosis agent MVP."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from pcb_assistant.catalog.common import CatalogError
from agent.cli import main
from agent.gate import extract_facts
from agent.models import fingerprint_result
from agent.retriever import normalize_evidence, source_tier
from agent.models import CaseInput
from agent.taxonomy import cause_prior, detect_defects
from agent.providers import ExternalSearchAdapter
from agent.workflow import diagnose


def evidence_item(citation_id, text, *, tier="B", title="合成证据", doc_id=None,
                  source_id=None):
    weights = {"A": 3, "B": 2, "C": 1}
    return {
        "citation_id": citation_id,
        "doc_id": doc_id or "DOC_" + citation_id,
        "source_id": source_id or "SRC_" + citation_id,
        "title": title,
        "source_type": "case",
        "source_version": "synthetic",
        "doc_type": "CASE",
        "evidence_status": "unverified_email_discussion",
        "independently_verified": False,
        "source_tier": tier,
        "tier_weight": weights[tier],
        "text": text,
        "start": 0,
        "end": len(text),
        "sha256": "0" * 64,
        "matched_terms": [],
        "retrieval_score": 1.0,
    }


class FakeRetriever:
    def __init__(self, rules=()):
        self.rules = list(rules)
        self.queries = []

    def search(self, query, *, limit=6):
        self.queries.append(query)
        results = []
        for needle, items in self.rules:
            if needle in query:
                results.extend(items)
        deduped = {}
        for item in results:
            deduped.setdefault(item["citation_id"], item)
        return list(deduped.values())[:limit]


class LaminationAgentTests(unittest.TestCase):
    def test_housekeeping_terms_cannot_support_board_contamination(self):
        for text in (
            "现场不可以有掉漆，不可以有锈，不可以有残胶。",
            "现场只可以使用塑胶栈板，栈板不可损坏，不可污染。",
        ):
            with self.subTest(text=text):
                fake = FakeRetriever((("污染", [evidence_item(
                    "EXC_housekeeping", text, title="铜面污染导致白斑和空洞")]),))
                for question in ("压合后出现白斑。", "压合后出现空洞。"):
                    result = diagnose({"question": question}, retriever=fake)
                    self.assertEqual(result["citations"], [])
                    self.assertEqual(result["status"], "insufficient_evidence")
        result = diagnose({"question": "压合后出现白斑。"}, retriever=FakeRetriever((
            ("污染", [evidence_item("EXC_board", "铜面残胶和污染造成层间结合不良。")]),
        )))
        self.assertIn("EXC_board", [c["citation_id"] for c in result["citations"]])

    def test_ocr_citations_remain_low_tier_traceable_and_explicitly_unverified(self):
        item = normalize_evidence({"results": [{
            "doc_id": "DOC_ocr", "source_id": "SRC_ocr", "type": "KNOWLEDGE",
            "source_type": "attachment", "title": "PDF 附件/第5页",
            "evidence_status": "untrusted_attachment_ocr",
            "citation": {"locator": {"page": 5}},
            "excerpts": [{"excerpt_id": "EXC_ocr", "text": "真空20 kPa时排气不足造成空洞。"}],
        }]})[0]
        self.assertEqual(item["source_tier"], "C")
        result = diagnose({"question": "压合后出现空洞。"}, retriever=FakeRetriever((("真空", [item]),)))
        citation = next(c for c in result["citations"] if c["citation_id"] == "EXC_ocr")
        self.assertFalse(citation["ocr_values_independently_verified"])
        self.assertEqual(citation["locator"], {"page": 5})
        self.assertIn("20 kPa", citation["text"])
        self.assertIn("OCR 未核实", result["answer"])
        self.assertIn("不可直接作为工艺设定", result["answer"])
        self.assertEqual(result["result_fingerprint"], fingerprint_result(result))

    def test_title_and_search_metadata_cannot_supply_missing_cause_support(self):
        for title, terms in (("PP 受潮造成白斑", []), ("合成证据", ["受潮", "白斑"])):
            with self.subTest(title=title, matched_terms=terms):
                item = evidence_item("EXC_metadata", "本批样品已送检。", title=title)
                item["matched_terms"] = terms
                result = diagnose(
                    {"question": "压合后出现白斑。"},
                    retriever=FakeRetriever((("受潮", [item]),)),
                )
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["citations"], [])
                self.assertTrue(all(h["evidence_score"] == 0 for h in result["hypotheses"]))

    def test_metadata_cannot_inflate_body_evidence_score(self):
        def score(title, terms):
            item = evidence_item("EXC_body", "PP 受潮造成异常。", title=title)
            item["matched_terms"] = terms
            result = diagnose(
                {"question": "压合后出现白斑。"},
                retriever=FakeRetriever((("受潮", [item]),)),
            )
            return next(h["evidence_score"] for h in result["hypotheses"]
                        if h["cause_id"] == "prepreg_moisture")

        self.assertEqual(
            score("合成证据", []),
            score("PP 受潮、水分、挥发导致白斑", ["水分", "挥发", "白斑"]),
        )

    def test_text_beyond_returned_quote_cannot_supply_cause_support(self):
        item = evidence_item("EXC_long", "本批样品已送检。" * 100 + "PP 受潮导致白斑。")
        result = diagnose(
            {"question": "压合后出现白斑。"},
            retriever=FakeRetriever((("受潮", [item]),)),
        )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["citations"], [])

    def test_title_only_local_match_does_not_suppress_external_fallback(self):
        calls = []

        def search(query, limit):
            calls.append(query)
            return [{"title": "Moisture guidance",
                     "snippet": "Prepreg moisture can cause white spots during lamination.",
                     "url": "https://example.test/moisture"}]

        result = diagnose(
            {"question": "压合后出现白斑。"},
            retriever=FakeRetriever((("受潮", [evidence_item(
                "EXC_title_only", "本批样品已送检。", title="PP 受潮造成白斑",
            )]),)),
            external_search=ExternalSearchAdapter(search),
            allow_external_search=True,
        )
        self.assertTrue(any("受潮" in query for query in calls))
        moisture = next(h for h in result["hypotheses"] if h["cause_id"] == "prepreg_moisture")
        self.assertTrue(moisture["supporting_evidence"])
        self.assertNotIn("EXC_title_only", [c["citation_id"] for c in result["citations"]])

    def test_evidence_score_rewards_only_current_defect_and_counts_aliases_once(self):
        def moisture_score(text):
            result = diagnose(
                {"question": "压合后出现白斑。"},
                retriever=FakeRetriever((("受潮", [evidence_item("EXC_m", text)]),)),
            )
            return next(
                item["evidence_score"] for item in result["hypotheses"]
                if item["cause_id"] == "prepreg_moisture"
            )

        base = moisture_score("PP 受潮造成异常。")
        unrelated = moisture_score("PP 受潮造成分层、起泡、空洞、缺胶和翘曲。")
        current = moisture_score("PP 受潮造成白斑。")
        aliases = moisture_score("PP 受潮造成白斑、白点和白點。")
        traditional = moisture_score("PP 受潮造成白點。")
        self.assertEqual(base, unrelated)
        self.assertGreater(current, unrelated)
        self.assertEqual(current, aliases)
        self.assertEqual(current, traditional)

    def test_external_search_is_opt_in_and_case_fields_do_not_leave(self):
        calls = []

        def search(query, limit):
            calls.append((query, limit))
            return [{
                "title": "Vacuum guidance",
                "snippet": "Vacuum pressure loss can cause voids during PCB lamination.",
                "url": "https://example.test/vacuum",
            }]

        provider = ExternalSearchAdapter(search, provider_name="test_web")
        local = diagnose({
            "question": "压合后 BGA 区出现空洞，批次 SECRET-42。",
            "batch": "SECRET-42",
        }, retriever=FakeRetriever())
        self.assertFalse(calls)
        self.assertFalse(local["retrieval"]["external_search_enabled"])

        result = diagnose(
            {
                "question": "压合后 BGA 区出现空洞，批次 SECRET-42。",
                "batch": "SECRET-42",
            },
            retriever=FakeRetriever(),
            external_search=provider,
            allow_external_search=True,
        )
        self.assertTrue(calls)
        self.assertTrue(all("SECRET-42" not in query for query, _ in calls))
        self.assertTrue(result["retrieval"]["external_search_enabled"])
        self.assertEqual(result["boundaries"]["data_egress"], "defect_and_cause_terms_only")
        self.assertTrue(any(item["citation_id"].startswith("EXC_WEB_") for item in result["citations"]))

    def test_external_search_requires_provider_when_enabled(self):
        with self.assertRaisesRegex(CatalogError, "external_search_provider_required"):
            diagnose(
                {"question": "压合后出现空洞。"},
                retriever=FakeRetriever(),
                allow_external_search=True,
            )
    def test_out_of_domain_is_refused_without_hypotheses(self):
        result = diagnose({"question": "明天上海天气怎么样？"})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["gate"]["status"], "refused")
        self.assertEqual(result["hypotheses"], [])
        self.assertIn("不属于", result["answer"])

    def test_lamination_signal_without_defect_asks_for_clarification(self):
        result = diagnose({"question": "压合后出现异常，应该怎么排查？"})
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIsNone(result["defect"])
        self.assertIn("缺陷类型", result["answer"])

    def test_white_spot_ranks_evidence_backed_causes(self):
        fake = FakeRetriever((
            ("真空", [evidence_item(
                "EXC_vacuum", "真空度不足会造成气泡、填胶不良和层间白斑。"
            )]),
            ("受潮", [evidence_item(
                "EXC_moisture", "PP 受潮或挥发物超标会造成白斑和分层。", tier="C"
            )]),
        ))
        result = diagnose(
            {
                "question": "压合后板边出现白斑，PP 是 FR-4，六层板，使用真空压机。",
                "defect": "white_spot",
            },
            retriever=fake,
        )
        self.assertEqual(result["status"], "diagnosed")
        self.assertEqual(result["defect"], "white_spot")
        self.assertEqual(result["hypotheses"][0]["rank"], 1)
        top_ids = {item["cause_id"] for item in result["hypotheses"][:2]}
        self.assertTrue(top_ids & {"vacuum_pressure_loss", "prepreg_moisture"})
        self.assertTrue(result["hypotheses"][0]["supporting_evidence"])
        self.assertTrue(result["citations"])
        self.assertTrue(result["boundaries"]["human_confirmation_required"])
        self.assertFalse(result["boundaries"]["engineering_conclusion_supported"])
        self.assertTrue(fake.queries)

    def test_missing_material_and_stackup_are_reported(self):
        result = diagnose(
            {"question": "压合后板边出现白斑，怎么排查？", "defect": "white_spot"},
            retriever=FakeRetriever(),
        )
        codes = {item["code"] for item in result["missing_information"]}
        self.assertIn("material", codes)
        self.assertIn("stackup", codes)
        self.assertTrue(result["next_questions"])

    def test_no_evidence_returns_insufficient_without_inventing_citations(self):
        result = diagnose(
            {"question": "压合后出现白斑，材料未知。", "defect": "white_spot"},
            retriever=FakeRetriever(),
        )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertTrue(result["hypotheses"])
        self.assertTrue(all(item["evidence_strength"] == "none" for item in result["hypotheses"]))
        self.assertEqual(result["citations"], [])
        self.assertIn("没有找到可直接引用的证据", result["answer"])

    def test_chinese_defect_alias_is_resolved(self):
        result = diagnose(
            {"question": "板边有白斑，想确认原因。", "defect": "白斑"},
            retriever=FakeRetriever(),
        )
        self.assertEqual(result["defect"], "white_spot")

    def test_ascii_aliases_do_not_match_inside_other_words(self):
        self.assertEqual(detect_defects("avoid this wording"), [])
        self.assertEqual(detect_defects("a void in the laminate"), ["void"])
        facts = extract_facts(CaseInput.from_mapping({
            "question": "PP受潮后压合出现白斑",
        }))
        self.assertEqual(facts["material"], "PP")

    def test_traditional_defect_aliases_are_resolved(self):
        self.assertEqual(detect_defects("板邊白點"), ["white_spot"])
        self.assertEqual(detect_defects("壓合後缺膠"), ["resin_shortage"])
        self.assertEqual(detect_defects("板子翹曲"), ["warpage"])
        self.assertEqual(detect_defects("層間氣泡"), ["void"])

    def test_delamination_context_beats_broad_burst_symptom(self):
        result = diagnose(
            {"question": "压合后出现爆板分层，板边有铜皮分离。"},
            retriever=FakeRetriever(),
        )
        self.assertEqual(result["defect"], "delamination")
        self.assertEqual(detect_defects("爆板分層、板邊分層" )[0], "delamination")

    def test_blister_context_keeps_standalone_burst_symptom(self):
        result = diagnose(
            {"question": "压合后大孔爆板异常。"},
            retriever=FakeRetriever(),
        )
        self.assertEqual(result["defect"], "blister")
        self.assertEqual(detect_defects("已爆板分層")[0], "blister")

    def test_native_pdf_attachment_is_tier_b(self):
        self.assertEqual(source_tier({
            "type": "KNOWLEDGE",
            "source_type": "attachment",
            "title": "PDF 附件/第5页",
        }), "B")

    def test_defect_specific_prior_keeps_void_focused_on_vacuum(self):
        self.assertGreater(
            cause_prior("void", "vacuum_pressure_loss"),
            cause_prior("void", "prepreg_moisture"),
        )
        fake = FakeRetriever((
            ("真空", [evidence_item(
                "EXC_vac", "真空不足会导致空洞和填胶不良，原因是排气不充分。"
            )]),
            ("受潮", [
                evidence_item("EXC_m1", "材料受潮会产生挥发物并导致气泡。"),
                evidence_item("EXC_m2", "PP 受潮会造成层间空洞。", tier="B"),
            ]),
        ))
        result = diagnose(
            {"question": "压合后 BGA 区出现空洞。", "defect": "void"},
            retriever=fake,
        )
        self.assertEqual(result["hypotheses"][0]["cause_id"], "vacuum_pressure_loss")

    def test_evidence_is_attached_only_to_matching_causes(self):
        fake = FakeRetriever((
            ("真", [evidence_item(
                "EXC_vac", "真空度不足会造成气泡和填胶不良。"
            )]),
            ("受潮", [evidence_item(
                "EXC_moist", "PP 受潮会产生挥发物并导致气泡。"
            )]),
            ("污染", [evidence_item(
                "EXC_copper", "铜面氧化会造成界面污染。"
            )]),
        ))
        result = diagnose(
            {"question": "压合后 BGA 区出现空洞。", "defect": "void"},
            retriever=fake,
        )
        by_cause = {item["cause_id"]: item for item in result["hypotheses"]}
        vacuum_ids = {
            item["citation_id"]
            for item in by_cause["vacuum_pressure_loss"]["supporting_evidence"]
        }
        moisture_ids = {
            item["citation_id"]
            for item in by_cause["prepreg_moisture"]["supporting_evidence"]
        }
        contamination_ids = {
            item["citation_id"]
            for item in by_cause["contamination"]["supporting_evidence"]
        }
        self.assertIn("EXC_vac", vacuum_ids)
        self.assertNotIn("EXC_vac", moisture_ids)
        self.assertIn("EXC_moist", moisture_ids)
        self.assertNotIn("EXC_copper", vacuum_ids)
        self.assertIn("EXC_copper", contamination_ids)

    def test_fingerprint_is_stable_and_covers_content(self):
        first = diagnose(
            {"question": "板边有白斑", "defect": "白斑"},
            retriever=FakeRetriever(),
        )
        second = diagnose(
            {"question": "板边有白斑", "defect": "白斑"},
            retriever=FakeRetriever(),
        )
        self.assertEqual(first["result_fingerprint"], second["result_fingerprint"])
        self.assertEqual(first["result_fingerprint"], fingerprint_result(first))
        changed = diagnose(
            {"question": "板中有白斑", "defect": "白斑"},
            retriever=FakeRetriever(),
        )
        self.assertNotEqual(first["result_fingerprint"], changed["result_fingerprint"])

    def test_normalize_evidence_flattens_excerpts_and_assigns_tiers(self):
        result = normalize_evidence({
            "results": [{
                "doc_id": "DOC_1",
                "source_id": "SRC_1",
                "type": "PUBLIC",
                "source_type": "public_reference",
                "title": "IPC-A-600 标准摘要",
                "source_version": "public",
                "evidence_status": "public_reference",
                "independently_verified": False,
                "retrieval_score": 2.5,
                "excerpts": [{
                    "excerpt_id": "EXC_1",
                    "text": "白斑通常与树脂流动和界面结合有关。",
                    "start": 0,
                    "end": 20,
                    "sha256": "a" * 64,
                    "matched_terms": ["白斑"],
                }],
            }],
        })
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["citation_id"], "EXC_1")
        # A title mentioning IPC is insufficient provenance for tier A.
        self.assertEqual(result[0]["source_tier"], "B")
        self.assertEqual(result[0]["tier_weight"], 2)

    def test_cli_json_mode_returns_the_result(self):
        payload = {
            "schema": "lamination-agent-v1",
            "status": "diagnosed",
            "answer": "候选方向",
        }
        buffer = io.StringIO()
        with patch("agent.cli.diagnose", return_value=payload):
            with redirect_stdout(buffer):
                code = main(["diagnose", "--question", "白斑", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue()), payload)

    def test_expanded_material_grades_and_normal_observations(self):
        for grade in ("IT-180A", "TU-872", "EM-827", "S1000-2M", "7628"):
            with self.subTest(grade=grade):
                facts = extract_facts(CaseInput.from_mapping({
                    "question": f"8层板 {grade} 压合后板边出现白斑。"
                }))
                self.assertEqual(facts["material"].upper(), grade.upper())
        # Must not treat abnormal vacuum followed by normal temperature as normal vacuum.
        mixed = extract_facts(CaseInput.from_mapping({
            "question": "压合后板边白斑，真空不足，温度曲线正常。"
        }))
        causes = {item["cause_id"] for item in mixed["normal_observations"]}
        self.assertNotIn("vacuum_pressure_loss", causes)
        self.assertIn("temperature_profile", causes)

    def test_contradicting_evidence_damps_normal_cause_and_renders(self):
        fake = FakeRetriever((
            ("真空", [evidence_item("EXC_vac", "真空不足会导致压合白斑。")]),
            ("受潮", [evidence_item("EXC_moist", "PP 受潮会导致压合白斑。")]),
        ))
        result = diagnose(
            {"question": "压合后板边出现白斑，真空曲线和保压正常，怎么排查？"},
            retriever=fake,
        )
        by_cause = {h["cause_id"]: h for h in result["hypotheses"]}
        vac = by_cause["vacuum_pressure_loss"]
        moist = by_cause["prepreg_moisture"]
        self.assertTrue(vac["contradicting_evidence"])
        self.assertEqual(vac["contradicting_evidence"][0]["basis"], "user_reported_normal_condition")
        self.assertLess(vac["priority_score"], moist["priority_score"])
        self.assertGreater(vac["rank"], moist["rank"])
        self.assertIn("已记录的正常观测", result["answer"])
        self.assertEqual(result["result_fingerprint"], fingerprint_result(result))

    def test_related_attachment_excerpts_traces_mail_relationships(self):
        from agent.retriever import CatalogRetriever, related_attachment_excerpts
        from tests.phase6_test_fixtures import MAIL_ID, SyntheticCatalogTestCase

        class _TempHolder(SyntheticCatalogTestCase):
            pass

        _TempHolder.setUpClass()
        try:
            traced = related_attachment_excerpts(_TempHolder.catalog, [MAIL_ID], query="白斑")
            self.assertTrue(traced)
            self.assertTrue(all(item["citation_id"].startswith("EXC_") for item in traced))
            retriever = CatalogRetriever(
                _TempHolder.catalog, limit=3, include_related_attachments=True)
            results = retriever.search("白斑")
            self.assertTrue(results)
        finally:
            _TempHolder.doClassCleanups()


if __name__ == "__main__":
    unittest.main()
