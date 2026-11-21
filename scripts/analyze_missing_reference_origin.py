"""Read-only origin analysis for unresolved parent-mail references.

This module reconciles the existing missing-reference audit with the sanitized
mail quarantine manifest and attachment manifests. It never opens the PST,
never reads candidate prose (subject/text/title), and never mutates source or
catalog assets.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mail_status import MessageStatusError, validate_message_status_rows


DEFAULT_AUDIT = "docs/phase3_missing_reference_audit_20261021.json"
DEFAULT_OUTPUT = "docs/phase3_missing_reference_origin_20261021.json"
DEFAULT_QUARANTINE = "data/sanitized_v3/quarantine_manifest.jsonl"
DEFAULT_MESSAGE_STATUS = "data/sanitized_v3/message_status.jsonl"
DEFAULT_OCCURRENCES = "data/attachments_v2/occurrences.jsonl"
DEFAULT_DOCUMENTS = "data/attachments_v2/documents.jsonl"
DEFAULT_CATALOG = "data/phase3_catalog_v1/catalog.sqlite3"
DEFAULT_SANITIZED_REPORT = "data/sanitized_v3/report.json"
DEFAULT_ATTACHMENTS_REPORT = "data/attachments_v2/report.json"
ORIGINAL_MAIL = "19924784-20240420.pst"
DEFAULT_PROFILE = "v1"

PROFILE_DEFAULTS = {
    "v1": {
        "audit": DEFAULT_AUDIT,
        "output": DEFAULT_OUTPUT,
        "quarantine": (DEFAULT_QUARANTINE,),
        "message_status": (DEFAULT_MESSAGE_STATUS,),
        "occurrences": DEFAULT_OCCURRENCES,
        "documents": DEFAULT_DOCUMENTS,
        "catalog": DEFAULT_CATALOG,
        "sanitized_report": (DEFAULT_SANITIZED_REPORT,),
        "attachments_report": DEFAULT_ATTACHMENTS_REPORT,
    },
    "v3": {
        "audit": "docs/phase3_missing_reference_audit_20261105.json",
        "output": "docs/phase3_missing_reference_origin_20261105.json",
        "quarantine": (
            "data/sanitized_v8/quarantine_manifest.jsonl",
            "data/sanitized_v9/quarantine_manifest.jsonl",
        ),
        "message_status": ("data/sanitized_v9/message_status.jsonl",),
        "occurrences": DEFAULT_OCCURRENCES,
        "documents": DEFAULT_DOCUMENTS,
        "catalog": "data/phase3_catalog_v3/catalog.sqlite3",
        "sanitized_report": (
            "data/sanitized_v8/report.json",
            "data/sanitized_v9/report.json",
        ),
        "attachments_report": DEFAULT_ATTACHMENTS_REPORT,
    },
}

TARGET_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
CLASSIFICATIONS = (
    "explicit_quarantine_no_text_body",
    "explicit_quarantine_empty_after_redaction",
    "explicit_quarantine_residual",
    "explicit_quarantine_other",
    "explicit_duplicate_of_retained_candidate",
    "attachment_shared_with_retained_mail",
    "not_explained_by_retained_manifests",
)
QUARANTINE_CLASSIFICATIONS = {
    "no_text_body": "explicit_quarantine_no_text_body",
    "empty_after_redaction": "explicit_quarantine_empty_after_redaction",
    "residual_quarantine": "explicit_quarantine_residual",
}
CLASSIFICATION_DEFINITIONS = {
    "explicit_quarantine_no_text_body": (
        "The exact parent ID has a quarantine record stating no_text_body."
    ),
    "explicit_quarantine_empty_after_redaction": (
        "The exact parent ID has a quarantine record stating empty_after_redaction."
    ),
    "explicit_quarantine_residual": (
        "The exact parent ID has a quarantine record stating residual_quarantine."
    ),
    "explicit_quarantine_other": (
        "The exact parent ID has an explicit quarantine record with another reason."
    ),
    "explicit_duplicate_of_retained_candidate": (
        "The exact parent ID has a duplicate status linked to a candidate retained in "
        "the Catalog. This proves the sanitizer's deduplication link, but does not "
        "restore the missing parent mail or its prose."
    ),
    "attachment_shared_with_retained_mail": (
        "No quarantine record was found, but at least one attachment document attached "
        "to the missing parent is also attached to a retained catalog mail. This is "
        "partial evidence only and does not prove body deduplication."
    ),
    "not_explained_by_retained_manifests": (
        "No quarantine record and no attachment document shared with a retained catalog "
        "mail were found. Attachment occurrence evidence exists, but the retained "
        "manifests do not identify why the mail was not retained."
    ),
}
SELECTED_SANITIZED_COUNTS = (
    "messages_visited",
    "unique_candidates",
    "engineering_candidates",
    "duplicate_candidates",
    "contact_read_error",
    "no_text_body",
    "body_no_text",
    "body_html",
    "empty_after_redaction",
    "residual_quarantine",
    "oversize_body",
    "body_read_error",
    "processing_error",
)
SELECTED_ATTACHMENT_COUNTS = (
    "attachment_occurrences",
    "unique_documents",
    "parse_failed",
    "duplicate_occurrences",
    "attachment_list_errors",
    "identity_errors",
)


class MissingReferenceOriginError(RuntimeError):
    """Raised when inputs cannot be reconciled safely."""


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _as_paths(value: str | Path | Iterable[str | Path]) -> tuple[str | Path, ...]:
    if isinstance(value, (str, Path)):
        return (value,)
    return tuple(value)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def _fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"present": False, "size_bytes": None, "mtime_ns": None}
    stat = path.stat()
    return {"present": True, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MissingReferenceOriginError(f"invalid JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise MissingReferenceOriginError(f"JSON object required: {path}")
    return value


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MissingReferenceOriginError(
                        f"invalid JSONL at {path}:{line_number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise MissingReferenceOriginError(
                        f"JSON object required at {path}:{line_number}"
                    )
                yield line_number, row
    except OSError as exc:
        raise MissingReferenceOriginError(f"cannot read input: {path}") from exc


def _require_string(row: dict[str, Any], field: str, path: Path, line: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise MissingReferenceOriginError(
            f"missing non-empty string {field!r} at {path}:{line}"
        )
    return value


def _require_count(row: dict[str, Any], field: str, path: Path, line: int) -> int:
    value = row.get(field)
    if not _is_count(value):
        raise MissingReferenceOriginError(
            f"invalid count {field!r} at {path}:{line}"
        )
    return value


def _load_audit(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    data = _read_json_object(path)
    if data.get("report_version") != 1:
        raise MissingReferenceOriginError("unsupported missing-reference audit version")
    summary = data.get("summary")
    targets = data.get("targets")
    if not isinstance(summary, dict) or not isinstance(targets, list):
        raise MissingReferenceOriginError("invalid missing-reference audit shape")
    expected = summary.get("unique_target_ids")
    if not _is_count(expected) or expected != len(targets):
        raise MissingReferenceOriginError("missing-reference target count mismatch")

    result: dict[str, dict[str, Any]] = {}
    for item in targets:
        if not isinstance(item, dict):
            raise MissingReferenceOriginError("invalid missing-reference target")
        target_id = item.get("target_source_id")
        if not isinstance(target_id, str) or not TARGET_ID.fullmatch(target_id):
            raise MissingReferenceOriginError("invalid missing-reference target ID")
        if target_id in result:
            raise MissingReferenceOriginError("duplicate missing-reference target ID")
        if item.get("target_type") != "MAIL":
            raise MissingReferenceOriginError("unexpected missing-reference target type")
        if item.get("classification") != "reference_only":
            raise MissingReferenceOriginError("unexpected missing-reference classification")
        reference_count = item.get("reference_count")
        affected_count = item.get("affected_document_count")
        if not _is_count(reference_count) or not _is_count(affected_count):
            raise MissingReferenceOriginError("invalid missing-reference counts")
        result[target_id] = {
            "target_source_id": target_id,
            "reference_count": reference_count,
            "affected_document_count": affected_count,
            "audit_classification": item["classification"],
        }
    if not result:
        raise MissingReferenceOriginError("empty missing-reference audit")
    return result, summary


def _load_quarantine(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line, row in _iter_jsonl(path):
        target_id = _require_string(row, "id", path, line)
        reason = _require_string(row, "reason", path, line)
        if target_id in result:
            raise MissingReferenceOriginError(f"duplicate quarantine ID at {path}:{line}")
        result[target_id] = reason
    return result


def _load_quarantines(paths: Iterable[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in paths:
        for target_id, reason in _load_quarantine(path).items():
            previous = result.get(target_id)
            if previous is not None and previous != reason:
                raise MissingReferenceOriginError(
                    f"conflicting quarantine reasons for {target_id}"
                )
            result[target_id] = reason
    return result


def _load_message_statuses(
    paths: Iterable[Path],
    reports_by_directory: dict[Path, dict[str, Any]],
    retained_mail_ids: set[str],
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, int] | None, int]:
    by_id: dict[str, dict[str, Any]] = {}
    total_counts: Counter = Counter()
    manifest_count = 0
    for path in paths:
        if not path.exists():
            continue
        report = reports_by_directory.get(path.parent.resolve())
        if report is None:
            raise MissingReferenceOriginError(
                f"missing sanitized report for message status manifest: {path}"
            )
        stats = _selected_counts(report, SELECTED_SANITIZED_COUNTS, "sanitized report")
        rows: list[dict[str, Any]] = []
        for _, row in _iter_jsonl(path):
            rows.append(row)
        try:
            counts = validate_message_status_rows(rows, stats)
        except MessageStatusError as exc:
            raise MissingReferenceOriginError("invalid message status manifest") from exc
        for row in rows:
            target_id = row["id"]
            if target_id in by_id:
                raise MissingReferenceOriginError("duplicate message status ID")
            if (
                row["status"] == "duplicate"
                and row["duplicate_of"] not in retained_mail_ids
            ):
                raise MissingReferenceOriginError(
                    "message status duplicate target is not a retained Catalog mail"
                )
            by_id[target_id] = row
        total_counts.update(counts)
        manifest_count += 1
    if not by_id:
        return None, None, 0
    return by_id, dict(sorted(total_counts.items())), manifest_count


def _load_occurrences(
    path: Path, targets: dict[str, dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total = 0
    for line, row in _iter_jsonl(path):
        total += 1
        parent = _require_string(row, "parent_source_id", path, line)
        ordinal = row.get("attachment_ordinal")
        if type(ordinal) is not int or ordinal < 0:
            raise MissingReferenceOriginError(
                f"invalid attachment_ordinal at {path}:{line}"
            )
        status = _require_string(row, "status", path, line)
        document_id = row.get("document_id")
        if document_id is not None and (
            not isinstance(document_id, str) or not document_id
        ):
            raise MissingReferenceOriginError(f"invalid document_id at {path}:{line}")
        if parent in targets:
            by_target[parent].append({
                "line": line,
                "document_id": document_id,
                "attachment_ordinal": ordinal,
                "status": status,
            })
    return dict(by_target), total


def _load_documents(
    path: Path, needed_ids: set[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    result: dict[str, dict[str, Any]] = {}
    total = 0
    for line, row in _iter_jsonl(path):
        total += 1
        document_id = _require_string(row, "document_id", path, line)
        if document_id in result:
            raise MissingReferenceOriginError(f"duplicate document ID at {path}:{line}")
        parents = row.get("parent_source_ids")
        if not isinstance(parents, list) or any(
            not isinstance(parent, str) or not parent for parent in parents
        ):
            raise MissingReferenceOriginError(
                f"invalid parent_source_ids at {path}:{line}"
            )
        status = _require_string(row, "status", path, line)
        exported_units = row.get("exported_units")
        counts = row.get("counts")
        parser_stats = row.get("parser_stats")
        if not _is_count(exported_units):
            raise MissingReferenceOriginError(f"invalid exported_units at {path}:{line}")
        if not isinstance(counts, dict) or not isinstance(parser_stats, dict):
            raise MissingReferenceOriginError(f"invalid document stats at {path}:{line}")
        if document_id in needed_ids:
            result[document_id] = {
                "line": line,
                "document_id": document_id,
                "parent_source_ids": sorted(set(parents)),
                "status": status,
                "exported_units": exported_units,
                "counts": counts,
                "parser_stats": parser_stats,
            }
    missing = needed_ids - result.keys()
    if missing:
        raise MissingReferenceOriginError(
            f"attachment documents missing from manifest: {len(missing)}"
        )
    return result, total


def _load_retained_mail_ids(path: Path) -> tuple[set[str], int]:
    try:
        connection = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT source_id FROM documents WHERE type=? AND source_type=?",
                ("MAIL", "mail"),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise MissingReferenceOriginError(f"cannot query catalog: {path}") from exc
    if any(not isinstance(row[0], str) or not row[0] for row in rows):
        raise MissingReferenceOriginError("invalid retained mail ID")
    return {row[0] for row in rows}, len(rows)


def _selected_counts(report: dict[str, Any], fields: Iterable[str], name: str) -> dict[str, int]:
    counts = report.get("counts")
    if not isinstance(counts, dict):
        raise MissingReferenceOriginError(f"missing counts in {name}")
    result = {}
    for field in fields:
        value = counts.get(field, 0)
        if not _is_count(value):
            raise MissingReferenceOriginError(f"invalid {field} in {name}")
        result[field] = value
    return result


def _coverage_status(document: dict[str, Any]) -> str:
    if document["status"] != "processed":
        return "parse_failed"
    parser_stats = document["parser_stats"]
    if any(
        value and ("limit" in key or "stopped" in key)
        for key, value in parser_stats.items()
    ):
        return "limited"
    if (
        parser_stats.get("embedded_image_errors")
        or parser_stats.get("archive_member_errors")
        or parser_stats.get("unsupported_members")
        or document["counts"].get("residual_units_quarantined")
    ):
        return "partial_components"
    return "parsed_text"


def _classify(
    quarantine_reason: str | None,
    duplicate_of: str | None,
    shared_count: int,
) -> str:
    if quarantine_reason is not None:
        return QUARANTINE_CLASSIFICATIONS.get(
            quarantine_reason, "explicit_quarantine_other"
        )
    if duplicate_of is not None:
        return "explicit_duplicate_of_retained_candidate"
    if shared_count:
        return "attachment_shared_with_retained_mail"
    return "not_explained_by_retained_manifests"


class MissingReferenceOriginAudit:
    """Reconcile missing mail IDs without reading mail prose or the PST."""

    def __init__(
        self,
        root: Path | str = ROOT,
        *,
        audit: str | Path = DEFAULT_AUDIT,
        output: str | Path = DEFAULT_OUTPUT,
        quarantine: str | Path | Iterable[str | Path] = DEFAULT_QUARANTINE,
        message_status: (
            str | Path | Iterable[str | Path]
        ) = DEFAULT_MESSAGE_STATUS,
        occurrences: str | Path = DEFAULT_OCCURRENCES,
        documents: str | Path = DEFAULT_DOCUMENTS,
        catalog: str | Path = DEFAULT_CATALOG,
        sanitized_report: (
            str | Path | Iterable[str | Path]
        ) = DEFAULT_SANITIZED_REPORT,
        attachments_report: str | Path = DEFAULT_ATTACHMENTS_REPORT,
        original_mail: str | Path = ORIGINAL_MAIL,
        profile: str = DEFAULT_PROFILE,
    ) -> None:
        self.root = Path(root).resolve()
        self.profile = profile
        self.audit_path = _resolve(self.root, audit)
        if not self.audit_path.exists() and self.audit_path.is_relative_to(self.root):
            archived = self.root / '.private/project_records' / self.audit_path.relative_to(self.root)
            if archived.is_file():
                self.audit_path = archived
        self.output_path = _resolve(self.root, output)
        self.quarantine_paths = tuple(
            _resolve(self.root, path) for path in _as_paths(quarantine)
        )
        self.message_status_paths = tuple(
            _resolve(self.root, path) for path in _as_paths(message_status)
        )
        self.occurrences_path = _resolve(self.root, occurrences)
        self.documents_path = _resolve(self.root, documents)
        self.catalog_path = _resolve(self.root, catalog)
        self.sanitized_report_paths = tuple(
            _resolve(self.root, path) for path in _as_paths(sanitized_report)
        )
        self.attachments_report_path = _resolve(self.root, attachments_report)
        self.original_mail_path = _resolve(self.root, original_mail)
        self.quarantine_path = self.quarantine_paths[0]
        self.message_status_path = self.message_status_paths[0]
        self.sanitized_report_path = self.sanitized_report_paths[0]

    def _input_paths(self) -> tuple[Path, ...]:
        paths = (
            self.audit_path,
            *self.quarantine_paths,
            self.occurrences_path,
            self.documents_path,
            self.catalog_path,
            *self.sanitized_report_paths,
            self.attachments_report_path,
        )
        paths += tuple(
            path for path in self.message_status_paths if path.exists()
        )
        return paths

    def run(self, *, generated_at: str | None = None) -> dict[str, Any]:
        paths = self._input_paths()
        before = {path: _fingerprint(path) for path in paths}
        mail_before = _fingerprint(self.original_mail_path)
        for path in paths:
            if not path.is_file():
                raise MissingReferenceOriginError(f"required input missing: {path}")

        targets, audit_summary = _load_audit(self.audit_path)
        quarantine = _load_quarantines(self.quarantine_paths)
        occurrences, occurrence_total = _load_occurrences(
            self.occurrences_path, targets
        )
        needed_document_ids = {
            occurrence["document_id"]
            for rows in occurrences.values()
            for occurrence in rows
            if occurrence["document_id"]
        }
        documents, document_total = _load_documents(
            self.documents_path, needed_document_ids
        )
        retained_mail_ids, retained_mail_document_count = _load_retained_mail_ids(
            self.catalog_path
        )

        sanitized_reports = []
        for path in self.sanitized_report_paths:
            report = _read_json_object(path)
            sanitized_reports.append((
                path,
                report,
                _selected_counts(report, SELECTED_SANITIZED_COUNTS, "sanitized report"),
            ))
        sanitized_counts = {
            field: sum(counts[field] for _, _, counts in sanitized_reports)
            for field in SELECTED_SANITIZED_COUNTS
        }
        reports_by_directory = {
            path.parent.resolve(): report
            for path, report, _ in sanitized_reports
        }
        attachments_report = _read_json_object(self.attachments_report_path)
        attachment_counts = _selected_counts(
            attachments_report, SELECTED_ATTACHMENT_COUNTS, "attachment report"
        )
        message_status, message_status_counts, message_status_manifest_count = (
            _load_message_statuses(
                self.message_status_paths,
                reports_by_directory,
                retained_mail_ids,
            )
        )
        status_directories = {
            path.parent.resolve()
            for path in self.message_status_paths
            if path.exists()
        }
        unlogged_duplicate_candidates = sum(
            counts["duplicate_candidates"]
            for path, _, counts in sanitized_reports
            if path.parent.resolve() not in status_directories
        )

        if occurrence_total != attachment_counts["attachment_occurrences"]:
            raise MissingReferenceOriginError("attachment occurrence count mismatch")
        if document_total != attachment_counts["unique_documents"]:
            raise MissingReferenceOriginError("attachment document count mismatch")
        if retained_mail_document_count != sanitized_counts["unique_candidates"]:
            raise MissingReferenceOriginError("retained mail count mismatch")
        if targets.keys() & retained_mail_ids:
            raise MissingReferenceOriginError("audited target is present in retained catalog")

        target_rows = []
        classification_counts: Counter = Counter()
        quarantine_reason_counts: Counter = Counter()
        attachment_status_counts: Counter = Counter()
        attachment_coverage_counts: Counter = Counter()
        shared_attachment_ids: set[str] = set()
        targets_with_occurrence = 0
        quarantine_shared = Counter()
        occurrence_reference_count = 0
        duplicate_status_count = 0
        message_status_targets_matched = 0
        duplicate_links_outside_targets = 0

        for target_id in sorted(targets):
            audit_target = targets[target_id]
            target_occurrences = sorted(
                occurrences.get(target_id, []),
                key=lambda item: (
                    item["line"], item["attachment_ordinal"], item["document_id"] or ""
                ),
            )
            if target_occurrences:
                targets_with_occurrence += 1
            occurrence_reference_count += len(target_occurrences)
            document_ids = sorted({
                item["document_id"] for item in target_occurrences if item["document_id"]
            })
            if len(document_ids) != audit_target["reference_count"]:
                raise MissingReferenceOriginError(
                    f"attachment reference count mismatch for {target_id}"
                )

            document_rows = []
            shared_retained_parents: set[str] = set()
            shared_document_ids: set[str] = set()
            for document_id in document_ids:
                document = documents[document_id]
                if target_id not in document["parent_source_ids"]:
                    raise MissingReferenceOriginError(
                        f"attachment parent manifest mismatch for {target_id}"
                    )
                shared_parents = set(document["parent_source_ids"]) & retained_mail_ids
                if shared_parents:
                    shared_document_ids.add(document_id)
                    shared_retained_parents.update(shared_parents)
                coverage = _coverage_status(document)
                attachment_status_counts[document["status"]] += 1
                attachment_coverage_counts[coverage] += 1
                document_rows.append({
                    "document_id": document_id,
                    "line": document["line"],
                    "status": document["status"],
                    "coverage_status": coverage,
                    "exported_units": document["exported_units"],
                    "parent_source_id_count": len(document["parent_source_ids"]),
                    "shared_with_retained_mail": bool(shared_parents),
                    "shared_retained_parent_count": len(shared_parents),
                })

            quarantine_reason = quarantine.get(target_id)
            status_row = message_status.get(target_id) if message_status is not None else None
            message_status_value = status_row["status"] if status_row else None
            duplicate_of = (
                status_row["duplicate_of"]
                if status_row is not None and status_row["status"] == "duplicate"
                else None
            )
            if status_row is not None:
                message_status_targets_matched += 1
            classification = _classify(
                quarantine_reason, duplicate_of, len(shared_document_ids)
            )
            classification_counts[classification] += 1
            if classification == "explicit_duplicate_of_retained_candidate":
                duplicate_status_count += 1
            if quarantine_reason is not None:
                quarantine_reason_counts[quarantine_reason] += 1
                quarantine_shared[
                    "shared" if shared_document_ids else "not_shared"
                ] += 1
            shared_attachment_ids.update(shared_document_ids)

            target_rows.append({
                "target_source_id": target_id,
                "resolution": "unresolved",
                "classification": classification,
                "audit_reference_count": audit_target["reference_count"],
                "audit_affected_document_count": audit_target["affected_document_count"],
                "quarantine_reason": quarantine_reason,
                "message_status": message_status_value,
                "message_status_duplicate_of": duplicate_of,
                "retained_primary_match": False,
                "attachment_occurrence_count": len(target_occurrences),
                "attachment_document_count": len(document_ids),
                "attachment_document_ids": document_ids,
                "attachment_status_counts": dict(sorted(Counter(
                    item["status"] for item in target_occurrences
                ).items())),
                "attachment_coverage_counts": dict(sorted(Counter(
                    row["coverage_status"] for row in document_rows
                ).items())),
                "shared_attachment_document_count": len(shared_document_ids),
                "shared_retained_parent_count": len(shared_retained_parents),
                "shared_retained_parent_preview": sorted(shared_retained_parents)[:3],
                "evidence": {
                    "occurrences": target_occurrences,
                    "documents": document_rows,
                },
            })

        after = {path: _fingerprint(path) for path in paths}
        mail_after = _fingerprint(self.original_mail_path)
        inputs_unchanged = before == after
        mail_unchanged = mail_before == mail_after
        if not inputs_unchanged:
            raise MissingReferenceOriginError("input changed during audit")
        if not mail_unchanged:
            raise MissingReferenceOriginError("original mail metadata changed during audit")

        quarantine_targets = set(quarantine) & targets.keys()
        quarantine_outside = set(quarantine) - targets.keys()
        if message_status is not None:
            duplicate_links_outside_targets = sum(
                1
                for target_id, row in message_status.items()
                if row["status"] == "duplicate" and target_id not in targets
            )
        direct = sum(
            count for name, count in classification_counts.items()
            if name.startswith("explicit_quarantine_")
        )
        shared_only = classification_counts["attachment_shared_with_retained_mail"]
        unexplained = classification_counts["not_explained_by_retained_manifests"]
        summary = {
            "audited_targets": len(targets),
            "audit_reference_count": sum(
                item["reference_count"] for item in targets.values()
            ),
            "target_attachment_occurrence_count": occurrence_reference_count,
            "attachment_manifest_occurrence_count": occurrence_total,
            "targets_with_attachment_occurrence": targets_with_occurrence,
            "unique_attachment_document_count": len(needed_document_ids),
            "shared_attachment_document_count": len(shared_attachment_ids),
            "quarantine_record_count": len(quarantine),
            "quarantine_manifest_count": len(self.quarantine_paths),
            "quarantine_targets_matched": len(quarantine_targets),
            "directly_explained_by_quarantine": direct,
            "explicitly_attributed_to_duplicate": duplicate_status_count,
            "not_directly_explained": len(targets) - direct,
            "shared_attachment_only": shared_only,
            "unexplained_by_retained_manifests": unexplained,
            "retained_primary_matches": 0,
            "remaining_unresolved": len(targets),
            "classification_counts": dict(sorted(classification_counts.items())),
            "quarantine_reason_counts": dict(sorted(quarantine_reason_counts.items())),
            "quarantine_shared_attachment_counts": dict(sorted(quarantine_shared.items())),
            "attachment_status_counts": dict(sorted(attachment_status_counts.items())),
            "attachment_coverage_counts": dict(sorted(attachment_coverage_counts.items())),
            "quarantine_entries_outside_audited_targets": len(quarantine_outside),
            "message_status_record_count": len(message_status) if message_status is not None else 0,
            "message_status_manifest_count": message_status_manifest_count,
            "message_status_targets_matched": message_status_targets_matched,
            "message_status_duplicate_links_outside_audited_targets": duplicate_links_outside_targets,
            "retained_mail_document_count": retained_mail_document_count,
            "retained_mail_unique_source_ids": len(retained_mail_ids),
            "retained_mail_duplicate_source_ids": (
                retained_mail_document_count - len(retained_mail_ids)
            ),
        }
        context = {
            "sanitized_counts": sanitized_counts,
            "sanitized_report_count": len(sanitized_reports),
            "attachments_v2_counts": attachment_counts,
            "message_status_available": message_status is not None,
            "message_status_manifest_count": message_status_manifest_count,
            "message_status_counts": message_status_counts,
            "unlogged_duplicate_candidates": unlogged_duplicate_candidates,
            "quarantine_manifest_count": len(self.quarantine_paths),
            "non_quarantined_missing_targets": len(targets) - len(quarantine_targets),
            "deduplication_interpretation": (
                "At least one sanitized source has no per-message status manifest. "
                "Duplicate candidates from that source remain aggregate-only and "
                "cannot be attributed to individual targets."
                if message_status is not None and unlogged_duplicate_candidates else
                "The message status manifest identifies duplicate targets and their "
                "retained candidate links individually. These links remain unresolved "
                "because the parent mail and its prose were not restored."
                if message_status is not None else
                "The non-quarantined missing-target count is within the aggregate "
                "duplicate-candidate count, but duplicate IDs were not logged. The "
                "audit therefore does not individually attribute those targets to "
                "body deduplication."
            ),
            "shared_attachment_interpretation": (
                "A shared attachment document is partial provenance evidence only. "
                "It does not prove that the parent mail body was a duplicate and does "
                "not justify creating a missing mail entity."
            ),
        }
        integrity = {
            "read_only": True,
            "source_prose_fields_read": False,
            "email_subject_or_body_emitted": False,
            "catalog_modified": False,
            "message_status_read": message_status is not None,
            "inputs_unchanged_during_audit": inputs_unchanged,
            "inputs": [
                {
                    "path": _relative(self.root, path),
                    "before": before[path],
                    "after": after[path],
                    "unchanged": before[path] == after[path],
                }
                for path in paths
            ],
            "original_mail": {
                "path": _relative(self.root, self.original_mail_path),
                **mail_after,
                "read": False,
                "stat_unchanged_during_audit": mail_unchanged,
            },
            "catalog_query": {
                "path": _relative(self.root, self.catalog_path),
                "columns_read": ["source_id"],
                "filter": "type='MAIL' AND source_type='mail'",
                "mode": "sqlite_read_only",
            },
        }
        return {
            "report_version": 1,
            "asset_profile": self.profile,
            "generated_at": generated_at or datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "scope": (
                "Read-only origin reconciliation of unresolved parent-mail references; "
                "no PST parsing, prose extraction, catalog mutation, or entity creation."
            ),
            "method": {
                "matching": "exact_opaque_source_id",
                "target_scope": "audit.reference_only_MAIL_targets",
                "quarantine_manifest_read": True,
                "message_status_manifest_read": message_status is not None,
                "attachment_manifest_read": True,
                "candidate_prose_read": False,
                "pst_read": False,
                "classification_is_evidence_based": True,
            },
            "classification_definitions": CLASSIFICATION_DEFINITIONS,
            "summary": summary,
            "context": context,
            "integrity": integrity,
            "targets": target_rows,
        }

    def write_report(
        self, report: dict[str, Any], output_path: str | Path | None = None
    ) -> Path:
        path = _resolve(self.root, output_path) if output_path is not None else self.output_path
        try:
            path.relative_to((self.root / "data").resolve())
        except ValueError:
            pass
        else:
            raise ValueError("origin reports must not be written under data/")
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读定位缺失父邮件引用的来源环节，不读取 PST 或邮件正文"
    )
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default=DEFAULT_PROFILE)
    parser.add_argument("--audit")
    parser.add_argument("--output")
    parser.add_argument("--quarantine", action="append")
    parser.add_argument("--message-status", action="append")
    parser.add_argument("--occurrences")
    parser.add_argument("--documents")
    parser.add_argument("--catalog")
    parser.add_argument("--sanitized-report", action="append")
    parser.add_argument("--attachments-report")
    parser.add_argument("--original-mail")
    args = parser.parse_args(argv)
    defaults = PROFILE_DEFAULTS[args.profile]
    runner = MissingReferenceOriginAudit(
        ROOT,
        audit=args.audit or defaults["audit"],
        output=args.output or defaults["output"],
        quarantine=args.quarantine or defaults["quarantine"],
        message_status=args.message_status or defaults["message_status"],
        occurrences=args.occurrences or defaults["occurrences"],
        documents=args.documents or defaults["documents"],
        catalog=args.catalog or defaults["catalog"],
        sanitized_report=args.sanitized_report or defaults["sanitized_report"],
        attachments_report=args.attachments_report or defaults["attachments_report"],
        original_mail=args.original_mail or ORIGINAL_MAIL,
        profile=args.profile,
    )
    report = runner.run()
    path = runner.write_report(report)
    print(json.dumps({
        "status": "completed",
        "output": _relative(ROOT, path),
        "summary": report["summary"],
        "integrity": {
            "pst_read": report["integrity"]["original_mail"]["read"],
            "inputs_unchanged": report["integrity"]["inputs_unchanged_during_audit"],
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
