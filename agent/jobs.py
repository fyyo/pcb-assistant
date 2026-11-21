"""Local persistent background jobs for longer Agent evaluations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid
import ctypes
from datetime import datetime, timezone
from typing import Any

from catalog_common import CatalogError

from .evaluation import evaluate_file, render_summary, write_report
from .models import canonical_json


JOB_SCHEMA = "lamination-agent-job-v1"
JOB_STATES = {"queued", "running", "completed", "failed", "cancelled"}
DEFAULT_JOB_DIR = Path(__file__).resolve().parents[1] / ".private" / "agent_jobs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _job_dir(root: Path | str | None = None) -> Path:
    configured = os.environ.get("PCB_AGENT_JOB_DIR")
    return Path(root or configured or DEFAULT_JOB_DIR).expanduser().resolve()


def _record_path(job_id: str, root: Path | str | None = None) -> Path:
    if not job_id.startswith("AJOB_") or not all(c.isalnum() or c == "_" for c in job_id):
        raise CatalogError("invalid_job_id")
    return _job_dir(root) / f"{job_id}.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("job_record_unavailable") from exc
    if not isinstance(value, dict) or value.get("schema") != JOB_SCHEMA:
        raise CatalogError("invalid_job_record")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(canonical_json(value, pretty=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _refresh(record: dict[str, Any], root: Path | str | None = None) -> dict[str, Any]:
    if record.get("status") in {"queued", "running"}:
        pid = record.get("worker_pid", record.get("pid"))
        if isinstance(pid, int) and not _alive(pid):
            record.update({"status": "failed", "updated_at": _now(), "error": "worker_exited_without_report"})
            _write(_record_path(record["job_id"], root), record)
    return record


def start_evaluation(
    cases: Path | str,
    *,
    catalog: Path | str,
    top_k: int = 3,
    check_determinism: bool = False,
    include_answers: bool = False,
    root: Path | str | None = None,
    public_pack: Path | str | None = None,
) -> dict[str, Any]:
    suite_path = Path(cases).expanduser().resolve()
    catalog_path = Path(catalog).expanduser().resolve()
    from .evaluation import load_suite

    suite = load_suite(suite_path)
    pack_options = {}
    if public_pack is not None:
        from .public_pack import load_pack
        pack_path = Path(public_pack).expanduser().resolve()
        load_pack(pack_path)
        pack_options["public_pack"] = str(pack_path)
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise CatalogError("invalid_top_k")
    directory = _job_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    job_id = "AJOB_" + uuid.uuid4().hex[:16].upper()
    record = {
        "schema": JOB_SCHEMA,
        "job_id": job_id,
        "kind": "evaluation",
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "progress": {"completed": 0, "total": len(suite), "case_id": None},
        "options": {
            "cases": str(suite_path),
            "catalog": str(catalog_path),
            "top_k": top_k,
            "check_determinism": check_determinism,
            "include_answers": include_answers,
            **pack_options,
        },
        "report_path": str(directory / f"{job_id}.report.json"),
        "log_path": str(directory / f"{job_id}.log"),
    }
    path = _record_path(job_id, directory)
    _write(path, record)
    log = open(record["log_path"], "ab")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "agent.cli", "_run-eval-job", job_id, "--job-dir", str(directory)],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    except OSError as exc:
        log.close()
        record.update({"status": "failed", "updated_at": _now(), "error": "worker_start_failed"})
        _write(path, record)
        raise CatalogError("job_start_failed") from exc
    finally:
        if not log.closed:
            log.close()
    current = _read(path)
    current["pid"] = process.pid
    if current.get("status") == "queued":
        current["status"] = "running"
    current["updated_at"] = _now()
    _write(path, current)
    return {key: value for key, value in current.items() if key not in {"options", "log_path"}}


def list_jobs(*, root: Path | str | None = None) -> list[dict[str, Any]]:
    directory = _job_dir(root)
    if not directory.exists():
        return []
    records = []
    for path in directory.glob("AJOB_*.json"):
        try:
            record = _refresh(_read(path), directory)
        except CatalogError:
            continue
        records.append({key: value for key, value in record.items() if key not in {"options", "log_path"}})
    return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)


def get_job(job_id: str, *, root: Path | str | None = None) -> dict[str, Any]:
    record = _refresh(_read(_record_path(job_id, root)), root)
    return {key: value for key, value in record.items() if key not in {"options", "log_path"}}


def get_result(job_id: str, *, root: Path | str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    record = _refresh(_read(_record_path(job_id, root)), root)
    if record.get("status") != "completed":
        raise CatalogError("job_not_completed")
    report_path = Path(record["report_path"])
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("job_report_unavailable") from exc
    return {key: value for key, value in record.items() if key not in {"options", "log_path"}}, report


def stop_job(job_id: str, *, root: Path | str | None = None) -> dict[str, Any]:
    path = _record_path(job_id, root)
    record = _read(path)
    if record.get("status") not in {"queued", "running"}:
        raise CatalogError("job_not_running")
    pid = record.get("worker_pid", record.get("pid"))
    if isinstance(pid, int) and _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise CatalogError("job_stop_failed") from exc
    record.update({"status": "cancelled", "updated_at": _now()})
    _write(path, record)
    return {key: value for key, value in record.items() if key not in {"options", "log_path"}}


def run_worker(job_id: str, *, root: Path | str | None = None) -> int:
    path = _record_path(job_id, root)
    record = _read(path)
    if record.get("status") not in {"queued", "running"}:
        return 2
    record.update({"status": "running", "updated_at": _now(), "worker_pid": os.getpid()})
    _write(path, record)

    def progress(completed: int, total: int, case_id: str) -> None:
        current = _read(path)
        if current.get("status") == "cancelled":
            raise InterruptedError("evaluation_cancelled")
        current.update({"status": "running", "updated_at": _now(), "progress": {"completed": completed, "total": total, "case_id": case_id}})
        _write(path, current)

    try:
        options = record["options"]
        report = evaluate_file(
            options["cases"], catalog=Path(options["catalog"]), top_k=options["top_k"],
            check_determinism=options["check_determinism"], include_answers=options["include_answers"],
            progress_callback=progress,
            **({"public_pack": options["public_pack"]} if options.get("public_pack") else {}),
        )
        write_report(report, record["report_path"])
        current = _read(path)
        if current.get("status") == "cancelled":
            return 2
        current.update({"status": "completed", "updated_at": _now(), "progress": {"completed": report["summary"]["case_count"], "total": report["summary"]["case_count"], "case_id": None}})
        _write(path, current)
        print(render_summary(report))
        return 0
    except InterruptedError:
        return 2
    except Exception as exc:
        current = _read(path)
        if current.get("status") == "cancelled":
            return 2
        current.update({"status": "failed", "updated_at": _now(), "error": getattr(exc, "code", "evaluation_failed")})
        _write(path, current)
        print(f"评测任务失败：{current['error']}")
        return 1

