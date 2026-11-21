"""Synthetic checks for inventory labels and preservation of source splits."""
from pathlib import Path
import tempfile
import unittest

from scripts.audits.audit_existing_cases import audit, classify
from continuous_common import read
from continuous_data import rows
from .test_continuous import foundation_fixture


class ExistingCaseTests(unittest.TestCase):
    def test_proposed_test_is_not_an_observed_result(self):
        _, is_case, clues = classify('压合后出现白斑，怀疑真空不足。建议检查管路。计划在调整后测试结果是否合格。')
        self.assertTrue(is_case)
        self.assertFalse(clues)

    def test_success_and_failure_quotes_retain_exact_source_offsets(self):
        for result in ('复测结果合格。', '复测结果不合格，仍发生分层。'):
            text = '压合出现分层，建议检查管路。' + result
            fields, is_case, clues = classify(text)
            self.assertTrue(is_case)
            self.assertTrue(clues)
            for quote in clues:
                self.assertEqual(quote['text'], text[quote['start']:quote['end']])

    def test_inventory_does_not_import_old_cases_as_new_or_change_splits(self):
        with tempfile.TemporaryDirectory(prefix='pcb-case-inventory-') as tmp:
            root = Path(tmp)
            source, pointer = foundation_fixture(root)
            before = {name: (source / name).read_bytes() for name in ('train_domain.jsonl', 'validation_domain.jsonl')}
            report = audit(root)
            self.assertEqual(report['counts']['train']['case_like_passages'], 1)
            inventory = rows(root / 'data/continuous/existing_cases/train_case_inventory.jsonl')
            self.assertEqual(inventory[0]['split'], 'train')
            self.assertFalse(inventory[0]['new_evidence'])
            self.assertEqual(read(root / 'data/current_model.json'), pointer)
            self.assertFalse(list((root / 'data/continuous/records').glob('*.json')))
            for name, content in before.items():
                self.assertEqual((source / name).read_bytes(), content)
            self.assertEqual(audit(root), report)


if __name__ == '__main__':
    unittest.main()
