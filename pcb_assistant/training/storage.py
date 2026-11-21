"""Local continual-learning storage, locks and immutable model provenance."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import time

try:
    import fcntl
except ImportError:  # Native Windows has no fcntl.
    fcntl = None
try:
    import msvcrt
except ImportError:  # Unix uses fcntl instead.
    msvcrt = None

from pcb_assistant.training.common import ROOT, digest_file, dump_json

CAPABILITIES = ['缺陷诊断', '材料与叠构', '工艺权衡', '试验设计', '结果验证', '适用边界']


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def setup(root=ROOT):
    for name in ('data/continuous', 'data/continuous/records', 'data/continuous/cycles',
                 'data/continuous/registry', 'data/learning_inbox', '.private/learning_pending'):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)


def _acquire_windows_lock(stream, blocking):
    # msvcrt locks a byte range. Keep one byte in the lock file and lock byte 0.
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b'\0')
        stream.flush()
    while True:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(errno.EAGAIN, 'file is locked') from exc
            time.sleep(0.1)


@contextmanager
def lock(name, root=ROOT, blocking=False, shared=False):
    setup(root)
    path = root / '.private' / (name + '.lock')
    with path.open('a+b') as stream:
        if os.name == 'nt':
            _acquire_windows_lock(stream, blocking)
            try:
                yield stream
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(stream, mode | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield stream
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def gpu_requested(root=ROOT):
    try:
        with lock('learning_gpu_request', root):
            return False
    except BlockingIOError:
        return True


@contextmanager
def training_gpu(root=ROOT):
    # The local chat process observes this lease, unloads its model, and releases VRAM.
    # Kernel locks disappear on process exit, so a crash leaves no stale flag.
    with lock('learning_gpu_request', root, blocking=True):
        with lock('learning_gpu', root, blocking=True):
            yield


def validate_case(data):
    if not isinstance(data, dict) or set(data) - {'case_id', 'kind', 'context', 'conclusion', 'evidence', 'capability'}:
        raise ValueError('invalid_case_fields')
    kind = data.get('kind')
    if kind not in ('note', 'correction', 'outcome'):
        raise ValueError('invalid_case_kind')
    result = {'kind': kind}
    for name, minimum, maximum in [('case_id', 2, 120), ('context', 20, 4000),
                                   ('conclusion', 10, 4000), ('evidence', 0 if kind == 'note' else 20, 6000)]:
        value = data.get(name, '')
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise ValueError('invalid_case_' + name)
        result[name] = value.strip()
    result['capability'] = data.get('capability', CAPABILITIES[0])
    if result['capability'] not in CAPABILITIES:
        raise ValueError('invalid_capability')
    return result


def submit_case(data, root=ROOT):
    result = validate_case(data)
    setup(root)
    ident = fingerprint(result)
    path = root / '.private/learning_pending' / (ident + '.json')
    # This is an intake record, not a training example; sanitation occurs offline.
    if not path.exists():
        dump_json(path, {'case': result, 'submitted_at': time.time()})
    return {'status': 'queued_for_local_processing', 'id': ident[:12]}


def pointer_for(run, source_hash=None):
    report = read(run / 'report.json')
    if report['status'] != 'completed' or report['optimizer_updates'] <= 0:
        raise ValueError('model_incomplete')
    selected = report['selected_adapter']
    for name, checksum in report['adapter_artifacts'][selected].items():
        if Path(name).name != name or digest_file(run / selected / name) != checksum:
            raise ValueError('adapter_checksum_mismatch')
    inference = read(run / 'inference_config.json')
    if inference['adapter_sha256'] != report['adapter_artifacts'][selected]['adapter_model.safetensors']:
        raise ValueError('inference_adapter_mismatch')
    return {'run_name': run.name, 'report_sha256': digest_file(run / 'report.json'),
            'selected_adapter': selected, 'inference_config_sha256': digest_file(run / 'inference_config.json'),
            'source_artifact_sha256': source_hash}


def verify_pointer(pointer, root=ROOT):
    run = (root / 'data' / pointer['run_name']).resolve()
    if run.parent != (root / 'data').resolve():
        raise ValueError('invalid_model_path')
    actual = pointer_for(run, pointer.get('source_artifact_sha256'))
    if actual != pointer:
        raise ValueError('model_pointer_mismatch')
    return run


def publish(pointer, expected, evaluation, root=ROOT):
    with lock('learning_publication', root, blocking=True):
        current = read(root / 'data/current_model.json')
        if current != expected:
            raise ValueError('incumbent_changed')
        verify_pointer(pointer, root)
        if not evaluation.get('accepted'):
            raise ValueError('candidate_not_accepted')
        event = {'created_at': time.time(), 'previous': current, 'selected': pointer, 'evaluation': evaluation}
        # Journal before commit; rollback selects only journaled, verified artifacts.
        dump_json(root / 'data/continuous/registry' / (str(time.time_ns()) + '.json'), event)
        dump_json(root / 'data/current_model.json', pointer)


def rollback(root=ROOT):
    with lock('learning_cycle', root):
        current = read(root / 'data/current_model.json')
        for path in sorted((root / 'data/continuous/registry').glob('*.json'), reverse=True):
            event = read(path)
            if event['selected'] == current and event.get('previous') and event['previous'] != current:
                publish(event['previous'], current, {'accepted': True, 'action': 'rollback'}, root)
                return event['previous']['run_name']
        raise ValueError('no_previous_version')


def status(root=ROOT):
    state = read(root / 'data/continuous/status.json', {'state': 'not_started'})
    current = read(root / 'data/current_model.json', {})
    try:
        with lock('learning_worker', root):
            alive = False
    except BlockingIOError:
        alive = time.time() - state.get('heartbeat_at', 0) < 180
    coverage = read(root / 'data/continuous/capability_counts.json', {})
    return {**state, 'current_model': current.get('run_name'),
            'pending_submissions': len(list((root / '.private/learning_pending').glob('*.json'))),
            'worker_alive': alive,
            'capabilities': {name: f"已记录 {coverage.get(name, 0)} 个结果案例；尚未通过真实独立案例评定" for name in CAPABILITIES},
            'chief_engineer_certified': False}
