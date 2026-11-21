"""Synthetic fixtures: evidence isolation, version gates, crash-safe publication."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from continuous_common import (CAPABILITIES, fingerprint, lock, pointer_for, publish,
                               read, rollback, setup, submit_case, status)
from continuous_data import FILES, initialize, rows, snapshot
from continuous_ingest import ingest
from continuous_learning import cycle
from evaluate_continuous import behavior_check, decide
from learning_common import ROOT, digest_file, dump_json
from mail_privacy import Sanitizer, IdentityIndex


def synthetic_case(number=0):
    return {'case_id': '虚构案例' + str(number), 'kind': 'outcome', 'capability': CAPABILITIES[0],
            'context': f'压合后出现第{number}种局部白斑，材料叠构与真空曲线保持原始记录，缺陷位置在边缘。',
            'conclusion': '仅记录本批次切片中观察到的改善，具体原因仍需重复试验确认。',
            'evidence': '虚构测试记录：采用相同叠构设置对照组，逐片检查白斑和切片结果；重复性尚待验证。'}


def model_fixture(root, name='model_training_fixture', data=None):
    run = root / 'data' / name
    adapter = run / 'instruction_adapter'
    adapter.mkdir(parents=True)
    (adapter / 'adapter_model.safetensors').write_bytes(('synthetic-' + name).encode())
    digest = digest_file(adapter / 'adapter_model.safetensors')
    dump_json(run / 'report.json', {'status': 'completed', 'optimizer_updates': 1, 'selected_adapter': 'instruction_adapter',
               'adapter_artifacts': {'instruction_adapter': {'adapter_model.safetensors': digest}}})
    dump_json(run / 'inference_config.json', {'adapter_scale': .5, 'adapter_sha256': digest})
    if data:
        dump_json(run / 'run_config.json', {'data_report_sha256': digest_file(data / 'report.json')})
    return pointer_for(run, 'synthetic-source')


def foundation_fixture(root):
    setup(root)
    dump_json(root / 'continuous_policy.json', read(ROOT / 'continuous_policy.json'))
    data = root / 'data/training_data_fixture'
    data.mkdir()
    texts = {'train': '压合中出现边缘白斑，需要核查真空记录并采取切片观察。试验尚未完成，原因和改善结果仍需进一步确认。',
             'validation': '树脂固化过程应当核对升温曲线和材料叠构。这里是必须隔离的压合试验记录，不能把未知验证结果当作通用工艺。'}
    artifacts = {}
    for name in FILES:
        split, kind = name[:-6].split('_')
        value = {'group_id': 'base_' + split, 'source_id': 'base_' + split, 'text': texts[split],
                 'prompt': '请分析虚构压合试验的适用条件与未知结果。', 'response': texts[split]}
        (data / name).write_text(json.dumps(value, ensure_ascii=False) + '\n')
        artifacts[name] = {'sha256': digest_file(data / name)}
    dump_json(data / 'report.json', {'status': 'completed', 'artifacts': artifacts})
    pointer = model_fixture(root, data=data)
    dump_json(root / 'data/current_model.json', pointer)
    return data, pointer


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pcb-continuous-test-')
        self.root = Path(self.tmp.name)
        self.data, self.pointer = foundation_fixture(self.root)
        self.engine = Sanitizer(IdentityIndex().pattern())

    def tearDown(self):
        self.tmp.cleanup()

    def add_record(self, ident, text, group=None):
        record = {'id': ident, 'kind': 'document', 'group_id': group or ident, 'eligible': True,
                  'created_at': time.time() - 90000, 'samples': [{'text': text, 'source_kind': 'untrusted_attachment'}]}
        dump_json(self.root / 'data/continuous/records' / (ident + '.json'), record)
        return record

    def test_no_data_no_training_and_worker_status_truthful(self):
        with patch('continuous_learning.execute') as execute:
            cycle(self.root, force=True)
            self.assertEqual(execute.call_count, 1)  # intake only, no trainer/evaluator
        self.assertEqual(status(self.root)['state'], 'waiting_for_new_data')
        self.assertFalse(status(self.root)['worker_alive'])
        with lock('learning_worker', self.root):
            self.assertTrue(status(self.root)['worker_alive'])

    def test_no_assistant_targets_and_evidence_required(self):
        with self.assertRaises(ValueError):
            submit_case(synthetic_case() | {'model_answer': '不可信回答'}, self.root)
        with self.assertRaises(ValueError):
            submit_case(synthetic_case() | {'evidence': ''}, self.root)
        submit_case(synthetic_case() | {'kind': 'note', 'evidence': ''}, self.root)
        ingest(self.root, self.engine)
        record = read(next((self.root / 'data/continuous/records').glob('*.json')))
        self.assertFalse(record['eligible'])
        self.assertTrue(record['recorded_fields']['conclusion'])
        self.assertEqual(record['samples'], [])

    def test_submission_dedup_and_local_sanitation(self):
        case = synthetic_case() | {'context': synthetic_case()['context'] + '\n联系邮箱 test@example.com'}
        submit_case(case, self.root)
        ingest(self.root, self.engine)
        submit_case(case, self.root)
        ingest(self.root, self.engine)
        records = list((self.root / 'data/continuous/records').glob('*.json'))
        self.assertEqual(len(records), 1)
        self.assertNotIn('test@example.com', records[0].read_text())
        self.assertEqual(len(list((self.root / '.private/learning_pending').glob('*.json'))), 0)

    def test_fixed_holdout_is_not_copied_into_training(self):
        held = rows(self.data / 'validation_domain.jsonl')[0]['text']
        self.add_record('copy', held)
        prepared, report = snapshot(self.root)
        self.assertEqual(prepared, self.data)
        self.assertFalse(report['continuous']['training_record_ids'])
        self.assertEqual(rows(prepared / 'train_domain.jsonl'), rows(self.data / 'train_domain.jsonl'))

    def test_cumulative_data_and_permanent_case_split(self):
        for n in range(20):
            self.add_record(f'doc_{n}', f'压合第{n}种实验出现局部空洞，采用对应的第{n}种树脂和不同铜厚。需要检查第{n}次材料测试并核对真空管路，切片和可靠性测试结果还未提供。')
        prepared, report = snapshot(self.root)
        train = rows(prepared / 'train_domain.jsonl')
        held = rows(prepared / 'new_validation_domain.jsonl')
        self.assertTrue(held)
        self.assertGreater(len(train), 1)
        self.assertFalse({r['group_id'] for r in train} & {r['group_id'] for r in held})
        self.assertEqual(rows(prepared / 'validation_domain.jsonl'), rows(self.data / 'validation_domain.jsonl'))
        assignments = read(self.root / 'data/continuous/split_assignments.json')
        held_group = next(k for k, v in assignments.items() if v == 'validation')
        self.add_record('latest_revision', '压合修订案例发现新的分层位置，需要重新分析该材料的固化情况以及试验条件。建议检查真空记录并做切片验证，结果仍需等待后续记录。', group=held_group)
        second, _ = snapshot(self.root)
        self.assertEqual(read(self.root / 'data/continuous/split_assignments.json')[held_group], 'validation')
        self.assertNotIn('latest_revision', {r['source_id'] for r in rows(second / 'train_domain.jsonl')})
        self.assertEqual(snapshot(self.root)[0], second)

    def test_bridge_between_previous_splits_is_quarantined(self):
        long_a = '压合材料的真空状态需要检查。' + '甲乙丙丁戊己庚辛壬癸' * 12
        long_b = '压合材料的固化状态需要检查。' + '子丑寅卯辰巳午未申酉' * 12
        self.add_record('prior_train', long_a)
        self.add_record('prior_held', long_b)
        dump_json(self.root / 'data/continuous/split_assignments.json', {'prior_train': 'train', 'prior_held': 'validation'})
        snapshot(self.root)
        self.add_record('bridge', long_a + '\n' + long_b)
        prepared, report = snapshot(self.root)
        self.assertNotIn('bridge', report['continuous']['training_record_ids'])
        self.assertNotIn('bridge', report['continuous']['holdout_record_ids'])

    def test_publish_reject_rollback_and_compare_swap(self):
        candidate = model_fixture(self.root, 'model_training_next')
        with self.assertRaisesRegex(ValueError, 'candidate_not_accepted'):
            publish(candidate, self.pointer, {'accepted': False}, self.root)
        self.assertEqual(read(self.root / 'data/current_model.json'), self.pointer)
        publish(candidate, self.pointer, {'accepted': True}, self.root)
        with self.assertRaisesRegex(ValueError, 'incumbent_changed'):
            publish(candidate, self.pointer, {'accepted': True}, self.root)
        self.assertEqual(rollback(self.root), self.pointer['run_name'])
        self.assertEqual(read(self.root / 'data/current_model.json'), self.pointer)
        # Artifacts are checked again, not trusted from a registry row.
        (self.root / 'data' / candidate['run_name'] / 'instruction_adapter/adapter_model.safetensors').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            publish(candidate, self.pointer, {'accepted': True}, self.root)

    def test_gate_requires_gain_and_preserves_individual_passes(self):
        policy = read(self.root / 'continuous_policy.json')
        base = {'losses': {'frozen_domain': 3., 'frozen_instruction': 2., 'new_domain': 3.},
                'checks': {'a': {'passed': True, 'critical': True}, 'b': {'passed': True, 'critical': False}}}
        self.assertFalse(decide(base, base, policy)['accepted'])
        improved = copy.deepcopy(base)
        improved['losses'] = {k: v * .95 for k, v in base['losses'].items()}
        self.assertTrue(decide(base, improved, policy)['accepted'])
        improved['checks']['b']['passed'] = False
        self.assertFalse(decide(base, improved, policy)['accepted'])
        improved['checks']['b']['passed'] = True
        improved['losses']['frozen_domain'] = 3.2
        self.assertFalse(decide(base, improved, policy)['accepted'])
        improved['losses']['frozen_domain'] = float('nan')
        self.assertFalse(decide(base, improved, policy)['accepted'])

    def test_number_checks_bind_units_and_signs(self):
        spec = {'quantities': [['23', 'bar'], ['-0.082', 'MPa']]}
        self.assertTrue(behavior_check(spec, '23 bar，-0.082 MPa'))
        for answer in ('23 MPa，-0.082 bar', '23 bar，0.082 MPa', '23 bar，-0.082 MPa，30 min'):
            self.assertFalse(behavior_check(spec, answer))

    def test_negated_success_is_not_scored_as_fabricated_success(self):
        spec = {'required': ['不能|需要'], 'assertion_forbidden': ['已经验证成功']}
        self.assertTrue(behavior_check(spec, '不能宣称已经验证成功，需要实际测试。'))
        self.assertFalse(behavior_check(spec, '已经验证成功，需要扩大生产。'))
        self.assertFalse(behavior_check(spec, '不能否认已经验证成功。'))
        self.assertFalse(behavior_check(spec, '不能忽略检查，但已经验证成功。'))

    def test_worker_trains_compares_promotes_once_and_rejection_keeps_current(self):
        self.add_record('new', '压合出现白斑后应当先检查材料叠构的含胶情况，并结合真空管路状态分析问题。建议采用切片试验验证原因，不能直接推定已有效。')
        dump_json(self.root / 'data/continuous/split_assignments.json', {'new': 'train'})
        calls = []
        def execute(command, root):
            calls.append(command[1])
            if command[1].endswith('continuous_learning.py'):
                model_fixture(root, command[command.index('--run-name') + 1])
            elif command[1].endswith('evaluate_continuous.py'):
                output = Path(command[command.index('--output') + 1])
                dump_json(output / 'promotion_evaluation.json', {'accepted': True, 'incumbent_run': self.pointer['run_name'], 'candidate_run': output.name})
        with patch('continuous_learning.execute', side_effect=execute):
            cycle(self.root, force=True)
            selected = read(self.root / 'data/current_model.json')
            self.assertNotEqual(selected, self.pointer)
            cycle(self.root, force=True)
        self.assertEqual(sum(p.endswith('continuous_learning.py') for p in calls), 1)
        self.assertEqual(status(self.root)['last_result'], 'promoted')

    def test_rejected_candidate_is_not_retried_without_new_data(self):
        self.add_record('new', '压合实验出现分层，需要结合固化与真空记录分析本次缺陷。建议增加相同材料的对照切片试验，试验尚未完成不能判定原因。')
        dump_json(self.root / 'data/continuous/split_assignments.json', {'new': 'train'})
        calls = []
        def execute(command, root):
            calls.append(command[1])
            if command[1].endswith('continuous_learning.py'):
                model_fixture(root, command[command.index('--run-name') + 1])
            elif command[1].endswith('evaluate_continuous.py'):
                output = Path(command[command.index('--output') + 1])
                dump_json(output / 'promotion_evaluation.json', {'accepted': False, 'incumbent_run': self.pointer['run_name'], 'candidate_run': output.name})
        with patch('continuous_learning.execute', side_effect=execute):
            cycle(self.root, force=True)
            cycle(self.root, force=True)
        self.assertEqual(read(self.root / 'data/current_model.json'), self.pointer)
        self.assertEqual(sum(p.endswith('continuous_learning.py') for p in calls), 1)
        self.assertEqual(status(self.root)['last_result'], 'rejected')

    def test_crash_after_promotion_recovers_without_retraining(self):
        self.add_record('new', '压合实验出现分层，需要结合固化与真空记录分析本次缺陷。建议增加相同材料的对照切片试验，试验尚未完成不能判定原因。')
        dump_json(self.root / 'data/continuous/split_assignments.json', {'new': 'train'})
        prepared, report = snapshot(self.root)
        name = 'model_training_cont_' + report['continuous']['revision'][:12]
        model_fixture(self.root, name)
        candidate = pointer_for(self.root / 'data' / name, report['source_artifact_sha256'])
        publish(candidate, self.pointer, {'accepted': True}, self.root)
        with patch('continuous_learning.execute') as execute:
            cycle(self.root, force=True)
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(status(self.root)['last_result'], 'promoted')
        self.assertTrue(read(self.root / 'data/continuous/last_attempt.json')['recovered_commit'])


if __name__ == '__main__':
    unittest.main()
