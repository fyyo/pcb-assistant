"""Read-only exact-ID audit for unresolved Phase 3 catalog references.

The audit never rebuilds or edits source assets. It scans retained mail,
attachment, case, and catalog assets for exact target identifiers and writes a
JSON/CSV report containing identifiers, paths, line numbers, field names, and
counts only. Email bodies and subjects are deliberately excluded from output.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import gzip
import json
import io
from pathlib import Path
import re
from typing import Any, Iterable, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNRESOLVED = "data/phase3_catalog_v1/unresolved_references.jsonl"
DEFAULT_JSON_OUTPUT = "docs/phase3_missing_reference_audit_20261021.json"
DEFAULT_CSV_OUTPUT = "docs/phase3_missing_reference_targets_20261021.csv"
DEFAULT_PROFILE = "v1"
ORIGINAL_MAIL = "19924784-20240420.pst"

# Values under these fields are source content, not identity/reference evidence.
# They are skipped so an incidental occurrence in an email body is never treated
# as a document or relationship match.
CONTENT_FIELDS = frozenset({
    "text", "subject", "title", "original_title", "topics",
    "document_topics", "tags",
})

CLASSIFICATION_DEFINITIONS = {
    "indexed_document_present": (
        "The exact target ID is a source_id in the retained catalog or source map."
    ),
    "retained_document_present": (
        "The exact target ID is a primary identity in a retained non-catalog asset, "
        "but no catalog source_id match was found."
    ),
    "reference_only": (
        "The exact target ID occurs only in reference or other non-identity fields; "
        "no independent retained document was found."
    ),
    "not_found": (
        "The exact target ID was not found in any scanned identifier/reference field."
    ),
}


class MissingReferenceAuditError(RuntimeError):
    """Raised when an input cannot be audited safely."""


@dataclass(frozen=True)
class AssetSpec:
    path: str
    kind: str
    primary_fields: tuple[str, ...] = ()
    reference_fields: tuple[str, ...] = ()


ASSET_SPECS_V1 = (
    AssetSpec(
        "data/sanitized_v3/candidates.jsonl", "retained_mail", ("id",)
    ),
    AssetSpec(
        "data/sanitized_v3/engineering_candidates.jsonl", "retained_mail", ("id",)
    ),
    AssetSpec(
        "data/attachments_v1/documents.jsonl", "attachment_document",
        ("document_id",), ("parent_source_ids",),
    ),
    AssetSpec(
        "data/attachments_v1/occurrences.jsonl", "attachment_occurrence",
        (), ("parent_source_id", "document_id"),
    ),
    AssetSpec(
        "data/attachments_v1/engineering_candidates.jsonl",
        "attachment_candidate", ("id",), ("document_id", "parent_source_ids"),
    ),
    AssetSpec(
        "data/attachments_v2/documents.jsonl", "attachment_document",
        ("document_id",), ("parent_source_ids",),
    ),
    AssetSpec(
        "data/attachments_v2/occurrences.jsonl", "attachment_occurrence",
        (), ("parent_source_id", "document_id"),
    ),
    AssetSpec(
        "data/attachments_v2/engineering_candidates.jsonl",
        "attachment_candidate", ("id",), ("document_id", "parent_source_ids"),
    ),
    AssetSpec(
        "data/combined_sources_v1/engineering_candidates.jsonl",
        "combined_source", ("id",), ("document_id", "parent_source_ids"),
    ),
    AssetSpec(
        "data/knowledge_v1/cases.jsonl", "knowledge_case", ("source_id",)
    ),
    AssetSpec(
        "data/phase3_catalog_v1/catalog.jsonl", "catalog",
        ("source_id",), ("metadata.parent_source_ids",),
    ),
    AssetSpec(
        "data/phase3_catalog_v1/source_map.jsonl", "catalog",
        ("source_id",), ("metadata.parent_source_ids",),
    ),
)

ASSET_SPECS_V3 = (
    AssetSpec(
        "data/sanitized_v8/candidates.jsonl", "retained_mail", ("id",)
    ),
    AssetSpec(
        "data/sanitized_v8/engineering_candidates.jsonl", "retained_mail", ("id",)
    ),
    AssetSpec(
        "data/sanitized_v9/candidates.jsonl", "retained_mail", ("id",)
    ),
    AssetSpec(
        "data/sanitized_v9/engineering_candidates.jsonl", "retained_mail", ("id",)
    ),
    AssetSpec(
        "data/attachments_v2/documents.jsonl", "attachment_document",
        ("document_id",), ("parent_source_ids",),
    ),
    AssetSpec(
        "data/attachments_v2/occurrences.jsonl", "attachment_occurrence",
        (), ("parent_source_id", "document_id"),
    ),
    AssetSpec(
        "data/attachments_v2/engineering_candidates.jsonl",
        "attachment_candidate", ("id",), ("document_id", "parent_source_ids"),
    ),
    AssetSpec(
        "data/combined_sources_v1/engineering_candidates.jsonl",
        "combined_source", ("id",), ("document_id", "parent_source_ids"),
    ),
    AssetSpec(
        "data/knowledge_v1/cases.jsonl", "knowledge_case", ("source_id",)
    ),
    AssetSpec(
        "data/phase3_catalog_v3/catalog.jsonl.gz", "catalog",
        ("source_id",), ("metadata.parent_source_ids",),
    ),
    AssetSpec(
        "data/phase3_catalog_v3/source_map.jsonl.gz", "catalog",
        ("source_id",), ("metadata.parent_source_ids",),
    ),
)

ASSET_SPECS = ASSET_SPECS_V1
ASSET_PROFILES = {
    "v1": ASSET_SPECS_V1,
    "v3": ASSET_SPECS_V3,
}
PROFILE_DEFAULTS = {
    "v1": {
        "unresolved": DEFAULT_UNRESOLVED,
        "json_output": DEFAULT_JSON_OUTPUT,
        "csv_output": DEFAULT_CSV_OUTPUT,
    },
    "v3": {
        "unresolved": "data/phase3_catalog_v3/unresolved_references.jsonl.gz",
        "json_output": "docs/phase3_missing_reference_audit_20261105.json",
        "csv_output": "docs/phase3_missing_reference_targets_20261105.csv",
    },
}


def _normalized_field(path: str) -> str:
    return re.sub(r"\[\d+\]", "", path)


def _field_leaf(path: str) -> str:
    normalized = _normalized_field(path)
    return normalized.rsplit(".", 1)[-1]


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        if path.name.endswith(".gz"):
            stream = gzip.open(path, "rt", encoding="utf-8-sig")
        else:
            stream = path.open("r", encoding="utf-8-sig")
    except OSError as exc:
        raise MissingReferenceAuditError(f"cannot open {path}: {exc}") from exc
    with stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (UnicodeError, ValueError) as exc:
                raise MissingReferenceAuditError(
                    f"invalid JSON in {path} at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise MissingReferenceAuditError(
                    f"expected JSON object in {path} at line {line_number}"
                )
            yield line_number, row


def _iter_scalar_paths(value: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key in sorted(value):
            if key in CONTENT_FIELDS:
                continue
            child = f"{prefix}.{key}" if prefix else key
            yield from _iter_scalar_paths(value[key], child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_scalar_paths(item, f"{prefix}[{index}]")
    elif isinstance(value, str) and prefix:
        yield prefix, value


def _fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _sorted_counter(counter: Counter) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _role_for(spec: AssetSpec, field_path: str) -> str:
    normalized = _normalized_field(field_path)
    if normalized in spec.primary_fields:
        return "catalog_primary" if spec.kind == "catalog" else "primary"
    if normalized in spec.reference_fields:
        return "reference"
    return "other"


def _require_string(row: dict[str, Any], field: str, path: Path, line: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise MissingReferenceAuditError(
            f"missing non-empty string field {field!r} in {path} at line {line}"
        )
    return value


class MissingReferenceAudit:
    """Perform and serialize a deterministic, read-only missing-ID audit."""

    def __init__(
        self,
        root: Path | str = ROOT,
        unresolved_path: str | Path = DEFAULT_UNRESOLVED,
        asset_specs: Iterable[AssetSpec] = ASSET_SPECS,
        original_mail: str | Path = ORIGINAL_MAIL,
        profile: str = DEFAULT_PROFILE,
    ) -> None:
        self.root = Path(root).resolve()
        self.unresolved_path = self._resolve_input(unresolved_path)
        self.asset_specs = tuple(asset_specs)
        self.original_mail = self._resolve_input(original_mail)
        self.profile = profile

    def _resolve_input(self, value: str | Path) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    def _targets(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        targets: dict[str, dict[str, Any]] = {}
        affected_documents: set[str] = set()
        breakdowns = {
            "target_type": Counter(),
            "relation": Counter(),
            "reason": Counter(),
            "source_path": Counter(),
        }
        composite: Counter = Counter()
        reference_count = 0
        for line, row in _iter_jsonl(self.unresolved_path):
            target_id = _require_string(
                row, "target_source_id", self.unresolved_path, line
            )
            target_type = _require_string(row, "target_type", self.unresolved_path, line)
            relation = _require_string(row, "relation", self.unresolved_path, line)
            reason = _require_string(row, "reason", self.unresolved_path, line)
            source_path = _require_string(
                row, "source_path", self.unresolved_path, line
            )
            from_id = _require_string(row, "from_id", self.unresolved_path, line)
            _require_string(row, "reference_id", self.unresolved_path, line)

            target = targets.setdefault(target_id, {
                "target_source_id": target_id,
                "target_types": set(),
                "reference_count": 0,
                "affected_document_ids": set(),
                "by_relation": Counter(),
                "by_reason": Counter(),
                "by_source_path": Counter(),
                "hits": [],
            })
            target["target_types"].add(target_type)
            target["reference_count"] += 1
            target["affected_document_ids"].add(from_id)
            target["by_relation"][relation] += 1
            target["by_reason"][reason] += 1
            target["by_source_path"][source_path] += 1
            breakdowns["target_type"][target_type] += 1
            breakdowns["relation"][relation] += 1
            breakdowns["reason"][reason] += 1
            breakdowns["source_path"][source_path] += 1
            composite[target_type, relation, reason, source_path] += 1
            affected_documents.add(from_id)
            reference_count += 1
        if not targets:
            raise MissingReferenceAuditError(
                f"no unresolved references found in {self.unresolved_path}"
            )
        return targets, {
            "references": reference_count,
            "unique_targets": len(targets),
            "affected_documents": len(affected_documents),
            "breakdowns": breakdowns,
            "composite": composite,
        }
    def _scan_assets(self, targets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        target_ids = set(targets)
        summaries: list[dict[str, Any]] = []
        for spec in self.asset_specs:
            path = self._resolve_input(spec.path)
            if not path.is_file():
                raise MissingReferenceAuditError(f"required asset is missing: {path}")
            summary = {
                "path": spec.path,
                "kind": spec.kind,
                "bytes": path.stat().st_size,
                "records": 0,
                "exact_hits": 0,
                "targets_hit": 0,
                "role_counts": Counter(),
                "field_counts": Counter(),
            }
            hit_targets: set[str] = set()
            for line, row in _iter_jsonl(path):
                summary["records"] += 1
                for field_path, value in _iter_scalar_paths(row):
                    if value not in target_ids:
                        continue
                    role = _role_for(spec, field_path)
                    hit = {
                        "path": spec.path,
                        "line": line,
                        "field": field_path,
                        "role": role,
                        "asset_kind": spec.kind,
                    }
                    targets[value]["hits"].append(hit)
                    hit_targets.add(value)
                    summary["exact_hits"] += 1
                    summary["role_counts"][role] += 1
                    summary["field_counts"][_field_leaf(field_path)] += 1
            summary["targets_hit"] = len(hit_targets)
            summary["role_counts"] = _sorted_counter(summary["role_counts"])
            summary["field_counts"] = _sorted_counter(summary["field_counts"])
            summaries.append(summary)
        return summaries

    def _classification(self, target: dict[str, Any]) -> str:
        roles = {hit["role"] for hit in target["hits"]}
        if "catalog_primary" in roles:
            return "indexed_document_present"
        if "primary" in roles:
            return "retained_document_present"
        if target["hits"]:
            return "reference_only"
        return "not_found"

    def run(self, generated_at: str | None = None) -> dict[str, Any]:
        input_paths = [self.unresolved_path, *(
            self._resolve_input(spec.path) for spec in self.asset_specs
        )]
        if self.original_mail.is_file():
            input_paths.append(self.original_mail)
        before = {path: _fingerprint(path) for path in input_paths}

        targets, reference_summary = self._targets()
        assets = self._scan_assets(targets)

        after = {path: _fingerprint(path) for path in input_paths}
        inputs_unchanged = before == after
        if not inputs_unchanged:
            raise MissingReferenceAuditError(
                "an input changed while the read-only audit was running"
            )

        final_targets: list[dict[str, Any]] = []
        classification_counts: Counter = Counter()
        for target_id in sorted(targets):
            target = targets[target_id]
            target["hits"].sort(
                key=lambda hit: (hit["path"], hit["line"], hit["field"], hit["role"])
            )
            classification = self._classification(target)
            classification_counts[classification] += 1
            target_types = sorted(target["target_types"])
            final_targets.append({
                "target_source_id": target_id,
                "target_type": target_types[0] if len(target_types) == 1 else "MIXED",
                "target_types": target_types,
                "classification": classification,
                "reference_count": target["reference_count"],
                "affected_document_count": len(target["affected_document_ids"]),
                "affected_document_ids": sorted(target["affected_document_ids"]),
                "by_relation": _sorted_counter(target["by_relation"]),
                "by_reason": _sorted_counter(target["by_reason"]),
                "by_source_path": _sorted_counter(target["by_source_path"]),
                "hit_count": len(target["hits"]),
                "hit_roles": _sorted_counter(Counter(
                    hit["role"] for hit in target["hits"]
                )),
                "evidence_assets": sorted({hit["path"] for hit in target["hits"]}),
                "hits": target["hits"],
            })

        composite = [
            {
                "target_type": key[0],
                "relation": key[1],
                "reason": key[2],
                "source_path": key[3],
                "count": count,
            }
            for key, count in reference_summary["composite"].items()
        ]
        composite.sort(key=lambda row: (
            row["target_type"], row["relation"], row["reason"],
            row["source_path"], row["count"],
        ))

        mail_present = self.original_mail.is_file()
        mail_stat = self.original_mail.stat() if mail_present else None
        return {
            "report_version": 1,
            "asset_profile": self.profile,
            "generated_at": generated_at or datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "workspace": str(self.root),
            "scope": (
                "Read-only exact-string audit of unresolved catalog target IDs "
                "across retained source and catalog assets."
            ),
            "method": {
                "matching": "exact_full_string_equality",
                "fuzzy_matching": False,
                "prefix_matching": False,
                "content_fields_excluded": sorted(CONTENT_FIELDS),
                "content_or_subject_included": False,
            },
            "classification_definitions": CLASSIFICATION_DEFINITIONS,
            "summary": {
                "unresolved_references": reference_summary["references"],
                "unique_target_ids": reference_summary["unique_targets"],
                "affected_documents": reference_summary["affected_documents"],
                "targets_with_evidence": sum(
                    count for classification, count in classification_counts.items()
                    if classification != "not_found"
                ),
                "classification_counts": _sorted_counter(classification_counts),
                "by_target_type": _sorted_counter(reference_summary["breakdowns"]["target_type"]),
                "by_relation": _sorted_counter(reference_summary["breakdowns"]["relation"]),
                "by_reason": _sorted_counter(reference_summary["breakdowns"]["reason"]),
                "by_source_path": _sorted_counter(reference_summary["breakdowns"]["source_path"]),
                "by_target_type_relation_reason_source_path": composite,
            },
            "assets": assets,
            "targets": final_targets,
            "integrity": {
                "read_only_analysis": True,
                "inputs_unchanged_during_audit": inputs_unchanged,
                "catalog_modified": False,
                "source_assets_modified": False,
                "relationships_fabricated": False,
                "email_bodies_emitted": False,
                "original_mail": {
                    "path": ORIGINAL_MAIL,
                    "present": mail_present,
                    "read": False,
                    "size_bytes": mail_stat.st_size if mail_stat else None,
                    "last_write_time_ns": mail_stat.st_mtime_ns if mail_stat else None,
                    "stat_unchanged_during_audit": (
                        mail_present and before.get(self.original_mail)
                        == after.get(self.original_mail)
                    ),
                },
            },
            "notes": [
                "No catalog records or source assets were modified.",
                "No unresolved relationship was repaired or fabricated.",
                "An ID found only in parent_source_ids is reference evidence, not proof "
                "that an independent source document exists.",
            ],
        }

    def write_reports(
        self,
        report: dict[str, Any],
        json_output: str | Path = DEFAULT_JSON_OUTPUT,
        csv_output: str | Path = DEFAULT_CSV_OUTPUT,
    ) -> tuple[Path, Path]:
        json_path = self._resolve_input(json_output)
        csv_path = self._resolve_input(csv_output)
        if json_path == csv_path:
            raise ValueError("JSON and CSV outputs must be different files")
        existing = [path for path in (json_path, csv_path) if path.exists()]
        if existing:
            names = ", ".join(str(path) for path in existing)
            raise FileExistsError(f"refusing to overwrite existing output: {names}")

        json_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        json_text = json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=False
        ) + "\n"

        fieldnames = [
            "target_source_id", "target_type", "classification",
            "reference_count", "affected_document_count",
            "affected_document_ids", "relations", "reasons", "source_paths",
            "hit_count", "hit_locations",
        ]
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(
            csv_buffer, fieldnames=fieldnames, lineterminator="\n"
        )
        writer.writeheader()
        for target in report["targets"]:
            writer.writerow({
                "target_source_id": target["target_source_id"],
                "target_type": target["target_type"],
                "classification": target["classification"],
                "reference_count": target["reference_count"],
                "affected_document_count": target["affected_document_count"],
                "affected_document_ids": ";".join(target["affected_document_ids"]),
                "relations": ";".join(
                    f"{key}:{value}" for key, value in target["by_relation"].items()
                ),
                "reasons": ";".join(
                    f"{key}:{value}" for key, value in target["by_reason"].items()
                ),
                "source_paths": ";".join(
                    f"{key}:{value}" for key, value in target["by_source_path"].items()
                ),
                "hit_count": target["hit_count"],
                "hit_locations": ";".join(
                    f"{hit['path']}:{hit['line']}:{hit['field']}:{hit['role']}"
                    for hit in target["hits"]
                ),
            })

        # Write both files only after all validation and serialization succeeded.
        json_path.write_text(json_text, encoding="utf-8", newline="\n")
        csv_path.write_text(csv_buffer.getvalue(), encoding="utf-8-sig", newline="")
        return json_path, csv_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", choices=sorted(ASSET_PROFILES), default=DEFAULT_PROFILE)
    parser.add_argument("--unresolved")
    parser.add_argument("--json-output")
    parser.add_argument("--csv-output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    defaults = PROFILE_DEFAULTS[args.profile]
    audit = MissingReferenceAudit(
        args.root,
        args.unresolved or defaults["unresolved"],
        ASSET_PROFILES[args.profile],
        profile=args.profile,
    )
    report = audit.run()
    json_path, csv_path = audit.write_reports(
        report,
        args.json_output or defaults["json_output"],
        args.csv_output or defaults["csv_output"],
    )
    summary = report["summary"]
    print(
        "audited "
        f"{summary['unresolved_references']} references / "
        f"{summary['unique_target_ids']} targets / "
        f"{summary['affected_documents']} documents"
    )
    print("classifications:", json.dumps(
        summary["classification_counts"], ensure_ascii=False, sort_keys=True
    ))
    print("json:", json_path)
    print("csv:", csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
