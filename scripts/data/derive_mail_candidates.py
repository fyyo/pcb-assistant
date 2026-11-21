#!/usr/bin/env python3
"""Derive sanitized catalog candidates for archive mail missing from a base run.

The local full archive stays read-only and never leaves the machine. Only the
redacted candidate JSONL produced here can be used by the Catalog builder.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pcb_assistant.privacy.mail import (  # noqa: E402
    EMAIL,
    IdentityIndex,
    IdentityMatcher,
    Sanitizer,
    technical_topics,
)
from pcb_assistant.ingestion.mail_status import MESSAGE_STATUS_SCHEMA_VERSION, validate_message_status_rows  # noqa: E402


SCHEMA_VERSION = 1
MAX_BODY_CHARS = 250_000
CANDIDATE_FIELDS = frozenset({
    "id", "subject", "text", "topics", "excluded_attachment_count",
    "review_required", "training_allowed", "source_kind",
})
RELEASE = {
    "training_allowed": False,
    "published": False,
    "external_mail_transmission": False,
    "reason": "Automated rules and NER can miss or over-redact identities; local review is required.",
}
HEADER_LINE = re.compile(
    r"(?im)^\s*[> ]*(?:from|to|cc|bcc|\u53d1\u4ef6\u4eba|\u5bc4\u4ef6\u8005|"
    r"\u5bc4\u4ef6\u4eba|\u6536\u4ef6\u4eba|\u6536\u4ef6\u8005|\u6284\u9001|"
    r"\u526f\u672c)\s*[:\uff1a](.+)$"
)
RUN_NAME = re.compile(r"[a-zA-Z0-9_-]+")


def emit(event, **details):
    print(json.dumps({"event": event, **details}, ensure_ascii=False), flush=True)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def connect_archive(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_jsonl(path):
    records, lines = [], []
    with Path(path).open(encoding="utf-8-sig") as stream:
        for raw in stream:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or set(record) != CANDIDATE_FIELDS:
                raise ValueError("unexpected_candidate_schema")
            records.append(record)
            lines.append(line)
    return records, lines


def load_base(paths):
    records, lines, engineering_records, engineering_lines = [], [], [], []
    for path in paths:
        source_records, source_lines = read_jsonl(path)
        records.extend(source_records)
        lines.extend(source_lines)
        sibling = Path(path).with_name("engineering_candidates.jsonl")
        if sibling.is_file():
            subset_records, subset_lines = read_jsonl(sibling)
            engineering_records.extend(subset_records)
            engineering_lines.extend(subset_lines)
        else:
            for record, line in zip(source_records, source_lines):
                if record["topics"]:
                    engineering_records.append(record)
                    engineering_lines.append(line)
    return records, lines, engineering_records, engineering_lines


def build_identity_index(connection, sensitive_terms=None):
    index = IdentityIndex()
    for row in connection.execute(
        "select sender_name, sender_address, to_json, cc_json, bcc_json, body_text from messages"
    ):
        index.contact(row["sender_name"] or "")
        index.contact(row["sender_address"] or "")
        for column in ("to_json", "cc_json", "bcc_json"):
            try:
                values = json.loads(row[column] or "[]")
            except (TypeError, ValueError):
                values = []
            for value in values if isinstance(values, list) else []:
                if isinstance(value, dict):
                    index.contact(f"{value.get('name', '')} <{value.get('address', '')}>")
                elif isinstance(value, str):
                    index.contact(value)
        body = row["body_text"] or ""
        for match in HEADER_LINE.finditer(body):
            index.contact(match.group(1))
        for match in EMAIL.finditer(body):
            index.contact(match.group(0))
    if sensitive_terms and Path(sensitive_terms).is_file():
        for line in Path(sensitive_terms).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                index.add(line)
    return index


def build_sanitizer(terms):
    import spacy

    disabled = ["tagger", "parser", "attribute_ruler", "lemmatizer", "senter"]
    nlp_zh = spacy.load("zh_core_web_sm", disable=disabled)
    nlp_en = spacy.load("en_core_web_sm", disable=disabled)
    sanitizer = Sanitizer(IdentityMatcher(terms), nlp_zh, nlp_en)
    models = {
        "zh_core_web_sm": nlp_zh.meta["version"],
        "en_core_web_sm": nlp_en.meta["version"],
        "spacy": spacy.__version__,
    }
    return sanitizer, models


def select_missing(connection, base_ids):
    best = {}
    order = []
    for row in connection.execute(
        "select message_pk, stable_id, subject, body_latest, body_text, sent_at, "
        "attachment_count from messages order by message_pk"
    ):
        ident = row["stable_id"]
        if ident in base_ids:
            continue
        latest = row["body_latest"] or ""
        body = latest if latest.strip() else (row["body_text"] or "")
        score = (bool(body.strip()), len(body), row["sent_at"] or "")
        if ident not in best:
            order.append(ident)
            best[ident] = None
        current = best[ident]
        if current is None or score > current["score"]:
            best[ident] = {
                "id": ident,
                "subject": row["subject"] or "",
                "body": body,
                "attachment_count": row["attachment_count"] or 0,
                "score": score,
            }
    return [best[ident] for ident in order]


def derive_new(messages, sanitizer, max_body_chars=MAX_BODY_CHARS):
    stats = collections.Counter({
        "messages_visited": 0,
        "unique_candidates": 0,
        "duplicate_candidates": 0,
        "no_text_body": 0,
        "empty_after_redaction": 0,
        "residual_quarantine": 0,
        "oversize_body": 0,
        "processing_error": 0,
    })
    records, engineering = [], []
    rejects, status_rows = [], []
    residuals = collections.Counter()
    topics_count = collections.Counter()
    redactions = collections.Counter()
    retained = {}

    def reject(sequence, ident, reason):
        stats[reason] += 1
        rejects.append({"id": ident, "reason": reason})
        status_rows.append({"id": ident, "status": reason, "duplicate_of": None})

    for sequence, message in enumerate(messages, 1):
        ident = message["id"]
        stats["messages_visited"] += 1
        body, subject = message["body"], message["subject"]
        if not body.strip():
            reject(sequence, ident, "no_text_body")
            continue
        if len(body) + len(subject) > max_body_chars:
            reject(sequence, ident, "oversize_body")
            continue
        try:
            sanitizer.counts.clear()
            clean_subject = sanitizer.clean(subject)
            clean_text = sanitizer.clean(body)
            found = sanitizer.residuals(clean_subject + "\n" + clean_text)
            if found:
                stats["residual_quarantine"] += 1
                rejects.append({"id": ident, "reason": "residual_quarantine"})
                status_rows.append({
                    "id": ident, "status": "residual_quarantine", "duplicate_of": None,
                })
                residuals.update(found)
                continue
            if not clean_text.strip():
                reject(sequence, ident, "empty_after_redaction")
                continue
            topics = technical_topics(clean_subject + "\n" + clean_text)
            fingerprint = hashlib.sha256(
                (clean_subject + "\n" + clean_text).encode("utf-8")
            ).digest()
            redactions.update(sanitizer.counts)
            if fingerprint in retained:
                stats["duplicate_candidates"] += 1
                status_rows.append({
                    "id": ident, "status": "duplicate", "duplicate_of": retained[fingerprint],
                })
                continue
            retained[fingerprint] = ident
            record = {
                "id": ident,
                "subject": clean_subject,
                "text": clean_text,
                "topics": topics,
                "excluded_attachment_count": message["attachment_count"],
                "review_required": True,
                "training_allowed": False,
                "source_kind": "untrusted_email",
            }
            records.append(record)
            stats["unique_candidates"] += 1
            status_rows.append({"id": ident, "status": "candidate", "duplicate_of": None})
            if topics:
                engineering.append(record)
                topics_count.update(topics)
        except Exception:
            reject(sequence, ident, "processing_error")

    status_counts = validate_message_status_rows(status_rows, stats)
    return {
        "records": records,
        "engineering": engineering,
        "rejects": rejects,
        "status_rows": status_rows,
        "status_counts": status_counts,
        "stats": dict(stats),
        "residuals": dict(residuals),
        "topic_counts": dict(topics_count),
        "redactions": dict(redactions),
    }


def write_jsonl(path, lines):
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        for line in lines:
            stream.write(line + "\n")


def audit_records(records, sanitizer):
    audit = collections.Counter()
    for record in records:
        if set(record) != CANDIDATE_FIELDS:
            raise ValueError("unexpected_output_schema")
        if record["training_allowed"] is not False or record["review_required"] is not True:
            raise ValueError("unsafe_release_flag")
        audit.update(sanitizer.residuals(record["subject"] + "\n" + record["text"]))
        audit["records_read_back"] += 1
    if any(count for name, count in audit.items() if name != "records_read_back"):
        raise ValueError("read_back_audit_failed")
    return dict(audit)


def run(archive_path, base_paths, output, *, sanitizer=None, models=None,
        sensitive_terms=None, limit_missing=None, dry_run=False, new_only=False):
    output = Path(output)
    if output.exists():
        raise ValueError("output_already_exists")
    archive_path = Path(archive_path)
    before = archive_path.stat()
    started = time.monotonic()
    connection = connect_archive(archive_path)
    try:
        base_records, base_lines, base_engineering, base_engineering_lines = load_base(base_paths)
        base_ids = {record["id"] for record in base_records}
        archive_messages = connection.execute("select count(*) from messages").fetchone()[0]
        archive_unique = connection.execute(
            "select count(distinct stable_id) from messages"
        ).fetchone()[0]
        messages = select_missing(connection, base_ids)
        if limit_missing:
            messages = messages[:limit_missing]
        scan = {
            "archive_messages": archive_messages,
            "archive_unique_ids": archive_unique,
            "base_candidates": len(base_records),
            "base_engineering_candidates": len(base_engineering),
            "missing_ids": len(messages),
            "missing_with_body": sum(1 for message in messages if message["body"].strip()),
        }
        if dry_run:
            return {"status": "dry_run", **scan}
        identity_terms = getattr(sanitizer, "identity_terms", None)
        if sanitizer is None:
            index = build_identity_index(connection, sensitive_terms)
            identity_terms = len(index.terms)
            sanitizer, models = build_sanitizer(index.terms)
        derived = derive_new(messages, sanitizer)
    finally:
        connection.close()

    after = archive_path.stat()
    unchanged = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    if not unchanged:
        raise ValueError("source_changed_during_processing")

    output.mkdir(parents=True, exist_ok=False)
    new_lines = [json.dumps(record, ensure_ascii=False) for record in derived["records"]]
    new_engineering_lines = [
        json.dumps(record, ensure_ascii=False) for record in derived["engineering"]
    ]
    artifacts = {
        "candidates.jsonl": new_lines if new_only else base_lines + new_lines,
        "engineering_candidates.jsonl": (
            new_engineering_lines if new_only
            else base_engineering_lines + new_engineering_lines
        ),
        "quarantine_manifest.jsonl": [
            json.dumps(reject, ensure_ascii=False) for reject in derived["rejects"]
        ],
        "message_status.jsonl": [
            json.dumps(row, ensure_ascii=False) for row in derived["status_rows"]
        ],
    }
    for name, lines in artifacts.items():
        write_jsonl(output / name, lines)

    audit = audit_records(derived["records"], sanitizer)
    artifact_metadata = {
        name: {"sha256": hash_file(output / name), "size_bytes": (output / name).stat().st_size}
        for name in artifacts
    }
    report = {
        "status": "completed_local_candidates_require_review",
        "schema_version": SCHEMA_VERSION,
        "derivation": "mail_archive_to_catalog_candidates",
        "output_mode": "new_only" if new_only else "merged_base_plus_new",
        "source_opened_read_only": True,
        "source_metadata_unchanged": unchanged,
        "source_size_bytes": before.st_size,
        "scan": scan,
        "counts": derived["stats"],
        "message_status": {
            "artifact": "message_status.jsonl",
            "schema_version": MESSAGE_STATUS_SCHEMA_VERSION,
            "rows": len(derived["status_rows"]),
            "counts": derived["status_counts"],
        },
        "identity_terms_in_memory": identity_terms,
        "rule_match_operations": derived["redactions"],
        "quarantined_residual_detections": derived["residuals"],
        "output_audit": audit,
        "topic_counts": derived["topic_counts"],
        "elapsed_seconds": round(time.monotonic() - started),
        "models": models or {"injected": True},
        "base_sources": [str(path) for path in base_paths],
        "artifacts": artifact_metadata,
        "release": RELEASE,
        "limits": [
            "Only anonymous mail IDs present in the archive but absent from the base run are processed.",
            "Missing mail prefers body_latest and falls back to body_text; attachment bodies are not duplicated.",
            "Duplicate detection is local to this batch; cross-source duplicates stay as Catalog duplicate groups.",
            "This output is still an unreviewed candidate set and must not be used for training or release.",
        ],
    }
    write_json(output / "report.json", report)
    write_json(output / "progress.json", {
        "status": "completed",
        "counts": derived["stats"],
        "elapsed_seconds": report["elapsed_seconds"],
    })
    write_json(output / "RELEASE_BLOCKED.json", RELEASE)
    emit("completed", **derived["stats"], elapsed_seconds=report["elapsed_seconds"])
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Derive redacted Catalog candidates for archive mail missing from a base run."
    )
    parser.add_argument("--archive", default="data/mail_archive_v1/mail_archive.sqlite3")
    parser.add_argument("--base-candidate", action="append", required=True)
    parser.add_argument("--run-name", default="sanitized_v10")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sensitive-terms", type=Path, default=".private/sensitive_terms.txt")
    parser.add_argument("--limit-missing", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Only write the newly derived candidates so existing source IDs stay stable.",
    )
    args = parser.parse_args()
    if not RUN_NAME.fullmatch(args.run_name):
        parser.error("invalid run name")
    output = args.output or ROOT / "data" / args.run_name
    try:
        report = run(
            args.archive,
            args.base_candidate,
            output,
            sensitive_terms=args.sensitive_terms,
            limit_missing=args.limit_missing,
            dry_run=args.dry_run,
            new_only=args.new_only,
        )
    except KeyboardInterrupt:
        emit("interrupted_outputs_incomplete")
        return 130
    except Exception as exc:
        emit("failed_outputs_incomplete", error_type=type(exc).__name__)
        return 1
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
