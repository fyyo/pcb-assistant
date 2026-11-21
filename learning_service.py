"""Start the persistent local learning worker; safe to invoke repeatedly."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time

from continuous_common import ROOT, lock, read, setup, status
from learning_common import dump_json, emit


def worker_running():
    try:
        with lock('learning_worker'):
            return False
    except BlockingIOError:
        return True


def launch(command, log_name):
    with (ROOT / 'data/continuous' / log_name).open('ab') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
    return process


def start():
    with lock('learning_service_start'):
        if not worker_running():
            process = launch([str(ROOT / '.venv/bin/python'), str(ROOT / 'continuous_learning.py'), 'watch'], 'worker_service.log')
            dump_json(ROOT / 'data/continuous/worker_process.json', {'pid': process.pid})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not worker_running():
                if process.poll() is not None:
                    raise RuntimeError('worker_start_failed')
                time.sleep(.1)
        emit('local_learning_services_started', worker_running=worker_running())


def restart_worker():
    record = read(ROOT / 'data/continuous/worker_process.json', {})
    pid = record.get('pid')
    if pid and Path('/proc', str(pid)).exists():
        process = Path('/proc', str(pid))
        args = (process / 'cmdline').read_bytes().decode().split('\0')
        if ((process / 'cwd').resolve() != ROOT or 'watch' not in args
                or str(ROOT / 'continuous_learning.py') not in args or os.getpgid(pid) != pid):
            raise ValueError('worker_identity_mismatch')
        os.killpg(pid, signal.SIGINT)
        deadline = time.monotonic() + 15
        while worker_running() and time.monotonic() < deadline:
            time.sleep(.1)
        if worker_running():
            raise RuntimeError('worker_still_finishing')
    start()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('start', 'status', 'restart-worker'))
    args = parser.parse_args()
    os.umask(0o077)
    setup()
    try:
        if args.command == 'start':
            start()
        elif args.command == 'restart-worker':
            restart_worker()
        else:
            emit('local_learning_services_status', **status())
    except Exception as exc:
        emit('local_learning_services_failed', error_type=type(exc).__name__)
        raise SystemExit(1)
