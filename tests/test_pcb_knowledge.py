"""Checks thread-safe tokenizer initialization without loading jieba dictionaries."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import pcb_knowledge


class FakeTokenizer:
    def __init__(self):
        self.words = []

    def add_word(self, word, freq):
        self.words.append((word, freq))


class TokenizerInitializationTests(unittest.TestCase):
    def test_concurrent_initialization_creates_one_shared_tokenizer(self):
        worker_count = 12
        barrier = threading.Barrier(worker_count)
        created = []
        created_lock = threading.Lock()
        results = [None] * worker_count
        errors = []

        def create_tokenizer():
            instance = FakeTokenizer()
            with created_lock:
                created.append(instance)
            time.sleep(0.05)
            return instance

        def initialize(index):
            try:
                barrier.wait(timeout=5)
                results[index] = pcb_knowledge.tokenizer()
            except BaseException as exc:  # Keep thread failures visible to unittest.
                errors.append(exc)

        with patch.object(pcb_knowledge, "_TOKENIZER", None), \
                patch.object(pcb_knowledge.jieba, "setLogLevel"), \
                patch.object(pcb_knowledge.jieba, "Tokenizer", side_effect=create_tokenizer):
            threads = [threading.Thread(target=initialize, args=(index,))
                       for index in range(worker_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(created), 1)
        self.assertTrue(all(result is created[0] for result in results))
        expected_words = sum(len(terms) for terms in pcb_knowledge.SYNONYMS.values())
        self.assertEqual(len(created[0].words), expected_words)


class FieldHitTests(unittest.TestCase):
    def test_field_hits_use_the_shared_patterns_and_ignore_case(self):
        hits = pcb_knowledge.field_hits([
            "压合后出现白斑，白斑位于板边。",
            "本批温度 170 ℃，压力 2.5 MPa",
        ])

        self.assertIn("phenomenon", hits)
        self.assertIn("conditions", hits)
        self.assertNotIn("materials", hits)
        self.assertEqual(hits, tuple(key for key in pcb_knowledge.FIELD_PATTERNS
                                     if key in hits))
        self.assertIn("materials", pcb_knowledge.field_hits(["叠构 8 层，PP 规格 1080"]))
        self.assertEqual(pcb_knowledge.field_hits(["与字段无关的一句话"]), ())
        self.assertEqual(pcb_knowledge.field_hits([]), ())


class RetrievalTermTests(unittest.TestCase):
    def test_equipment_questions_keep_process_context_without_question_wrappers(self):
        for question in ("真空在压合中的作用是什么？", "压力在层压中的作用是什么？"):
            with self.subTest(question=question):
                terms = [term for group in pcb_knowledge.term_groups(question) for term in group]
                self.assertIn("压合", terms)
                self.assertNotIn("作用", terms)
                self.assertIn("抽真空" if "真空" in question else "压合压力", terms)
        terms = [term for group in pcb_knowledge.term_groups("压合后白斑怎么排查真空？") for term in group]
        self.assertNotIn("压合", terms)
        self.assertEqual(pcb_knowledge.retrieval_query("真空在压合中的作用是什么？"), "真空")

    def test_question_wrappers_do_not_drive_retrieval(self):
        self.assertEqual(
            pcb_knowledge.retrieval_query(
                "多层板压合后出现分层，优先核对哪些证据？"),
            "多层板 分层")
        self.assertEqual(
            pcb_knowledge.retrieval_query(
                "压合时真空度不足可能造成什么问题，应该补充哪些信息？"),
            "真空度")
        self.assertEqual(
            pcb_knowledge.retrieval_query(
                "压合后出现固化异常，如何区分材料、温度和时间因素？"),
            "固化 材料 温度 时间")

    def test_question_wrappers_are_excluded_from_reranking_groups(self):
        groups = pcb_knowledge.term_groups(
            "多层板压合后出现分层，优先核对哪些证据？")
        used_terms = [term for group in groups for term in group]
        for wrapper in ("优先", "核对", "证据"):
            self.assertNotIn(wrapper, used_terms)
        self.assertIn("分层", used_terms)


class FieldCoverageTests(unittest.TestCase):
    def test_field_coverage_is_shared_and_lists_the_missing_labels(self):
        coverage = pcb_knowledge.field_coverage([
            "层间分层 0.5 MPa\t170 ℃", "试验记录",
        ])

        self.assertEqual(coverage["basis"], "field_patterns_in_returned_excerpts")
        for key in ("phenomenon", "conditions", "verification"):
            self.assertIn(key, coverage["covered"])
        self.assertEqual(coverage["labels"],
                         [pcb_knowledge.FIELD_LABELS[key]
                          for key in coverage["covered"]])
        self.assertEqual(
            set(coverage["labels"]) | set(coverage["missing_labels"]),
            set(pcb_knowledge.FIELD_LABELS.values()))

        empty = pcb_knowledge.field_coverage([])
        self.assertEqual(empty["covered"], [])
        self.assertEqual(empty["labels"], [])
        self.assertEqual(len(empty["missing_labels"]),
                         len(pcb_knowledge.FIELD_LABELS))

    def test_recommended_inputs_cover_exactly_the_uncovered_fields(self):
        coverage = pcb_knowledge.field_coverage([
            "层间分层 0.5 MPa\t170 ℃", "试验记录",
        ])

        inputs = coverage["recommended_inputs"]
        self.assertEqual(
            [item["field"] for item in inputs],
            [key for key in pcb_knowledge.FIELD_LABELS
             if key not in coverage["covered"]])
        self.assertEqual([item["label"] for item in inputs],
                         coverage["missing_labels"])
        for item in inputs:
            self.assertEqual(item["hint"],
                             pcb_knowledge.FIELD_INPUT_HINTS[item["field"]])
            self.assertTrue(item["hint"])

        empty = pcb_knowledge.field_coverage([])
        self.assertEqual(len(empty["recommended_inputs"]),
                         len(pcb_knowledge.FIELD_LABELS))
        self.assertEqual([item["label"] for item in empty["recommended_inputs"]],
                         empty["missing_labels"])


if __name__ == "__main__":
    unittest.main()
