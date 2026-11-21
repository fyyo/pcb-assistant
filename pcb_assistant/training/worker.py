"""Restartable local worker: ingest -> cumulative train -> compare -> promote."""
from pcb_assistant.paths import python_executable
import argparse
import os
from pathlib import Path
import subprocess
import threading
import time

from pcb_assistant.training.storage import (ROOT, lock, pointer_for, publish, read,
                               rollback, setup, status, submit_case, training_gpu, verify_pointer)
from pcb_assistant.training.data import initialize, snapshot
from pcb_assistant.training.common import digest_file, dump_json, emit


def update_state(root=ROOT, **values):
    with lock('learning_status', root, blocking=True):
        state = read(root / 'data/continuous/status.json', {})
        dump_json(root / 'data/continuous/status.json', {**state, **values, 'heartbeat_at': time.time()})


def execute(command, root=ROOT):
    # Any third-party traceback/generation remains in private local logs.
    with (root / 'data/continuous/worker.log').open('ab') as log:
        subprocess.run(command, check=True, cwd=root, stdout=log, stderr=log)


def cycle(root=ROOT, force=False):
    policy = read(root / 'continuous_policy.json')
    if not policy['enabled']:
        update_state(root, state='paused' if policy.get('paused_by_user') else 'disabled')
        return
    with lock('learning_cycle', root):
        foundation = initialize(root)
        update_state(root, state='processing_intake')
        execute([python_executable('.venv', root), '-m', 'pcb_assistant.training.ingest'], root)
        data, report = snapshot(root)
        info = report['continuous']
        cycle_id = info['revision']
        receipt_path = root / 'data/continuous/cycles' / (cycle_id + '.json')
        receipt = read(receipt_path, {})
        last = read(root / 'data/continuous/last_attempt.json', {})
        pending = set(info['training_record_ids']) - set(last.get('training_record_ids', []))
        records = [read(root / 'data/continuous/records' / (ident + '.json')) for ident in pending]
        oldest = min((r['created_at'] for r in records), default=time.time())
        common = {'eligible_training_records': len(info['training_record_ids']),
                  'heldout_records': len(info['holdout_record_ids']), 'pending_training_records': len(pending),
                  'excluded_holdout_overlap': info['excluded_holdout_overlap'],
                  'intake': read(root / 'data/continuous/intake_status.json', {})}
        if receipt.get('state') in ('promoted', 'rejected'):
            if receipt.get('time', 0) > last.get('time', 0):
                dump_json(root / 'data/continuous/last_attempt.json', receipt)
            update_state(root, **common, state='waiting_for_new_data', last_result=receipt['state'])
            return
        if not pending:
            update_state(root, **common, state='waiting_for_new_data')
            return
        if (not force and len(pending) < policy['minimum_new_training_records']
                and time.time() - oldest < policy['maximum_batch_wait_hours'] * 3600):
            update_state(root, **common, state='collecting_batch', next_batch_at=oldest + policy['maximum_batch_wait_hours'] * 3600)
            return
        if receipt.get('state') == 'error' and time.time() - receipt['time'] < policy['failure_retry_hours'] * 3600:
            update_state(root, **common, state='retry_wait', error_type=receipt.get('error_type'))
            return
        current = read(root / 'data/current_model.json')
        incumbent = verify_pointer(current, root)
        run = root / 'data' / ('model_training_cont_' + cycle_id[:12])
        if current['run_name'] == run.name and current.get('source_artifact_sha256') == report['source_artifact_sha256']:
            # Recover a crash after pointer commit but before the cycle receipt.
            receipt = {'state': 'promoted', 'time': time.time(), 'candidate_run': run.name,
                       'training_record_ids': info['training_record_ids'], 'recovered_commit': True}
            dump_json(receipt_path, receipt)
            dump_json(root / 'data/continuous/last_attempt.json', receipt)
            update_state(root, **common, state='waiting_for_new_data', last_result='promoted')
            return
        try:
            update_state(root, **common, state='waiting_for_gpu', candidate_run=run.name)
            with training_gpu(root):
                update_state(root, state='training', candidate_run=run.name)
                execute([python_executable('.venv-train', root), '-m', 'pcb_assistant.training.worker',
                         'train-candidate', '--data', str(data), '--run-name', run.name], root)
                candidate = pointer_for(run, report['source_artifact_sha256'])
                update_state(root, state='comparing', candidate_run=run.name)
                execute([python_executable('.venv-train', root), '-m', 'pcb_assistant.training.evaluate',
                         '--incumbent', str(incumbent), '--candidate', str(run),
                         '--foundation', str(root / 'data' / foundation['prepared']),
                         '--data', str(data), '--output', str(run)], root)
                decision = read(run / 'promotion_evaluation.json')
                if decision['incumbent_run'] != incumbent.name or decision['candidate_run'] != run.name:
                    raise ValueError('comparison_version_mismatch')
                if decision['accepted']:
                    publish(candidate, current, decision, root)
            receipt = {'state': 'promoted' if decision['accepted'] else 'rejected',
                       'time': time.time(), 'candidate_run': run.name, 'decision': decision,
                       'training_record_ids': info['training_record_ids']}
            dump_json(receipt_path, receipt)
            dump_json(root / 'data/continuous/last_attempt.json', receipt)
            update_state(root, state='waiting_for_new_data', last_result=receipt['state'],
                         last_decision=decision, pending_training_records=0)
        except Exception as exc:
            receipt = {'state': 'error', 'time': time.time(), 'error_type': type(exc).__name__, 'candidate_run': run.name}
            dump_json(receipt_path, receipt)
            update_state(root, state='retry_wait', error_type=type(exc).__name__)
            raise


def watch(root=ROOT):
    setup(root)
    with lock('learning_worker', root):
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(30):
                update_state(root)
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    policy = read(root / 'continuous_policy.json')
                    if not policy['enabled']:
                        update_state(root, state='paused' if policy.get('paused_by_user') else 'disabled')
                        stop.wait(max(10, policy['poll_seconds']))
                        continue
                    case_job = read(root / 'data/continuous/case_study_job.json', {})
                    resume_case = (case_job.get('state') in ('prepared', 'training') or
                                   (case_job.get('state') == 'error' and case_job.get('retry_count', 0) < 3
                                    and time.time() - case_job.get('failed_at', 0) >=
                                    read(root / 'continuous_policy.json')['failure_retry_hours'] * 3600))
                    if resume_case:
                        execute([python_executable('.venv', root), '-m', 'pcb_assistant.training.cases', 'start'], root)
                        update_state(root)
                    else:
                        cycle(root)
                except BlockingIOError:
                    # Preserve the concrete progress of another authorized
                    # pipeline (for example a lesson from historical cases).
                    update_state(root)
                except Exception as exc:
                    update_state(root, state='retry_wait', error_type=type(exc).__name__)
                stop.wait(max(10, read(root / 'continuous_policy.json')['poll_seconds']))
        finally:
            stop.set()
            thread.join(timeout=2)
            update_state(root, state='stopped')


def main():
    parser = argparse.ArgumentParser(description='本地持续学习、候选评测与自动升级')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'status', 'watch', 'rollback', 'assess'):
        commands.add_parser(name)
    once = commands.add_parser('once')
    once.add_argument('--force', action='store_true', help='立即处理小批量新增数据，仍须通过自动评测')
    submit = commands.add_parser('submit')
    submit.add_argument('file', type=Path)
    candidate = commands.add_parser('train-candidate')
    candidate.add_argument('--data', type=Path, required=True)
    candidate.add_argument('--run-name', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    setup()
    try:
        if args.command == 'init':
            initialize()
            emit('continuous_learning_initialized')
        elif args.command == 'status':
            emit('continuous_learning_status', **status())
        elif args.command == 'watch':
            watch()
        elif args.command == 'once':
            cycle(force=args.force)
        elif args.command == 'submit':
            emit('engineering_case_submitted', **submit_case(read(args.file)))
        elif args.command == 'rollback':
            emit('model_rolled_back', run_name=rollback())
        elif args.command == 'assess':
            with lock('learning_cycle'):
                foundation = initialize()
                run = verify_pointer(read(ROOT / 'data/current_model.json'))
                data = ROOT / 'data' / foundation['prepared']
                with training_gpu():
                    update_state(state='comparing')
                    from pcb_assistant.training.evaluate import evaluate
                    result = evaluate(run, data, data, ROOT / 'data/continuous/reference_evaluation.json')
                passed = sum(check['passed'] for check in result['checks'].values())
                update_state(state='waiting_for_new_data', reference_behavior_passed=passed,
                             reference_behavior_total=len(result['checks']))
                emit('current_model_assessed', passed=passed, total=len(result['checks']), chief_engineer_certified=False)
        else:
            import re
            from pcb_assistant.training.local import train_candidate
            if not re.fullmatch(r'model_training_cont_[a-f0-9]{12}', args.run_name):
                raise ValueError('invalid_candidate_run')
            run = ROOT / 'data' / args.run_name
            completed = read(run / 'report.json', {}).get('status') == 'completed'
            if completed and read(run / 'run_config.json')['data_report_sha256'] != digest_file(args.data / 'report.json'):
                raise ValueError('candidate_data_mismatch')
            train_candidate(args.data, run, completed)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit('continuous_learning_failed', error_type=type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
