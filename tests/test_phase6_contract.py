'''Phase 6.0 contract and evaluation skeleton checks; all local and offline.'''
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

import phase6_contract as contract
import phase6_mapping as mapping
from phase6_eval import build_cases


class Phase6ContractTests(unittest.TestCase):
    def test_all_contract_artifacts_validate(self):
        result = contract.validate_contracts()
        self.assertEqual(result['status'], 'passed', result['issues'])
        self.assertEqual(result['external_calls'], 0)
        self.assertEqual(result['counts']['tools'], 8)
        self.assertEqual(result['counts']['errors'], 17)
        self.assertEqual(result['counts']['eval_cases'], 150)
        self.assertEqual(result['counts']['pending_annotations'], 150)

    def test_sample_envelope_is_valid(self):
        self.assertEqual(
            contract.validate_instance(contract.SAMPLE_ENVELOPE, contract.ENVELOPE_SCHEMA),
            [],
        )

    def test_envelope_rejects_cloud_flag_without_egress(self):
        bad = copy.deepcopy(contract.SAMPLE_ENVELOPE)
        bad['external_services'] = True
        issues = contract.validate_instance(bad, contract.ENVELOPE_SCHEMA)
        self.assertTrue(issues)

    def test_envelope_rejects_egress_without_cloud_flag(self):
        bad = copy.deepcopy(contract.SAMPLE_ENVELOPE)
        bad['data_egress'] = 'sanitized_evidence'
        issues = contract.validate_instance(bad, contract.ENVELOPE_SCHEMA)
        self.assertTrue(issues)

    def test_answer_schema_accepts_cited_answer(self):
        self.assertEqual(
            contract.validate_instance(contract.SAMPLE_ANSWER, contract.ANSWER_SCHEMA),
            [],
        )

    def test_answer_schema_rejects_invalid_citation_id(self):
        bad = copy.deepcopy(contract.SAMPLE_ANSWER)
        bad['possible_causes'][0]['citation_ids'] = ['EXC_not-a-real-id']
        issues = contract.validate_instance(bad, contract.ANSWER_SCHEMA)
        self.assertTrue(issues)

    def test_tools_contract_is_read_only_and_has_expected_surface(self):
        result = contract.validate_tools_contract(contract.TOOLS_CONTRACT)
        self.assertEqual(result, [])
        tools = {item['name']: item for item in contract.TOOLS_CONTRACT['tools']}
        self.assertEqual(tools['pcb_answer']['default_mode'], 'evidence_only')
        self.assertEqual(tools['pcb_answer']['data_egress'], 'conditional')
        self.assertFalse(tools['pcb_search']['external_services'])
        self.assertFalse(tools['pcb_evidence']['side_effects'])
        self.assertIn('query', tools['pcb_evidence']['input_schema']['required'])

    def test_tool_schemas_accept_real_catalog_document_ids(self):
        example_suffix = '079d6df7ac8cc272572281a243b15833'
        tools = {item['name']: item for item in contract.TOOL_SPECS}
        for prefix in ('MAIL', 'CASE', 'KNOWLEDGE', 'ATTACHMENT', 'PUBLIC'):
            with self.subTest(prefix=prefix):
                doc_id = prefix + '_' + example_suffix
                for name in ('pcb_get_document', 'pcb_related', 'pcb_similar_cases'):
                    arguments = {'doc_id': doc_id}
                    self.assertEqual(
                        contract.validate_instance(
                            arguments, tools[name]['input_schema']),
                        [],
                    )

    def test_tool_mapping_is_valid_and_in_sync(self):
        self.assertEqual(mapping.validate_mapping(), [])
        self.assertEqual(mapping.validate_mapping_file(), [])

    def test_tool_mapping_exposes_only_p1_tools(self):
        exposed = [item['name'] for item in mapping.TOOL_MAPPING['tools']
                   if item['phase6_1_exposed']]
        self.assertEqual(len(exposed), 7)
        self.assertNotIn('pcb_answer', exposed)
        self.assertTrue(all(item['side_effects'] is False
                            for item in mapping.TOOL_MAPPING['tools']))
        self.assertTrue(all(item['data_egress'] == 'none'
                            for item in mapping.TOOL_MAPPING['tools']
                            if item['name'] != 'pcb_answer'))

    def test_phase62_mapping_exposes_the_complete_read_only_tool_set(self):
        exposed = [item['name'] for item in mapping.TOOL_MAPPING['tools']
                   if item['phase6_2_exposed']]
        self.assertEqual(len(exposed), 8)
        self.assertIn('pcb_answer', exposed)

    def test_tool_mapping_does_not_carry_paths_or_secrets(self):
        encoded = json.dumps(mapping.TOOL_MAPPING, ensure_ascii=False)
        self.assertEqual(mapping._contains_forbidden_key(mapping.TOOL_MAPPING), False)
        self.assertNotRegex(encoded, r'(?i)[a-z]:\\\\')
        self.assertNotRegex(encoded, r'sk-[A-Za-z0-9]{20,}')

    def test_errors_contract_is_unique_and_complete(self):
        result = contract.validate_errors_contract(contract.ERRORS_CONTRACT)
        self.assertEqual(result, [])
        codes = {item['code'] for item in contract.ERRORS_CONTRACT['errors']}
        self.assertIn('invalid_citation', codes)
        self.assertIn('policy_blocked', codes)
        self.assertIn('provider_timeout', codes)

    def test_eval_cases_are_unique_pending_and_balanced(self):
        cases = build_cases()
        self.assertEqual(contract.validate_eval_cases(cases), [])
        self.assertEqual(len(cases), 150)
        self.assertEqual(len({item['case_id'] for item in cases}), 150)
        self.assertEqual(len({item['question'] for item in cases}), 150)
        self.assertTrue(all(item['annotation']['status'] == 'pending' for item in cases))
        self.assertTrue(all(item['expected']['relevant_doc_ids'] == [] for item in cases))
        self.assertTrue(all(item['expected']['required_evidence_ids'] == [] for item in cases))

    def test_generated_cases_file_matches_generator(self):
        cases = contract.load_cases()
        self.assertEqual(cases, build_cases())

    def test_annotated_case_requires_reviewer_and_gold_ids(self):
        cases = copy.deepcopy(build_cases())
        first = cases[0]
        first['annotation'] = {
            'status': 'complete',
            'reviewer': 'reviewer-a',
            'reviewed_at': '2026-10-26T00:00:00+08:00',
        }
        first['expected']['relevant_doc_ids'] = [
            'MAIL_079d6df7ac8cc272572281a243b15833']
        first['expected']['required_evidence_ids'] = [
            'EXC_2ba4cda4fc3fbc2b133250fc']
        self.assertEqual(
            contract.validate_eval_cases(cases, require_pending=False), [])
        self.assertTrue(contract.validate_eval_cases(cases, require_pending=True))
        first['annotation']['reviewer'] = None
        self.assertTrue(contract.validate_eval_cases(cases, require_pending=False))

    def test_artifacts_do_not_contain_secret_like_values(self):
        paths = [
            contract.ENVELOPE_SCHEMA_PATH,
            contract.ANSWER_SCHEMA_PATH,
            contract.TOOLS_PATH,
            contract.ERRORS_PATH,
            contract.CASE_SCHEMA_PATH,
            contract.CASES_PATH,
        ]
        for path in paths:
            text = Path(path).read_text(encoding='utf-8')
            self.assertNotRegex(text, r'sk-[A-Za-z0-9]{20,}')

    def test_contract_json_is_utf8_and_parseable(self):
        for path in (contract.ENVELOPE_SCHEMA_PATH, contract.ANSWER_SCHEMA_PATH,
                     contract.TOOLS_PATH, contract.ERRORS_PATH, contract.CASE_SCHEMA_PATH):
            with self.subTest(path=path.name):
                value = json.loads(path.read_text(encoding='utf-8'))
                self.assertIsInstance(value, dict)


if __name__ == '__main__':
    unittest.main()
