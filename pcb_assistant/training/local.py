"""One-command local learning: prepare -> optimize -> evaluate -> select weights.

Unchanged data reuses a matching completed full-epoch run. New data gets separate
versioned artifacts. No downloads or external services are invoked by this script.
"""
from pcb_assistant.paths import python_executable
import argparse
import json
from pathlib import Path
import subprocess
import sys

from pcb_assistant.training.common import ROOT, digest_file, emit


def recipe_matches(configuration, data_hash, revision):
    return (configuration.get('data_report_sha256') == data_hash
            and configuration.get('base_revision') == revision
            and configuration.get('domain_steps') == 0
            and configuration.get('instruction_epochs') == 1
            and configuration.get('instruction_limit') == 0
            and configuration.get('sequence_length') == 512
            and configuration.get('gradient_accumulation') == 4
            and configuration.get('validation_batches') == 64)


def plan(source):
    source = Path(source).resolve()
    report = json.loads((source / 'report.json').read_text())
    if report['status'] not in ('completed_local_candidates_require_review', 'completed_local_training_sources'):
        raise ValueError('source_run_incomplete')
    source_hash = digest_file(source / 'engineering_candidates.jsonl')
    if source_hash != report['artifacts']['engineering_candidates.jsonl']['sha256']:
        raise ValueError('source_checksum_mismatch')
    prepared = None
    for candidate in sorted((ROOT / 'data').glob('training_data*/report.json')):
        data_report = json.loads(candidate.read_text())
        if data_report.get('status') == 'completed' and data_report.get('source_artifact_sha256') == source_hash:
            prepared = candidate.parent
            break
    prepared = prepared or ROOT / 'data' / ('training_data_' + source_hash[:12])
    training_run = ROOT / 'data' / ('model_training_' + source_hash[:12])
    completed = False
    if (prepared / 'report.json').exists():
        data_hash = digest_file(prepared / 'report.json')
        revision = json.loads((ROOT / 'config/training/model.json').read_text())['revision']
        for config_path in sorted((ROOT / 'data').glob('model_training*/run_config.json')):
            configuration = json.loads(config_path.read_text())
            if recipe_matches(configuration, data_hash, revision):
                training_run = config_path.parent
                if (training_run / 'report.json').exists():
                    completed = json.loads((training_run / 'report.json').read_text()).get('status') == 'completed'
                if completed:
                    break
    return {'source': source, 'prepared': prepared, 'training_run': training_run,
            'completed': completed, 'source_sha256': source_hash}


def train_candidate(prepared, training_run, completed=False):
    """Prepare calibrated weights without changing the deployed-model pointer."""
    if not completed:
        import shutil
        if shutil.disk_usage(ROOT).free < 8 * 1024**3:
            raise RuntimeError('insufficient_disk_space_for_training')
        command = [python_executable('.venv-train', ROOT), '-m', 'pcb_assistant.training.train',
                   '--data', str(prepared), '--run-name', training_run.name,
                   '--domain-steps', '0', '--validation-batches', '64']
        if (training_run / 'run_config.json').exists():
            command.append('--resume')
        subprocess.run(command, check=True, cwd=ROOT)
    report = json.loads((training_run / 'report.json').read_text())
    if report.get('optimizer_updates', 0) <= 0 or report.get('changed_parameter_tensors', 0) <= 0:
        raise ValueError('no_confirmed_parameter_updates')
    if report['status'] != 'completed' or not report['measured_loss_improved']:
        raise ValueError('heldout_loss_not_improved')
    selected = report['selected_adapter']
    for name, checksum in report['adapter_artifacts'][selected].items():
        if digest_file(training_run / selected / name) != checksum:
            raise ValueError('adapter_checksum_mismatch')
    if not (training_run / 'inference_config.json').exists():
        subprocess.run([python_executable('.venv-train', ROOT), '-m', 'scripts.training.calibrate_model',
                        '--run', str(training_run), '--data', str(prepared)], check=True, cwd=ROOT)
    inference = json.loads((training_run / 'inference_config.json').read_text())
    if inference['adapter_sha256'] != report['adapter_artifacts'][selected]['adapter_model.safetensors']:
        raise ValueError('calibration_adapter_mismatch')
    return report


def run(source, status_only=False):
    from pcb_assistant.training.storage import lock, pointer_for, publish, read, training_gpu
    chosen = plan(source)
    prepared, training_run = chosen['prepared'], chosen['training_run']
    if status_only:
        emit('automatic_learning_status', prepared=prepared.name, training_run=training_run.name,
             completed=chosen['completed'], training_needed=not chosen['completed'])
        return
    with lock('learning_cycle', ROOT):
        current = read(ROOT / 'data/current_model.json')
        if current and current.get('source_artifact_sha256') != chosen['source_sha256']:
            # Re-splitting a changed archive could leak the frozen holdout. New
            # material must use the continual intake and cumulative snapshot path.
            raise ValueError('use_continuous_learning_inbox_for_new_sources')
        if not (prepared / 'report.json').exists():
            subprocess.run([python_executable('.venv', ROOT), '-m', 'pcb_assistant.training.prepare',
                            '--source-run', str(chosen['source']), '--run-name', prepared.name], check=True, cwd=ROOT)
        if not chosen['completed'] or not (training_run / 'inference_config.json').exists():
            with training_gpu(ROOT):
                report = train_candidate(prepared, training_run, chosen['completed'])
        else:
            report = train_candidate(prepared, training_run, True)
        pointer = pointer_for(training_run, chosen['source_sha256'])
        if current != pointer:
            if current:
                raise ValueError('candidate_requires_continuous_comparison')
            publish(pointer, None, {'accepted': True, 'action': 'initial_calibrated_model'}, ROOT)
    emit('automatic_learning_ready', run_name=training_run.name, reused_completed_run=chosen['completed'],
         optimizer_updates=report['optimizer_updates'], trained_tokens=report['trained_tokens'])


def main():
    parser = argparse.ArgumentParser(description='一条命令自动准备数据、训练、评测并选用本地模型')
    parser.add_argument('--source-run', type=Path, default=None)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    try:
        if args.source_run is None:
            current_sources = ROOT / 'data/current_training_sources.json'
            if current_sources.exists():
                pointer = json.loads(current_sources.read_text())
                args.source_run = (ROOT / 'data' / pointer['run_name']).resolve()
                if (args.source_run.parent != (ROOT / 'data').resolve()
                        or digest_file(args.source_run / 'engineering_candidates.jsonl') != pointer['source_artifact_sha256']):
                    raise ValueError('training_source_pointer_mismatch')
            else:
                args.source_run = ROOT / 'data/sanitized_v3'
        run(args.source_run, args.status)
    except Exception as exc:
        emit('automatic_learning_failed', error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
