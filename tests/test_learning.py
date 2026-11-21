"""Synthetic-only tests for real model-training data and label construction."""
import collections
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pcb_assistant.knowledge import digest_file
from pcb_assistant.training.prepare import prepare, eligible, instruction
from pcb_assistant.training.train import instruction_tokens
from pcb_assistant.training.local import recipe_matches, run as auto_learn


class FakeTokenizer:
    def encode(self, text, add_special_tokens=False):
        return [ord(character) for character in text]

    def decode(self, ids, skip_special_tokens=True):
        return ''.join(chr(value) for value in ids)

    def apply_chat_template(self, messages, tokenize=True, add_generation_prompt=False):
        text = ''.join('<' + message['role'] + '>' + message['content'] + '</end>' for message in messages)
        if add_generation_prompt:
            text += '<assistant>'
        return self.encode(text)


class LearningTests(unittest.TestCase):
    def test_recipe_rejects_smoke_run_and_different_data(self):
        config = {'data_report_sha256': 'data', 'base_revision': 'base', 'domain_steps': 0,
                  'instruction_epochs': 1, 'instruction_limit': 0, 'sequence_length': 512,
                  'gradient_accumulation': 4, 'validation_batches': 64}
        self.assertTrue(recipe_matches(config, 'data', 'base'))
        self.assertFalse(recipe_matches(config | {'domain_steps': 1}, 'data', 'base'))
        self.assertFalse(recipe_matches(config, 'changed', 'base'))

    def test_automatic_learning_reuses_completed_weights_without_retraining(self):
        with tempfile.TemporaryDirectory(prefix='pcb-auto-learning-') as tmp:
            root = Path(tmp)
            data = root / 'data'
            source = data / 'sanitized_fixture'
            prepared = data / 'training_data_fixture'
            trained = data / 'model_training_fixture'
            for directory in (source, prepared, trained / 'instruction_adapter'):
                directory.mkdir(parents=True)
            original = source / 'engineering_candidates.jsonl'
            original.write_text('{"text":"虚构压合数据"}\n')
            source_hash = digest_file(original)
            (source / 'report.json').write_text(json.dumps({'status': 'completed_local_candidates_require_review',
                'artifacts': {original.name: {'sha256': source_hash}}}))
            (prepared / 'report.json').write_text(json.dumps({'status': 'completed', 'source_artifact_sha256': source_hash}))
            (root / 'config/training').mkdir(parents=True)
            (root / 'config/training/model.json').write_text('{"revision":"synthetic-base"}')
            config = {'data_report_sha256': digest_file(prepared / 'report.json'), 'base_revision': 'synthetic-base',
                      'domain_steps': 0, 'instruction_epochs': 1, 'instruction_limit': 0, 'sequence_length': 512,
                      'gradient_accumulation': 4, 'validation_batches': 64}
            (trained / 'run_config.json').write_text(json.dumps(config))
            artifact = trained / 'instruction_adapter/adapter_model.safetensors'
            artifact.write_bytes(b'synthetic-weights-not-a-real-model')
            (trained / 'report.json').write_text(json.dumps({'status': 'completed', 'measured_loss_improved': True,
                'optimizer_updates': 100, 'changed_parameter_tensors': 1, 'trained_tokens': 1000,
                'selected_adapter': 'instruction_adapter',
                'adapter_artifacts': {'instruction_adapter': {artifact.name: digest_file(artifact)}}}))
            (trained / 'inference_config.json').write_text(json.dumps({'adapter_scale': .25, 'adapter_sha256': digest_file(artifact)}))
            with patch('pcb_assistant.training.local.ROOT', root), patch('pcb_assistant.training.local.subprocess.run') as subprocess_run, contextlib.redirect_stdout(io.StringIO()):
                auto_learn(source)
                subprocess_run.assert_not_called()
            selected = json.loads((data / 'current_model.json').read_text())
            self.assertEqual(selected['run_name'], trained.name)
            self.assertEqual(selected['report_sha256'], digest_file(trained / 'report.json'))

    def test_filters_and_no_fabricated_success(self):
        self.assertFalse(eligible('[身份]' * 50 + '压合'))
        self.assertFalse(eligible('请处理行政文件并完成一般资料归档。' * 10))
        text = '压合后出现白斑，怀疑真空不足，需要检查真空管路。建议增加切片试验以确认原因，当前尚未提供具体的测试结果。'
        self.assertTrue(eligible(text))
        row = instruction(text)
        self.assertIsNotNone(row)
        self.assertIn('尚未提供', row['response'])
        self.assertNotIn('验证通过', row['response'])

    def test_labels_exclude_prompt_and_preserve_assistant_target(self):
        tokenizer = FakeTokenizer()
        row = {'prompt': '工艺记录：' + '温度条件待确认。' * 200, 'response': '建议先检查真空管路，不编造验证结果。'}
        encoded = instruction_tokens(tokenizer, row, 512)
        self.assertLessEqual(len(encoded['input_ids']), 512)
        self.assertIn(-100, encoded['labels'])
        targets = [value for value in encoded['labels'] if value != -100]
        self.assertTrue(tokenizer.decode(targets).startswith('建议先检查真空管路'))
        self.assertEqual(len(encoded['labels']), len(encoded['input_ids']))

    def test_group_split_keeps_duplicate_evidence_out_of_holdout(self):
        with tempfile.TemporaryDirectory(prefix='pcb-training-fixture-') as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            records = []
            for number in range(12):
                # Distinct long phrases avoid accidental grouping of all synthetic data.
                text = f'压合后出现第{number}类白斑，材料叠构编号为{number}。怀疑真空与温度条件存在差异。建议针对第{number}类问题检查管路并进行切片试验，当前试验结果尚未确认，需要进一步分析。'
                records.append({'id': 'mail_' + format(number, '024x'), 'subject': f'试验课题第{number}个独立主题', 'text': text})
            records.append(records[0] | {'id': 'mail_' + format(99, '024x'), 'subject': '另一个邮件标题'})
            for number, parent in enumerate((0, 0, 2)):
                records.append({'id': f'att_synthetic_{number}', 'subject': 'XLSX 附件工作表第一节',
                    'text': f'附件第{number}段记录压合温度与压力，材料叠构需要根据不同树脂参数确认。针对第{number}类白斑，建议检查真空管路，并进行切片和可靠性试验，具体结果尚未确认。',
                    'source_kind': 'untrusted_attachment', 'document_id': 'doc_first' if parent == 0 else 'doc_second',
                    'parent_source_ids': [records[parent]['id']]})
            path = source / 'engineering_candidates.jsonl'
            path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records))
            (source / 'report.json').write_text(json.dumps({'status': 'completed_local_candidates_require_review', 'artifacts': {path.name: {'sha256': digest_file(path)}}}))
            report = prepare(source, root / 'prepared')
            train = [json.loads(line) for line in (root / 'prepared/train_domain.jsonl').read_text().splitlines()]
            validation = [json.loads(line) for line in (root / 'prepared/validation_domain.jsonl').read_text().splitlines()]
            self.assertTrue(train and validation)
            self.assertFalse({row['group_id'] for row in train} & {row['group_id'] for row in validation})
            self.assertFalse({row['fingerprint'] for row in train} & {row['fingerprint'] for row in validation})
            self.assertEqual(len(train) + len(validation), 15)
            grouped = {row['source_id']: row['group_id'] for row in train + validation}
            self.assertEqual(grouped['att_synthetic_0'], grouped[records[0]['id']])
            self.assertEqual(grouped['att_synthetic_1'], grouped[records[0]['id']])
            self.assertEqual(grouped['att_synthetic_2'], grouped[records[2]['id']])
            self.assertNotEqual(grouped['att_synthetic_0'], grouped['att_synthetic_2'])
            self.assertTrue(report['policy']['local_model_training_allowed'])
            original = {row['id']: row for row in records}
            for row in train + validation:
                self.assertEqual(original[row['source_id']]['text'][row['start']:row['end']], row['text'])


if __name__ == '__main__':
    unittest.main()
