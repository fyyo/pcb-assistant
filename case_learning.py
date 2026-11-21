"""Restartable existing-case study: inherit -> train -> compare -> publish."""
import argparse
import os
from pathlib import Path
import subprocess
import threading
import time

from continuous_common import ROOT, fingerprint, lock, pointer_for, publish, read, training_gpu, verify_pointer
from continuous_data import initialize
from continuous_learning import execute, update_state
from learning_common import digest_file, dump_json, emit


def validate_tokens(data):
    from transformers import AutoTokenizer
    from train_engineer import instruction_tokens, jsonl
    tokenizer = AutoTokenizer.from_pretrained(ROOT / 'models/Qwen2.5-0.5B-Instruct', local_files_only=True, trust_remote_code=False)
    length = read(data / 'report.json')['case_study']['maximum_sequence_length']
    counts = {}
    for name in ('train_instruction.jsonl', 'case_validation_instruction.jsonl'):
        total, maximum, strict = 0, 0, 0
        for row in jsonl(data / name):
            encoded = instruction_tokens(tokenizer, row, length)
            total += 1
            maximum = max(maximum, len(encoded['input_ids']))
            strict += bool(row.get('preserve_complete_example'))
        counts[name] = {'samples': total, 'complete_additional_samples': strict, 'max_tokens': maximum}
    dump_json(data / 'token_validation.json', {'status': 'passed', 'counts': counts})
    emit('case_lesson_tokens_verified', counts=counts)


def run():
    from prepare_case_study import prepare
    with lock('case_study_runner'), lock('learning_cycle', blocking=True):
        if read(ROOT / 'continuous_policy.json').get('paused_by_user'):
            emit('existing_case_study_paused_by_user')
            return
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(30):
                update_state()
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            foundation = initialize()
            update_state(state='preparing_case_lessons', active_task='existing_case_study')
            data, report = prepare()
            job_path = ROOT / 'data/continuous/case_study_job.json'
            job = read(job_path)
            if not job:
                current = read(ROOT / 'data/current_model.json')
                ident = fingerprint({'parent': current, 'data': digest_file(data / 'report.json'),
                                     'recipe': 'continue-served-scale-lr1e-5-one-case-epoch-v1'})[:12]
                job = {'state': 'prepared', 'parent': current, 'data_run': data.name,
                       'data_report_sha256': digest_file(data / 'report.json'),
                       'run_name': 'model_training_cases_' + ident, 'created_at': time.time()}
                dump_json(job_path, job)
            if job['data_report_sha256'] != digest_file(data / 'report.json'):
                raise ValueError('case_study_recipe_changed')
            if job['state'] in ('promoted', 'rejected'):
                update_state(state='waiting_for_new_data', case_study_result=job['state'], active_task=None)
                emit('case_study_already_completed', result=job['state'], run_name=job['run_name'])
                return
            parent = verify_pointer(job['parent'])
            output = ROOT / 'data' / job['run_name']
            current = read(ROOT / 'data/current_model.json')
            if current.get('run_name') == job['run_name']:
                verify_pointer(current)
                job.update(state='promoted', recovered_commit=True)
                dump_json(job_path, job)
                update_state(state='waiting_for_new_data', case_study_result='promoted', active_task=None)
                return
            if current != job['parent']:
                raise ValueError('case_study_parent_changed')
            update_state(state='checking_case_lessons', candidate_run=output.name,
                         case_lesson_counts=report['case_study']['lesson_counts'])
            execute([str(ROOT / '.venv-train/bin/python'), str(ROOT / 'case_learning.py'), 'check-tokens', '--data', str(data)])
            with training_gpu():
                if not (output / 'report.json').exists():
                    update_state(state='training', active_task='existing_case_study', candidate_run=output.name)
                    command = [str(ROOT / '.venv-train/bin/python'), str(ROOT / 'train_engineer.py'),
                               '--data', str(data), '--run-name', output.name, '--initial-run', str(parent),
                               '--domain-steps', '0', '--instruction-epochs', '1', '--instruction-limit', '0',
                               '--sequence-length', str(report['case_study']['maximum_sequence_length']),
                               '--accumulation', '4', '--validation-batches', '64', '--instruction-learning-rate', '0.00001']
                    if (output / 'run_config.json').exists():
                        command.append('--resume')
                    job.update(state='training', started_at=time.time())
                    dump_json(job_path, job)
                    execute(command)
                trained = read(output / 'report.json')
                if trained['optimizer_updates'] <= 0 or trained['changed_parameter_tensors'] <= 0:
                    raise ValueError('no_case_parameter_updates')
                candidate = pointer_for(output, report['source_artifact_sha256'])
                update_state(state='comparing', active_task='existing_case_study')
                execute([str(ROOT / '.venv-train/bin/python'), str(ROOT / 'evaluate_continuous.py'),
                         '--incumbent', str(parent), '--candidate', str(output), '--data', str(data),
                         '--foundation', str(ROOT / 'data' / foundation['prepared']), '--output', str(output)])
                decision = read(output / 'promotion_evaluation.json')
                if decision['incumbent_run'] != parent.name or decision['candidate_run'] != output.name:
                    raise ValueError('case_comparison_mismatch')
                if decision['accepted']:
                    publish(candidate, job['parent'], decision)
            job.update(state='promoted' if decision['accepted'] else 'rejected',
                       completed_at=time.time(), decision=decision,
                       optimizer_updates=trained['optimizer_updates'], trained_tokens=trained['trained_tokens'])
            dump_json(job_path, job)
            update_state(state='waiting_for_new_data', active_task=None, case_study_result=job['state'],
                         last_decision=decision, last_result=job['state'])
            emit('existing_case_study_finished', state=job['state'], optimizer_updates=trained['optimizer_updates'])
        except Exception as exc:
            saved = read(ROOT / 'data/continuous/case_study_job.json')
            if saved and saved.get('state') not in ('promoted', 'rejected'):
                saved.update(state='error', error_type=type(exc).__name__, failed_at=time.time(),
                             retry_count=saved.get('retry_count', 0) + 1)
                dump_json(ROOT / 'data/continuous/case_study_job.json', saved)
            update_state(state='case_study_error', error_type=type(exc).__name__)
            raise
        finally:
            stop.set()
            thread.join(timeout=2)


def start():
    with lock('case_study_start'):
        if read(ROOT / 'continuous_policy.json').get('paused_by_user'):
            emit('existing_case_study_paused_by_user')
            return
        record = read(ROOT / 'data/continuous/case_study_process.json', {})
        pid = record.get('pid')
        if pid:
            process = Path('/proc') / str(pid)
            try:
                args = (process / 'cmdline').read_bytes().decode().split('\0')
                if (process / 'cwd').resolve() == ROOT and str(ROOT / 'case_learning.py') in args and 'run' in args:
                    emit('existing_case_study_running', pid=pid)
                    return
            except OSError:
                pass
        with (ROOT / 'data/continuous/case_study_service.log').open('ab') as log:
            child = subprocess.Popen([str(ROOT / '.venv/bin/python'), str(ROOT / 'case_learning.py'), 'run'],
                                     cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        dump_json(ROOT / 'data/continuous/case_study_process.json', {'pid': child.pid})
        emit('existing_case_study_started', pid=child.pid)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('start', 'run', 'status', 'check-tokens'))
    parser.add_argument('--data', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'start':
            start()
        elif args.command == 'run':
            run()
        elif args.command == 'check-tokens':
            validate_tokens(args.data)
        else:
            job = read(ROOT / 'data/continuous/case_study_job.json', {})
            progress = read(ROOT / 'data' / job.get('run_name', 'missing') / 'progress.json', {})
            emit('existing_case_study_status', state=job.get('state', 'not_started'),
                 run_name=job.get('run_name'), progress=progress)
    except Exception as exc:
        emit('existing_case_study_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
