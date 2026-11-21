"""Synthetic-only identity repair and preservation checks."""
import json
from pathlib import Path
import tempfile
import unittest

from pcb_assistant.privacy.candidates import CandidateIdentityCleaner
from pcb_assistant.catalog.common import digest_file
from scripts.data.rewash_candidate_identities import repair_run, read_rows


class CandidatePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.cleaner = CandidateIdentityCleaner({'陈欣瑶', 'Alice Example', 'resin', '真空',
                                                'FR-4', '身份', 'MPa', 'bar', 'mm', 'µm'})

    def test_known_contacts_and_contextual_given_names(self):
        text = '陈欣瑶确认；Alice Example 提供。\n请欣瑶 回复，真空 20 kPa。'
        out = self.cleaner.clean(text)
        for name in ('陈欣瑶', 'Alice Example', '欣瑶'):
            self.assertNotIn(name, out)
        self.assertIn('真空 20 kPa', out)
        self.assertFalse(self.cleaner.residuals(out))

    def test_unanchored_name_fragments_and_technical_terms_are_not_guessed(self):
        text = '欣瑶树脂样本；resin flow；FR-4；真空；审核：温度\n'
        self.assertEqual(self.cleaner.clean(text), text)

    def test_signature_labels_are_redacted_with_technical_content_preserved(self):
        text = '編制：王小明\t保温 60 min\n审核：真空\n签核：陈小华\n维护课长签核'
        out = self.cleaner.clean(text)
        self.assertNotIn('王小明', out)
        self.assertNotIn('陈小华', out)
        self.assertIn('审核：真空', out)
        self.assertIn('维护课长', out)
        self.assertIn('60 min', out)

    def test_placeholders_numbers_units_codes_and_mixed_line_endings_survive(self):
        text = ('[身份]\r\n[实体]\n温度 180℃\t压力 -0.095 MPa，升温 2.5℃/min；'
                '铜厚 35 μm，18 µm，板厚 1.6 mm，压力 25 bar，FR-4，IPC-4101，Tg170\r\n\r\n')
        self.assertEqual(self.cleaner.clean(text), text)

    def test_contextual_alias_does_not_match_inside_a_longer_word(self):
        text = '合成欣瑶回复，引用中只有名字片段。'
        self.assertEqual(self.cleaner.clean(text), text)

    def test_rules_converge_and_existing_names_are_detected(self):
        text = 'Dear 王五\n苏鹏副理确认 0.5 MPa'
        out = self.cleaner.clean(text)
        self.assertNotIn('王五', out)
        self.assertNotIn('苏鹏', out)
        self.assertEqual(self.cleaner.clean(out), out)
        self.assertIn('0.5 MPa', out)
        self.assertTrue(self.cleaner.residuals('陈欣瑶确认'))

    def test_readback_does_not_accept_existing_emails_or_paths(self):
        self.assertTrue(self.cleaner.residuals('person@example.org'))
        self.assertTrue(self.cleaner.residuals('C:\\private\\x'))

    def test_engineering_audit_distinguishes_words_inside_full_contact_names(self):
        cleaner = CandidateIdentityCleaner({'合成工程供应商'})
        text = '合成工程供应商确认：工程要求温度 180℃，压力 0.5 MPa'
        audit = cleaner.preservation(text,cleaner.clean(text))
        self.assertEqual(audit['technical_phrase_mismatches_outside_contacts'],0)
        self.assertEqual(audit['engineering_expression_mismatches'],0)
        self.assertGreater(audit['technical_tokens_within_contact_redactions'],0)

    def test_engineering_audit_detects_erased_process_terms_and_changed_units(self):
        audit = self.cleaner.preservation('真空 20 kPa，温度 180℃','20 psi，180℃')
        self.assertEqual(audit['technical_phrase_mismatches_outside_contacts'],1)
        self.assertEqual(audit['engineering_expression_mismatches'],1)

    def test_greetings_and_roles_cannot_swallow_engineering_prose_or_numbers(self):
        cleaner = CandidateIdentityCleaner(set())
        text = ('Hi resin flow 180℃\nDear 王五，压力 0.5 MPa，板厚 1.6 mm\n'
                '主管要求改善；編制：王主管\n主任工程说明；高温主管确认\n'
                'MPA 王五\nHi Alice Example 180℃\n维护课长签核')
        result = cleaner.clean(text)
        self.assertIn('Dear [身份]，压力 0.5 MPa，板厚 1.6 mm',result)
        self.assertIn('Hi [身份] 180℃',result)
        for phrase in ('Hi resin flow 180℃','主管要求改善','編制：[身份]主管',
                       '主任工程说明','高温主管确认','MPA','维护课长签核'):
            self.assertIn(phrase,result)
        self.assertFalse(any(v for k,v in cleaner.preservation(text,result).items() if 'mismatches' in k))

    def test_contacts_cannot_erase_code_prefixes_and_unit_symbols(self):
        cleaner = CandidateIdentityCleaner({'FR','IPC','Tg','ABC','Torr','Alice Example'})
        text = 'FR-4 IPC-4101 Tg170 ABC123 50 Torr；Alice Example 确认'
        result = cleaner.clean(text)
        self.assertEqual(result,'FR-4 IPC-4101 Tg170 ABC123 50 Torr；[身份] 确认')
        self.assertFalse(cleaner.residuals(result))
        self.assertFalse(any(v for k,v in cleaner.preservation(text,result).items() if 'mismatches' in k))

    def test_codes_next_to_chinese_text_have_stable_boundaries(self):
        cleaner=CandidateIdentityCleaner({'ABC','陈欣瑶'})
        text='陈欣瑶ABC123板厚1.6mm；陈欣瑶XYZ456材料'
        result=cleaner.clean(text)
        self.assertEqual(result,'[身份]ABC123板厚1.6mm；[身份]XYZ456材料')
        self.assertFalse(cleaner.residuals(result))

    def test_contact_crossing_code_boundary_redacts_only_the_identity_part(self):
        cleaner=CandidateIdentityCleaner({'陈欣瑶AB','王小明 ABC'})
        text='陈欣瑶ABC123材料；王小明 ABC456板厚1.6mm'
        result=cleaner.clean(text)
        self.assertEqual(result,'[身份]ABC123材料；[身份]ABC456板厚1.6mm')
        self.assertFalse(cleaner.residuals(result))

class RepairRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'repaired'
        self.cleaner = CandidateIdentityCleaner({'陈欣瑶'})

    def write(self, kind='mail', text='陈欣瑶确认 温度 180℃，压力 0.5 MPa'):
        row = {'id': 'mail_a' if kind == 'mail' else 'unit_a', 'subject': '合成记录', 'text': text,
               'topics': ['压合'], 'training_allowed': True}
        if kind == 'mail':
            row.update({'review_required': True})
            files = {'candidates.jsonl': [row], 'engineering_candidates.jsonl': [row],
                     'message_status.jsonl': [{'id': 'mail_a', 'status': 'retained'}]}
        else:
            row.update({'document_id': 'att_a', 'parent_source_ids': ['mail_a'],
                        'manual_review_required': False, 'source_coverage': 'limited'})
            files = {'documents.jsonl': [{'document_id': 'att_a', 'exported_units': 1}],
                     'occurrences.jsonl': [{'document_id': 'att_a', 'parent_source_id': 'mail_a'}],
                     'engineering_candidates.jsonl': [row]}
        for name, rows in files.items():
            (self.source/name).write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows), encoding='utf-8')
        report = {'artifacts': {name: {'sha256': digest_file(self.source/name)} for name in files}}
        (self.source/'report.json').write_text(json.dumps(report), encoding='utf-8')

    def test_mail_ids_counts_status_and_input_bytes_are_preserved(self):
        self.write()
        before = {p.name: digest_file(p) for p in self.source.iterdir()}
        result = repair_run(self.source, self.output, self.cleaner, kind='mail')
        self.assertEqual(before, {p.name: digest_file(p) for p in self.source.iterdir()})
        rows = list(read_rows(self.output/'candidates.jsonl'))
        self.assertEqual(rows[0]['id'], 'mail_a')
        self.assertNotIn('陈欣瑶', rows[0]['text'])
        self.assertIn('0.5 MPa', rows[0]['text'])
        self.assertFalse(rows[0]['training_allowed'])
        self.assertEqual(result['statistics']['records_read_back'], 2)
        self.assertEqual(digest_file(self.source/'message_status.jsonl'), digest_file(self.output/'message_status.jsonl'))
        self.assertFalse((self.output/'REPAIR_FAILED.json').exists())

    def test_attachment_manifest_and_occurrences_are_unchanged_and_release_restricted(self):
        self.write('attachment')
        repair_run(self.source, self.output, self.cleaner, kind='attachment')
        for name in ('documents.jsonl', 'occurrences.jsonl'):
            self.assertEqual(digest_file(self.source/name), digest_file(self.output/name))
        row = next(read_rows(self.output/'engineering_candidates.jsonl'))
        self.assertEqual(row['document_id'], 'att_a')
        self.assertEqual(row['parent_source_ids'], ['mail_a'])
        self.assertEqual(row['source_coverage'], 'limited')
        self.assertFalse(row['training_allowed'])
        self.assertTrue(row['manual_review_required'])

    def test_rejects_existing_output_and_changed_input_hash(self):
        self.write()
        (self.source/'candidates.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source_artifact_mismatch'):
            repair_run(self.source, self.output, self.cleaner, kind='mail')
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'output_already_exists'):
            repair_run(self.source, self.output, self.cleaner, kind='mail')

    def test_residual_contacts_abort_instead_of_publishing_success(self):
        self.write(text='新邮件 person@example.org 压力 0.5 MPa')
        with self.assertRaisesRegex(ValueError, 'candidate_residual_audit_failed'):
            repair_run(self.source, self.output, self.cleaner, kind='mail')
        self.assertTrue((self.output/'REPAIR_FAILED.json').exists())
        self.assertFalse((self.output/'report.json').exists())


if __name__ == '__main__':
    unittest.main()
