"""Re-apply residual person-name redaction to existing sanitized mail runs."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mail_privacy import redact_residual_person_names


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"record is not an object: {path}")
                rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def process_run(source: Path, output: Path) -> dict:
    source = source.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    if not source.is_dir():
        raise FileNotFoundError(f"source run not found: {source}")
    source_report = read_json(source / "report.json") if (source / "report.json").is_file() else {}
    for name, expected in (source_report.get("artifacts") or {}).items():
        path = source / name
        if path.is_file() and isinstance(expected, dict) and expected.get("sha256"):
            if digest_file(path) != expected["sha256"]:
                raise ValueError(f"source artifact checksum mismatch: {source.name}/{name}")
    output.mkdir(parents=True)
    stats = Counter()
    for name in ("candidates.jsonl", "engineering_candidates.jsonl"):
        path = source / name
        if not path.is_file():
            continue
        rows = []
        for row in read_jsonl(path):
            changed = False
            for field in ("text", "subject"):
                value = row.get(field)
                if not isinstance(value, str):
                    continue
                field_changed = False
                passes = 0
                while passes < 4:
                    counts = {}
                    updated = redact_residual_person_names(value, counts)
                    if updated == value:
                        break
                    field_changed = True
                    passes += 1
                    stats["residual_person_names"] += counts.get("residual_person_names", 0)
                    value = updated
                if field_changed:
                    changed = True
                    stats["fields_changed"] += 1
                    stats[f"{field}_changed"] += 1
                    stats["max_passes"] = max(stats["max_passes"], passes)
                row[field] = value
            stats["rows_scanned"] += 1
            if changed:
                stats["rows_changed"] += 1
            rows.append(row)
        write_jsonl(output / name, rows)
        stats[f"{name}_rows"] = len(rows)
    for name in ("quarantine_manifest.jsonl", "message_status.jsonl"):
        if (source / name).is_file():
            shutil.copy2(source / name, output / name)
    report = dict(source_report)
    report["status"] = "completed_local_candidates_require_review"
    report["rewash"] = {
        "rule": "mail_privacy.redact_residual_person_names",
        "source_run": source.name,
        "output_run": output.name,
        "rows_scanned": stats["rows_scanned"],
        "rows_changed": stats["rows_changed"],
        "fields_changed": stats["fields_changed"],
        "text_changed": stats["text_changed"],
        "subject_changed": stats["subject_changed"],
        "residual_person_names": stats["residual_person_names"],
        "max_passes": stats["max_passes"],
    }
    artifacts = {}
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name not in ("report.json", "progress.json", "artifact_manifest.json"):
            artifacts[path.name] = {"sha256": digest_file(path), "size_bytes": path.stat().st_size}
    report["artifacts"] = artifacts
    write_json(output / "report.json", report)
    write_json(output / "progress.json", {
        "status": "completed",
        "stats": dict(stats),
    })
    write_json(output / "RELEASE_BLOCKED.json", report.get("release") or {
        "training_allowed": False,
        "published": False,
        "external_mail_transmission": False,
        "reason": "本地候选邮件仅供目录检索与人工复核，未获准训练或发布。",
    })
    write_json(output / "artifact_manifest.json", {
        "schema_version": 1,
        "artifacts": artifacts,
    })
    return {"source": source.name, "output": output.name, **dict(stats)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True,
                        help="SOURCE=OUTPUT, repeat for each sanitized run")
    parser.add_argument("--report", type=Path,
                        default=ROOT / "docs" / "MAIL_REWASH_20261102.json")
    args = parser.parse_args()
    runs = []
    for item in args.run:
        source, separator, output = item.partition("=")
        if not separator or not source or not output:
            parser.error("--run must be SOURCE=OUTPUT")
        runs.append((source, output))
    results = []
    for source, output in runs:
        results.append(process_run(Path(source), Path(output)))
    write_json(args.report, {
        "status": "completed",
        "rule": "mail_privacy.redact_residual_person_names",
        "scope": "existing sanitized candidates; original PSTs were not reopened",
        "runs": results,
        "totals": {
            "rows_scanned": sum(item["rows_scanned"] for item in results),
            "rows_changed": sum(item["rows_changed"] for item in results),
            "fields_changed": sum(item["fields_changed"] for item in results),
            "residual_person_names": sum(item["residual_person_names"] for item in results),
        },
    })
    print(json.dumps({"event": "rewash_completed", "runs": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
