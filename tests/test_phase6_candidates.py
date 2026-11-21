'''Phase 6 proposed-candidate queue tests; local retrieval only.'''
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch
import pcb_assistant.catalog.agent as catalog_agent
import pcb_assistant.evaluation.candidates as phase6_candidates
from tests.phase6_test_fixtures import SyntheticCatalogTestCase

from pcb_assistant.evaluation.candidates import (
    build_candidate,
    load_candidates,
    validate_candidates,
    write_candidates,
)
from pcb_assistant.interfaces.contract import load_cases


def _has_key(value, key):
    if isinstance(value, dict):
        return any(item_key == key or _has_key(item, key)
                   for item_key, item in value.items())
    if isinstance(value, list):
        return any(_has_key(item, key) for item in value)
    return False


class Phase6CandidateTests(SyntheticCatalogTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        patcher = patch.object(phase6_candidates, 'catalog_agent', SimpleNamespace(
            ask_agent=partial(catalog_agent.ask_agent, directory=cls.catalog)))
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        cls.cases = load_cases()
        cls.normal_case = cls.cases[0]
        cls.injection_case = next(
            item for item in cls.cases if item['category'] == 'prompt_injection')

    def test_candidate_is_proposed_not_gold(self):
        candidate = build_candidate(self.normal_case, limit=2)
        self.assertEqual(candidate['annotation_status'], 'proposed')
        self.assertFalse(_has_key(candidate, 'relevant_doc_ids'))
        self.assertFalse(_has_key(candidate, 'required_evidence_ids'))
        self.assertFalse(_has_key(candidate, 'source_path'))
        self.assertIn('expert_confirmation_required', candidate['quality_flags'])
        self.assertEqual(candidate['case_id'], self.normal_case['case_id'])

    def test_candidate_excerpts_are_bounded(self):
        candidate = build_candidate(self.normal_case, limit=2)
        self.assertTrue(candidate['candidate_excerpt_ids'])
        for excerpt in candidate['candidate_excerpts']:
            self.assertLessEqual(len(excerpt['text']), 360)

    def test_prompt_injection_is_not_retrieved(self):
        candidate = build_candidate(self.injection_case)
        self.assertEqual(candidate['retrieval_status'], 'not_retrieved')
        self.assertEqual(candidate['candidate_doc_ids'], [])
        self.assertEqual(candidate['candidate_excerpt_ids'], [])
        self.assertEqual(candidate['candidate_quality'], 'not_applicable')
        self.assertIn('policy_blocked_not_retrieved', candidate['quality_flags'])

    def test_validator_accepts_proposed_queue_and_rejects_gold_mutation(self):
        candidates = [
            build_candidate(self.normal_case, limit=1),
            build_candidate(self.injection_case),
        ]
        cases = [self.normal_case, self.injection_case]
        self.assertEqual(validate_candidates(candidates, cases), [])
        candidates[0]['annotation_status'] = 'complete'
        self.assertTrue(validate_candidates(candidates, cases))
        candidates[0]['annotation_status'] = 'proposed'
        candidates[0]['expected'] = {'relevant_doc_ids': ['MAIL_' + '0' * 32]}
        self.assertTrue(validate_candidates(candidates, cases))

    def test_jsonl_round_trip_is_utf8_and_path_free(self):
        candidate = build_candidate(self.injection_case)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidates.jsonl'
            write_candidates([candidate], path)
            loaded = load_candidates(path)
        self.assertEqual(loaded, [candidate])
        self.assertIn(candidate['question'], json.dumps(loaded, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
