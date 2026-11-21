"""Synthetic checks for continued training and historical-case provenance."""
from pathlib import Path
import tempfile
import unittest

from continuous_data import rows
from continuous_common import read
from prepare_case_study import fragments, prepare
from .test_continuous import foundation_fixture
from .test_learning import FakeTokenizer
from train_engineer import instruction_tokens


class CaseStudyTests(unittest.TestCase):
    def test_strict_examples_never_silently_truncate_target(self):
        tokenizer = FakeTokenizer()
        row = {'prompt': '压合材料和叠构条件仍需确认。', 'response': '记录中的测试结果不合格，仍需核查真空管路。',
               'preserve_complete_example': True}
        sample = instruction_tokens(tokenizer, row, 768)
        self.assertIn(row['response'], tokenizer.decode([x for x in sample['labels'] if x != -100]))
        with self.assertRaisesRegex(ValueError, 'exceeds_sequence_length'):
            instruction_tokens(tokenizer, row | {'response': '明确的原文结果。' * 500}, 768)

    def test_fragment_preserves_numeric_sign_and_unit(self):
        text = '甲' * 135 + '-0.085 MPa，记录的压合压力为25 bar。'
        pieces = list(fragments(text))
        self.assertEqual(''.join(p['text'] for p in pieces), text)
        self.assertTrue(any('-0.085 MPa' in p['text'] for p in pieces))
        for p in pieces:
            self.assertEqual(text[p['start']:p['end']], p['text'])

    def test_curriculum_preserves_replay_splits_and_verbatim_targets(self):
        with tempfile.TemporaryDirectory(prefix='pcb-case-study-') as tmp:
            root = Path(tmp)
            source, pointer = foundation_fixture(root)
            output, report = prepare(root)
            original = rows(source / 'train_instruction.jsonl')
            trained = rows(output / 'train_instruction.jsonl')
            self.assertEqual(trained[:len(original)], original)
            self.assertGreater(len(trained), len(original))
            self.assertEqual((output / 'validation_domain.jsonl').read_bytes(), (source / 'validation_domain.jsonl').read_bytes())
            self.assertEqual((output / 'validation_instruction.jsonl').read_bytes(), (source / 'validation_instruction.jsonl').read_bytes())
            source_rows = {r['source_id']: r for r in rows(source / 'train_domain.jsonl')}
            for row in trained[len(original):]:
                text = source_rows[row['source_id']]['text']
                self.assertEqual(row['target_quote'], text[row['target_start']:row['target_end']])
                self.assertIn(row['target_quote'], row['response'])
                self.assertTrue(row['preserve_complete_example'])
                self.assertIn(row['group_id'], {r['group_id'] for r in original})
            self.assertEqual(read(root / 'data/current_model.json'), pointer)
            self.assertFalse(report['case_study']['new_external_evidence'])
            self.assertEqual(prepare(root)[0], output)


if __name__ == '__main__':
    unittest.main()
