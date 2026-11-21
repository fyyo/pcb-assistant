"""Interrupt only this workspace's attachment worker processes; preserve cache files."""
import argparse
import json
import os
from pathlib import Path
import signal
import time

ROOT = Path(__file__).resolve().parents[2]


def find_workers():
    found = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid() or (path / 'cwd').resolve() != ROOT:
                continue
            args = (path / 'cmdline').read_bytes().decode().split('\0')
            if 'pcb_assistant.ingestion.attachments' in args[1:4]:
                found.append(int(path.name))
        except (OSError, UnicodeError):
            continue
    return found


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    workers = find_workers()
    print(json.dumps({'matching_attachment_processes': len(workers), 'stop_requested': args.stop}), flush=True)
    if args.stop:
        for pid in workers:
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and find_workers():
            time.sleep(.2)
        remaining = find_workers()
        print(json.dumps({'attachment_processes_remaining': len(remaining), 'cache_files_untouched': True}), flush=True)
        raise SystemExit(bool(remaining))
