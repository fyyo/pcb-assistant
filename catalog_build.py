"""Create a new catalog from existing sanitized assets; never reprocess the PST."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from catalog_common import (
    CatalogError, FIELD_LABELS, ID_VERSION, ROOT, SCHEMA, SCHEMA_VERSION, TOPICS,
    canonical_topic, digest_file, digest_text, infer_public_topics, json_text,
    jsonl_records, normalize_text, normalize_title, read_json, stable_id,
    validate_database, write_json, VersionRegistry, ImportBatch,
    write_version_registry, write_import_batch, version_paths,
    write_entity_mapping, write_relation_mapping, write_fingerprint_mapping,
    write_jsonl_gzip,
)


DEFAULT_MAIL_SOURCE = "data/sanitized_v3/candidates.jsonl"
DEFAULT_MAIL_VERSION = "sanitized_v3"
DEFAULT_ATTACHMENT_SOURCE = "data/attachments_v2"
DEFAULT_CASE_SOURCE = "data/knowledge_v1"
ATTACHMENT_FILES = ("documents.jsonl", "occurrences.jsonl", "engineering_candidates.jsonl")

PRIMARY_INPUTS = (
    DEFAULT_MAIL_SOURCE, "data/knowledge_v1/cases.jsonl",
    "data/knowledge_v1/knowledge.sqlite3", "data/attachments_v2/documents.jsonl",
    "data/attachments_v2/occurrences.jsonl", "data/attachments_v2/engineering_candidates.jsonl",
    "data/public_lamination_v1/manifest.json", "data/public_lamination_v1/pages.jsonl",
    "data/public_lamination_v2/manifest.json", "data/public_lamination_v2/units.jsonl",
)
OPTIONAL_INPUTS = (
    "data/attachments_v1/documents.jsonl", "data/attachments_v1/engineering_candidates.jsonl",
    "data/combined_sources_v1/engineering_candidates.jsonl",
    "data/current_model.json", "data/current_training_sources.json", "data/work_state.json",
    "data/sanitized_v1/candidates.jsonl.partial", "data/sanitized_v1/engineering_candidates.jsonl.partial",
    "data/sanitized_v1/quarantine_manifest.jsonl.partial", "data/sanitized_v1/progress.json",
    "dist/dify_export_20261011_complete/cases/manifest.jsonl",
    "dist/dify_export_20261011_complete/attachments/manifest.jsonl",
    "dist/dify_export_20261011_complete/public/manifest.jsonl",
)


def emit(event: str, **details) -> None:
    print(json.dumps({"event": event, **details}, ensure_ascii=False), flush=True)


def normalize_mail_sources(mail_sources=None) -> list[dict]:
    """Return ordered mail candidate sources without changing the legacy default."""
    if not mail_sources:
        return [{"path": DEFAULT_MAIL_SOURCE, "version": DEFAULT_MAIL_VERSION}]
    result = []
    for item in mail_sources:
        if isinstance(item, str):
            path, version = item, Path(item).parent.name
        else:
            path = item.get("path")
            version = item.get("version") or (Path(path).parent.name if path else None)
        if not isinstance(path, str) or not path or not isinstance(version, str) or not version:
            raise CatalogError("invalid_mail_source")
        result.append({"path": Path(path).as_posix(), "version": version})
    if len({item["path"] for item in result}) != len(result):
        raise CatalogError("duplicate_mail_source")
    return result


def normalize_attachment_sources(attachment_sources=None) -> list[dict]:
    """Return ordered attachment sources without changing the legacy default."""
    if not attachment_sources:
        return [{"base": DEFAULT_ATTACHMENT_SOURCE,
                 "version": Path(DEFAULT_ATTACHMENT_SOURCE).name}]
    result = []
    for item in attachment_sources:
        if isinstance(item, str):
            base, version = item, None
        else:
            base = item.get("path") or item.get("base")
            version = item.get("version")
        if not isinstance(base, str) or not base:
            raise CatalogError("invalid_attachment_source")
        path = Path(base)
        if path.name == "documents.jsonl":
            path = path.parent
        version = version or path.name
        if not isinstance(version, str) or not version:
            raise CatalogError("invalid_attachment_source")
        result.append({"base": path.as_posix(), "version": version})
    if len({item["base"] for item in result}) != len(result):
        raise CatalogError("duplicate_attachment_source")
    if len({item["version"] for item in result}) != len(result):
        raise CatalogError("duplicate_attachment_source_version")
    return result


def normalize_case_source(case_source=None) -> dict:
    if case_source is None:
        return {"base": DEFAULT_CASE_SOURCE, "version": "knowledge_v1"}
    if isinstance(case_source, (str, Path)):
        base, version = str(case_source), None
    elif isinstance(case_source, dict):
        base, version = case_source.get("base"), case_source.get("version")
    else:
        raise CatalogError("invalid_case_source")
    if not isinstance(base, str) or not base:
        raise CatalogError("invalid_case_source")
    path = Path(base)
    version = version or path.name
    if not isinstance(version, str) or not version:
        raise CatalogError("invalid_case_source")
    return {"base": path.as_posix(), "version": version}


def primary_inputs(mail_sources=None, attachment_sources=None, case_source=None) -> tuple[str, ...]:
    sources = normalize_mail_sources(mail_sources)
    attachments = normalize_attachment_sources(attachment_sources)
    cases = normalize_case_source(case_source)
    fixed = tuple(path for path in PRIMARY_INPUTS
                  if path != DEFAULT_MAIL_SOURCE
                  and not path.startswith(DEFAULT_CASE_SOURCE + "/")
                  and not path.startswith(DEFAULT_ATTACHMENT_SOURCE + "/"))
    attachment_paths = tuple(
        f"{source['base']}/{name}" for source in attachments for name in ATTACHMENT_FILES
    )
    case_paths = tuple(f"{cases['base']}/{name}" for name in ("cases.jsonl", "knowledge.sqlite3"))
    return tuple(item["path"] for item in sources) + attachment_paths + case_paths + fixed


def source_snapshots(root: Path, mail_sources=None, attachment_sources=None, case_source=None) -> dict:
    paths = set(primary_inputs(mail_sources, attachment_sources, case_source))
    paths.update(relative for relative in OPTIONAL_INPUTS if (root / relative).is_file())
    for relative in list(paths):
        report = (Path(relative).parent / "report.json").as_posix()
        if (root / report).is_file():
            paths.add(report)
    result = {}
    for relative in sorted(paths):
        path = root / relative
        before = path.stat()
        sha256 = digest_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise CatalogError("source_changed_during_fingerprinting")
        result[relative] = {"sha256": sha256, "bytes": after.st_size, "mtime_ns": after.st_mtime_ns}
    return result


def verify_source_reports(root: Path, snapshots: dict, mail_sources=None,
                          attachment_sources=None, case_source=None) -> None:
    reports = {}
    for relative in primary_inputs(mail_sources, attachment_sources, case_source):
        parent = Path(relative).parent.as_posix()
        if parent not in reports:
            reports[parent] = read_json(root / parent / "report.json")
        expected = reports[parent].get("artifacts", {}).get(Path(relative).name)
        if isinstance(expected, dict):
            expected = expected.get("sha256")
        if expected != snapshots[relative]["sha256"]:
            raise CatalogError("source_report_checksum_mismatch")


def checked_output(root: Path, output: Path) -> Path:
    output = output.resolve()
    if (output.parent != (root / "data").resolve()
            or not re.fullmatch(r"phase3_catalog_[A-Za-z0-9_-]+", output.name)):
        raise CatalogError("output_must_be_new_phase3_catalog_directory_under_data")
    if output.exists() or output.is_symlink():
        raise CatalogError("output_already_exists")
    return output


class Builder:
    def __init__(self, root: Path, staging: Path, indexed_at: str, mail_sources=None,
                 attachment_sources=None, case_source=None, historical_exclusions="full_record"):
        self.root, self.staging, self.indexed_at = root, staging, indexed_at
        self.mail_sources = normalize_mail_sources(mail_sources)
        self.attachment_sources = normalize_attachment_sources(attachment_sources)
        self.case_source = normalize_case_source(case_source)
        if historical_exclusions not in ("full_record", "provenance_only"):
            raise CatalogError("invalid_historical_exclusions_mode")
        self.historical_exclusions = historical_exclusions
        self.connection = sqlite3.connect(staging / "catalog.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.counts = Counter()
        self.mail_ids: dict[str, str] = {}
        self.attachment_ids: dict[str, str] = {}
        self.unit_ids: dict[str, str] = {}
        self.legacy_ids: dict[str, str] = {}
        self.attachment_records: dict[str, dict] = {}
        self.public_ids: dict[tuple[str, str], str] = {}
        for key, value in {"schema_version": SCHEMA_VERSION, "identity_version": ID_VERSION,
                           "indexed_at": indexed_at, "status": "building"}.items():
            self.set_metadata(key, value)

    def set_metadata(self, key: str, value) -> None:
        self.connection.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json_text(value)))

    def issue(self, doc_id, code: str, path: str, locator: dict, details=None, severity="info") -> None:
        key = stable_id("ISSUE", doc_id or path, code, json_text(locator))
        self.connection.execute("INSERT OR IGNORE INTO quality_issues VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, doc_id, code, severity, path, json_text(locator), json_text(details or {})))

    def exclude(self, reason: str, path: str, source_id: str, locator: dict, row: dict) -> None:
        key = stable_id("EXCLUDED", path, source_id, json_text(locator))
        self.connection.execute("INSERT INTO excluded_records VALUES (?, ?, ?, ?, ?, ?)",
            (key, reason, path, source_id, json_text(locator), json_text(row)))

    def add_source(self, doc_id: str, role: str, source_id: str, version: str,
                   path: str, locator: dict, row: dict, text=None, metadata=None) -> None:
        key = stable_id("SOURCE", doc_id, role, path, source_id, json_text(locator))
        text_hash = digest_text(text) if isinstance(text, str) else None
        self.connection.execute("INSERT OR IGNORE INTO source_map VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, doc_id, role, source_id, version, path, json_text(locator), digest_text(json_text(row)),
             text_hash, json_text(metadata or {})))

    def add_tags(self, doc_id: str, labels: list, origin: str) -> None:
        if not isinstance(labels, list):
            raise CatalogError("topics_must_be_list")
        for label in labels:
            key, canonical = canonical_topic(label)
            self.connection.execute("INSERT OR IGNORE INTO tags VALUES (?, ?)", (key, canonical))
            self.connection.execute("INSERT OR IGNORE INTO document_tags VALUES (?, ?, ?)", (doc_id, key, origin))

    def document(self, *, kind: str, identity: tuple[str, ...], source_type: str, source_id: str,
                 version: str, path: str, locator: dict, row: dict, title: str, text: str,
                 metadata=None, evidence=None, file_format=None, extraction=None,
                 content_status=None, topics=None, topic_origin="source_metadata") -> str:
        doc_id = stable_id(kind, *identity)
        original_hash = digest_text(text)
        clean, operations = normalize_text(text)
        clean_hash = digest_text(clean)
        title_clean, fallback_used = normalize_title(title, {
            "MAIL": "无标题邮件", "CASE": "无标题案例", "ATTACHMENT": "附件",
            "KNOWLEDGE": "无标题知识单元", "PUBLIC": "无标题公开资料",
        }[kind])
        content_status = content_status or ("ready" if clean.strip() else "metadata_only")
        duplicate_group = stable_id("GROUP", kind, source_type, clean_hash) if clean.strip() else stable_id("GROUP", doc_id)
        original_metadata = {key: value for key, value in row.items() if key not in ("text", "body")}
        original_metadata.update(metadata or {})
        metadata_json = json_text(original_metadata)
        existing = self.connection.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if existing:
            if (existing["content_id"] != original_hash or existing["metadata_json"] != metadata_json
                    or existing["original_title"] != title):
                raise CatalogError("conflicting_source_identity")
            self.counts["repeated_identical_input_records"] += 1
        else:
            self.connection.execute("INSERT OR IGNORE INTO contents VALUES (?, ?, ?, ?, ?)",
                (original_hash, text, clean if clean != text else None, clean_hash, json_text(operations)))
            values = (doc_id, kind, source_type, source_id, version, path, json_text(locator), title_clean,
                      title, original_hash, duplicate_group, content_status,
                      evidence or row.get("evidence_status") or row.get("source_kind") or "not_independently_verified",
                      int(row.get("independently_verified") is True), file_format, extraction, None,
                      self.indexed_at, metadata_json)
            self.connection.execute("INSERT INTO documents VALUES (" + ",".join("?" for _ in values) + ")", values)
            self.counts["documents"] += 1
            self.counts[kind] += 1
            if operations:
                self.issue(doc_id, "text_layout_normalized", path, locator, {"operations": operations})
            if fallback_used:
                self.issue(doc_id, "missing_title", path, locator)
            if "\ufffd" in text:
                self.issue(doc_id, "unicode_replacement_character", path, locator, severity="warning")
            if clean.strip() and len(clean.strip()) < 20:
                self.issue(doc_id, "short_text_preserved", path, locator, {"characters": len(clean.strip())})
            if kind in ("KNOWLEDGE", "MAIL", "CASE") and not clean.strip():
                self.issue(doc_id, "empty_text_metadata_preserved", path, locator, severity="warning")
            if extraction == "local_ocr":
                ambiguous = sum(bool(re.search(r"\d[ \t]+\d", line)) for line in text.splitlines())
                if ambiguous:
                    self.issue(doc_id, "ocr_spaced_digits_preserved", path, locator,
                               {"ambiguous_lines": ambiguous}, severity="warning")
            if self.counts["documents"] % 5000 == 0:
                self.connection.commit()
                emit("catalog_records", documents=self.counts["documents"])
        self.add_source(doc_id, "primary", source_id, version, path, locator, row, text)
        self.add_tags(doc_id, topics or [], topic_origin)
        return doc_id

    def relate(self, from_id: str, to_id: str, relation: str, *, target_type: str,
               target_source_id: str, path: str, locator: dict, metadata=None) -> None:
        key = stable_id("REL", from_id, to_id, relation)
        if self.connection.execute("SELECT 1 FROM documents WHERE doc_id=?", (to_id,)).fetchone():
            self.connection.execute("INSERT OR IGNORE INTO relationships VALUES (?, ?, ?, ?, ?)",
                (key, from_id, to_id, relation, json_text(metadata or {})))
        else:
            self.connection.execute("INSERT OR IGNORE INTO unresolved_references VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, from_id, target_type, target_source_id, relation, "target_not_in_retained_complete_sources",
                 path, json_text(locator)))

    def import_mail(self) -> None:
        for source in self.mail_sources:
            path, version = source["path"], source["version"]
            for line, row in jsonl_records(self.root / path):
                source_id = row["id"]
                # Overlapping PSTs can expose the same stable mail ID with a
                # different body. Keep both records and disambiguate the later
                # copy by source version instead of failing or overwriting it.
                identity = (source_id,) if source_id not in self.mail_ids else (source_id, version)
                doc_id = self.document(kind="MAIL", identity=identity, source_type="mail",
                    source_id=source_id, version=version, path=path, locator={"line": line}, row=row,
                    title=row["subject"], text=row["text"], topics=row["topics"],
                    extraction="existing_sanitized_text")
                self.mail_ids.setdefault(source_id, doc_id)

    def import_cases(self) -> None:
        path = f"{self.case_source['base']}/cases.jsonl"
        sqlite_path = f"{self.case_source['base']}/knowledge.sqlite3"
        version = self.case_source['version']
        uri = (self.root / sqlite_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as source_db:
            source_db.execute("PRAGMA query_only=ON")
            for line, row in jsonl_records(self.root / path):
                source_id = row["source_id"]
                saved = source_db.execute("SELECT body FROM cases WHERE source_id=?", (source_id,)).fetchone()
                if not saved:
                    raise CatalogError("case_missing_sqlite_body")
                body = saved[0]
                sections = []
                for key, label in FIELD_LABELS.items():
                    quotes = row["fields"].get(key, [])
                    for quote in quotes:
                        start, end = quote["start"], quote["end"]
                        if (not isinstance(start, int) or not isinstance(end, int)
                                or not 0 <= start <= end <= len(body) or body[start:end] != quote["text"]):
                            raise CatalogError("case_quote_offset_mismatch")
                        self.counts["verbatim_quotes_checked"] += 1
                    if quotes:
                        sections.append("## " + label + "\n" + "\n".join("- " + quote["text"] for quote in quotes))
                locator = {"line": line}
                case_id = self.document(kind="CASE", identity=(source_id,), source_type="case", source_id=source_id,
                    version=version, path=path, locator=locator, row=row, title=row["subject"], text=body,
                    metadata={"field_offsets_reference": "contents.original_text", "body_source": sqlite_path},
                    topics=row["topics"], extraction="existing_case_body")
                self.add_source(case_id, "case_fulltext", source_id, version, sqlite_path,
                    {"table": "cases", "source_id": source_id}, {"source_id": source_id, "body": body}, body)
                self.relate(case_id, stable_id("MAIL", source_id), "DERIVED_FROM", target_type="MAIL",
                    target_source_id=source_id, path=path, locator=locator)
                knowledge_id = self.document(kind="KNOWLEDGE", identity=("case", source_id), source_type="case",
                    source_id=source_id, version=version, path=path, locator=locator, row=row,
                    title=row["subject"], text="\n\n".join(sections), topics=row["topics"],
                    metadata={"text_derivation": "rendered_existing_verbatim_fields", "field_offsets_reference":
                              "related_case.contents.original_text"}, extraction="existing_verbatim_fields")
                self.relate(knowledge_id, case_id, "EXTRACTED_FROM", target_type="CASE", target_source_id=source_id,
                    path=path, locator=locator)
                self.legacy_ids["case_" + source_id] = knowledge_id

    def import_attachments(self) -> None:
        records = {}
        document_parents = {}
        source_documents = []
        for source in self.attachment_sources:
            base, version = source["base"], source["version"]
            path = f"{base}/documents.jsonl"
            for line, row in jsonl_records(self.root / path):
                source_id = row["document_id"]
                locator = {"line": line}
                if (version, source_id) in document_parents:
                    raise CatalogError("duplicate_attachment_manifest_record")
                document_parents[(version, source_id)] = set(row.get("parent_source_ids", []))
                source_documents.append((version, path, line, row))
                if source_id not in records:
                    records[source_id] = row
                    self.attachment_records[source_id] = row
                    kind = row.get("format", "unknown").lower()
                    doc_id = self.document(kind="ATTACHMENT", identity=(source_id,), source_type="attachment",
                        source_id=source_id, version=version, path=path, locator=locator, row=row,
                        title="附件 · " + kind.upper(), text="", file_format=kind,
                        content_status="metadata_only")
                    self.attachment_ids[source_id] = doc_id
                    if row.get("status") != "processed":
                        self.issue(doc_id, "historical_attachment_parse_failure", path, locator,
                                   {"status": row.get("status")}, severity="warning")
                    elif not row.get("exported_units"):
                        self.issue(doc_id, "no_engineering_text_units", path, locator)
                else:
                    if records[source_id].get("format") != row.get("format"):
                        raise CatalogError("conflicting_attachment_format")
                    self.add_source(self.attachment_ids[source_id], "source_variant", source_id,
                                    version, path, locator, row,
                                    metadata={key: row[key] for key in
                                              ("status", "exported_units", "counts", "parser_stats")
                                              if key in row})

        unit_counts = Counter()
        seen_units = {}
        source_units = set()
        for source in self.attachment_sources:
            base, version = source["base"], source["version"]
            path = f"{base}/engineering_candidates.jsonl"
            for line, row in jsonl_records(self.root / path):
                source_id, parent = row["id"], row["document_id"]
                locator = {"line": line, "unit_id": source_id}
                expected_parents = document_parents.get((version, parent))
                if expected_parents is None:
                    raise CatalogError("attachment_unit_missing_source_manifest")
                if set(row["parent_source_ids"]) != expected_parents:
                    raise CatalogError("attachment_parent_manifest_mismatch")
                if (version, source_id) in source_units:
                    raise CatalogError("duplicate_attachment_unit_record")
                source_units.add((version, source_id))
                unit_counts[(version, parent)] += 1
                identity = ("attachment", source_id)
                if source_id in seen_units:
                    first = seen_units[source_id]
                    if first["document_id"] != parent:
                        raise CatalogError("conflicting_attachment_unit_parent")
                    if all(first.get(key) == row.get(key) for key in
                           ("text", "subject", "extraction_method", "source_coverage")):
                        doc_id = self.unit_ids[source_id]
                        self.add_source(doc_id, "source_variant", source_id,
                                        version, path, locator, row, text=row["text"])
                        self.add_tags(doc_id, row.get("topics", []), "source_metadata")
                        self.add_tags(doc_id, row.get("document_topics", []), "source_document_metadata")
                        continue
                    identity = ("attachment", source_id, version)
                else:
                    seen_units[source_id] = row
                manifest = records.get(parent)
                doc_id = self.document(kind="KNOWLEDGE", identity=identity,
                    source_type="attachment", source_id=source_id, version=version, path=path,
                    locator=locator, row=row, title=row["subject"], text=row["text"],
                    topics=row["topics"], file_format=manifest.get("format") if manifest else None,
                    extraction=row["extraction_method"])
                self.add_tags(doc_id, row.get("document_topics", []), "source_document_metadata")
                self.unit_ids.setdefault(source_id, doc_id)
                self.legacy_ids.setdefault("attachment_" + source_id, doc_id)
                self.relate(doc_id, stable_id("ATTACHMENT", parent), "EXTRACTED_FROM",
                    target_type="ATTACHMENT", target_source_id=parent, path=path, locator=locator)
        for version, path, line, row in source_documents:
            if unit_counts[(version, row["document_id"])] != row["exported_units"]:
                raise CatalogError("attachment_unit_count_mismatch")

        occurrences = defaultdict(lambda: {"ordinals": set(), "occurrences": 0,
                                           "versions": set()})
        source_parents = defaultdict(set)
        for source in self.attachment_sources:
            base, version = source["base"], source["version"]
            path = f"{base}/occurrences.jsonl"
            for line, row in jsonl_records(self.root / path):
                key = (row["document_id"], row["parent_source_id"])
                if (version, key[0]) not in document_parents:
                    raise CatalogError("occurrence_missing_attachment_document")
                info = occurrences[key]
                info["ordinals"].add(row["attachment_ordinal"])
                info["occurrences"] += 1
                info.setdefault("line", line)
                info.setdefault("path", path)
                info["versions"].add(version)
                source_parents[(version, key[0])].add(key[1])
                self.counts["attachment_occurrences"] += 1
        for version, path, line, row in source_documents:
            source_id = row["document_id"]
            if source_parents.get((version, source_id), set()) != document_parents[(version, source_id)]:
                raise CatalogError("attachment_occurrence_parent_set_mismatch")
        parents = defaultdict(set)
        for (source_id, parent), info in sorted(occurrences.items()):
            parents[source_id].add(parent)
            metadata = {"attachment_ordinals": sorted(info["ordinals"]),
                        "occurrences": info["occurrences"]}
            if len(self.attachment_sources) > 1:
                metadata["source_versions"] = sorted(info["versions"])
            self.relate(self.attachment_ids[source_id], stable_id("MAIL", parent), "ATTACHED_TO",
                target_type="MAIL", target_source_id=parent, path=info["path"],
                locator={"line": info["line"]}, metadata=metadata)
        for source_id, record in records.items():
            if not set(record["parent_source_ids"]) <= parents[source_id]:
                raise CatalogError("attachment_occurrence_parent_set_mismatch")
        self.connection.execute("""INSERT OR IGNORE INTO document_tags
            SELECT r.to_id, t.tag_id, 'child_unit_aggregate' FROM relationships r
            JOIN documents d ON d.doc_id=r.to_id AND d.type='ATTACHMENT'
            JOIN document_tags t ON t.doc_id=r.from_id WHERE r.relation='EXTRACTED_FROM'""")

    def import_public(self) -> None:
        for version, filename, ordinal_key in (("v1", "pages.jsonl", "page"), ("v2", "units.jsonl", "unit")):
            base = "data/public_lamination_" + version
            manifest_path = base + "/manifest.json"
            manifests = read_json(self.root / manifest_path)
            for position, row in enumerate(manifests):
                source_id = row["id"]
                doc_id = self.document(kind="PUBLIC", identity=(version, source_id), source_type="public_reference",
                    source_id=source_id, version="public_lamination_" + version, path=manifest_path,
                    locator={"array_index": position}, row=row, title=row.get("title", ""), text="",
                    evidence="published_reference_not_independently_verified", content_status="metadata_only")
                self.public_ids[(version, source_id)] = doc_id
            by_id = {row["id"]: row for row in manifests}
            path = base + "/" + filename
            for line, row in jsonl_records(self.root / path):
                parent = row["source_id"]
                number = str(row[ordinal_key])
                source_id = parent + ":" + number
                locator = {"line": line, ordinal_key: row[ordinal_key], "source_id": parent}
                if not row["text"].strip():
                    self.exclude("empty_public_text", path, source_id, locator, row)
                    continue
                if parent not in by_id:
                    raise CatalogError("public_unit_missing_source_manifest")
                source = by_id[parent]
                doc_id = self.document(kind="KNOWLEDGE", identity=("public", version, parent, number),
                    source_type="public_reference", source_id=source_id, version="public_lamination_" + version,
                    path=path, locator=locator, row=row,
                    title=source.get("title", "") + " · " + str(row.get("locator", row[ordinal_key])), text=row["text"],
                    metadata={"public_source": source, "locator_type": row.get("locator_type", "page")},
                    evidence="published_reference_not_independently_verified", extraction="existing_public_text",
                    topics=infer_public_topics(source.get("title", "") + "\n" + row["text"]),
                    topic_origin="documented_keyword_rule")
                self.relate(doc_id, self.public_ids[(version, parent)], "EXTRACTED_FROM", target_type="PUBLIC",
                    target_source_id=parent, path=path, locator=locator)
                self.legacy_ids[f"public_{version}_{parent}_{number}"] = doc_id
        self.connection.execute("""INSERT OR IGNORE INTO document_tags
            SELECT r.to_id, t.tag_id, 'child_unit_aggregate' FROM relationships r
            JOIN documents d ON d.doc_id=r.to_id AND d.type='PUBLIC'
            JOIN document_tags t ON t.doc_id=r.from_id WHERE r.relation='EXTRACTED_FROM'""")

    def import_source_aliases(self) -> None:
        versions = (
            ("data/attachments_v1/documents.jsonl", "attachments_v1", "document_id", self.attachment_ids, "previous_version"),
            ("data/attachments_v1/engineering_candidates.jsonl", "attachments_v1", "id", self.unit_ids, "previous_version"),
            ("data/combined_sources_v1/engineering_candidates.jsonl", "combined_sources_v1", "id",
             {**self.mail_ids, **self.unit_ids}, "training_derivative"),
        )
        for path, version, identity_key, mapping, role in versions:
            if not (self.root / path).is_file():
                continue
            for line, row in jsonl_records(self.root / path):
                source_id = row[identity_key]
                doc_id = mapping.get(source_id)
                if not doc_id:
                    payload = row if self.historical_exclusions == "full_record" else {
                        "source_id": source_id, "record_representation": "provenance_only",
                        "source_record_sha256": digest_text(json_text(row)), "text_imported": False,
                    }
                    self.exclude("historical_record_without_current_identity", path, source_id, {"line": line}, payload)
                    continue
                metadata = {"imported_as_additional_document": False}
                if isinstance(row.get("text"), str):
                    current_hash = self.connection.execute("SELECT content_id FROM documents WHERE doc_id=?", (doc_id,)).fetchone()[0]
                    equal = digest_text(row["text"]) == current_hash
                    metadata["text_matches_current_source"] = equal
                    self.counts[role + ("_text_identical" if equal else "_text_different")] += 1
                if "ocr_ambiguous_numeric_lines_excluded" in row:
                    metadata["historical_ocr_lines_excluded"] = row["ocr_ambiguous_numeric_lines_excluded"]
                self.add_source(doc_id, role, source_id, version, path, {"line": line}, row, row.get("text"), metadata)

        for bundle in ("cases", "attachments", "public"):
            base = Path("dist/dify_export_20261011_complete") / bundle
            manifest = base / "manifest.jsonl"
            if not (self.root / manifest).is_file():
                continue
            for line, row in jsonl_records(self.root / manifest):
                doc_id = self.legacy_ids.get(row["document_id"])
                if not doc_id:
                    raise CatalogError("legacy_markdown_has_no_current_identity")
                path = (self.root / base / row["path"].replace("\\", "/")).resolve()
                if not path.is_relative_to((self.root / base / "documents").resolve()):
                    raise CatalogError("legacy_markdown_path_outside_documents")
                if digest_file(path) != row["sha256"]:
                    raise CatalogError("legacy_markdown_checksum_mismatch")
                self.add_source(doc_id, "legacy_markdown", row["document_id"], "dify_export_20261011_complete",
                    path.relative_to(self.root).as_posix(), {"manifest": manifest.as_posix(), "line": line}, row,
                    metadata={"original_markdown_sha256": row["sha256"], "imported_as_additional_document": False})
                self.counts["legacy_markdown_files_verified"] += 1

    def build_search_index(self) -> None:
        # Reuse the existing offline Chinese/domain tokenizer; this never opens source databases.
        from pcb_knowledge import tokens

        emit("indexing_started", documents=self.counts["documents"])
        cursor = self.connection.execute("""SELECT d.doc_id, d.title, d.content_id,
            COALESCE(c.normalized_text, c.original_text) AS body
            FROM documents d JOIN contents c USING(content_id) ORDER BY d.content_id, d.doc_id""")
        last_content = body_tokens = None
        indexed = 0
        for row in cursor:
            if row["content_id"] != last_content:
                body_tokens = " ".join(tokens(row["body"]))
                last_content = row["content_id"]
            tags = [r[0] for r in self.connection.execute("""SELECT DISTINCT t.label
                FROM document_tags dt JOIN tags t USING(tag_id) WHERE dt.doc_id=? ORDER BY t.label""", (row["doc_id"],))]
            self.connection.execute("INSERT INTO document_search VALUES (?, ?, ?, ?)",
                (row["doc_id"], " ".join(tokens(row["title"])), body_tokens, " ".join(tokens(" ".join(tags)))))
            indexed += 1
            if indexed % 2500 == 0:
                emit("indexing_progress", indexed=indexed, total=self.counts["documents"])
        self.connection.execute("INSERT INTO document_search(document_search) VALUES ('optimize')")
        self.connection.execute("INSERT INTO document_search(document_search) VALUES ('integrity-check')")
        self.connection.commit()

    def statistics(self) -> dict:
        connection = self.connection
        totals = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in (
            "documents", "contents", "relationships", "source_map", "unresolved_references", "excluded_records")}
        by_type = [dict(row) for row in connection.execute("""SELECT type, source_type, COUNT(*) AS documents,
            SUM(content_status='ready') AS documents_with_text FROM documents GROUP BY type, source_type ORDER BY type, source_type""")]
        duplicates = [dict(row) for row in connection.execute("""SELECT type, source_type,
            COUNT(*) AS duplicate_groups, SUM(members-1) AS extra_records FROM
            (SELECT type, source_type, duplicate_group_id, COUNT(*) AS members FROM documents
             WHERE content_status='ready' GROUP BY type, source_type, duplicate_group_id HAVING COUNT(*)>1)
            GROUP BY type, source_type ORDER BY type, source_type""")]
        report = {
            "schema_version": SCHEMA_VERSION, "status": "completed", "indexed_at": self.indexed_at,
            "identity_version": ID_VERSION, "totals": totals, "documents_by_type": by_type,
            "duplicate_content": duplicates,
            "relationships_by_type": dict(connection.execute("SELECT relation,COUNT(*) FROM relationships GROUP BY relation")),
            "source_maps_by_role": dict(connection.execute("SELECT role,COUNT(*) FROM source_map GROUP BY role")),
            "quality_issue_counts": dict(connection.execute("SELECT code,COUNT(*) FROM quality_issues GROUP BY code")),
            "excluded_by_reason": dict(connection.execute("SELECT reason,COUNT(*) FROM excluded_records GROUP BY reason")),
            "unresolved_target_counts": [dict(row) for row in connection.execute("""SELECT target_type,
                COUNT(DISTINCT target_source_id) AS unique_missing_targets,COUNT(*) AS missing_references,
                COUNT(DISTINCT from_id) AS affected_documents FROM unresolved_references GROUP BY target_type""")],
            "tag_counts": [dict(row) for row in connection.execute("""SELECT t.tag_id,t.label,d.type,
                COUNT(DISTINCT d.doc_id) AS documents FROM tags t JOIN document_tags dt USING(tag_id)
                JOIN documents d ON d.doc_id=dt.doc_id GROUP BY t.tag_id,t.label,d.type ORDER BY t.tag_id,d.type""")],
            "source_created_at_unknown": connection.execute("SELECT COUNT(*) FROM documents WHERE source_created_at IS NULL").fetchone()[0],
            "processing": dict(self.counts), "original_assets_modified": False,
            "content_deduplication": "shared_content_storage_and_query_time_grouping_sources_retained",
            "timestamp_policy": "source_dates_remain_null_when_unknown_indexed_at_is_catalog_ingestion_time",
            "text_policy": "layout_only_no_numeric_correction_no_semantic_rewriting_no_ocr_line_deletion",
        }
        return report

    def export(self) -> dict:
        self.set_metadata("status", "completed")
        statistics = self.statistics()
        self.set_metadata("statistics", statistics)
        self.connection.commit()
        with write_jsonl_gzip(self.staging / "catalog.jsonl.gz") as stream:
            for row in self.connection.execute("""SELECT d.*, COALESCE(c.normalized_text,c.original_text) AS text,
                c.normalized_sha256, c.normalization_json FROM documents d JOIN contents c USING(content_id) ORDER BY doc_id"""):
                entry = dict(row)
                for key in ("locator", "metadata", "normalization"):
                    entry[key] = json.loads(entry.pop(key + "_json"))
                entry["independently_verified"] = bool(entry["independently_verified"])
                entry["original_text_sha256"] = entry.pop("content_id")
                entry["tags"] = [dict(tag) for tag in self.connection.execute("""SELECT t.tag_id,t.label,dt.origin
                    FROM document_tags dt JOIN tags t USING(tag_id) WHERE dt.doc_id=? ORDER BY t.tag_id,dt.origin""", (row["doc_id"],))]
                stream.write(json_text(entry) + "\n")
        for table, filename, order in (
            ("source_map", "source_map.jsonl.gz", "mapping_id"),
            ("relationships", "relationships.jsonl.gz", "relationship_id"),
            ("unresolved_references", "unresolved_references.jsonl.gz", "reference_id"),
            ("quality_issues", "quality_issues.jsonl.gz", "issue_id"),
            ("excluded_records", "excluded_records.jsonl.gz", "exclusion_id"),
        ):
            with write_jsonl_gzip(self.staging / filename) as stream:
                for row in self.connection.execute("SELECT * FROM " + table + " ORDER BY " + order):
                    entry = dict(row)
                    for key in list(entry):
                        if key.endswith("_json"):
                            entry[key[:-5]] = json.loads(entry.pop(key))
                    stream.write(json_text(entry) + "\n")
        with write_jsonl_gzip(self.staging / "duplicate_groups.jsonl.gz") as stream:
            for row in self.connection.execute("""SELECT duplicate_group_id,type,source_type,COUNT(*) AS members
                FROM documents WHERE content_status='ready' GROUP BY duplicate_group_id,type,source_type
                HAVING COUNT(*)>1 ORDER BY duplicate_group_id"""):
                entry = dict(row)
                entry["doc_ids"] = [r[0] for r in self.connection.execute(
                    "SELECT doc_id FROM documents WHERE duplicate_group_id=? ORDER BY doc_id", (row["duplicate_group_id"],))]
                stream.write(json_text(entry) + "\n")
        write_json(self.staging / "taxonomy.json", {"version": 1, "topics": TOPICS,
                   "inference": "Keyword rules apply only to public reference texts; all tag origins are recorded."})
        write_json(self.staging / "report.json", statistics)
        return statistics


def write_readme(staging: Path, report: dict) -> None:
    rows = ["| 类型 | 来源 | 记录 | 有正文 |", "| --- | --- | ---: | ---: |"]
    rows += [f"| {row['type']} | {row['source_type']} | {row['documents']} | {row['documents_with_text']} |"
             for row in report["documents_by_type"]]
    duplicate_rows = ["| 类型 | 来源 | 重复组 | 可合并展示的额外记录 |", "| --- | --- | ---: | ---: |"]
    duplicate_rows += [f"| {row['type']} | {row['source_type']} | {row['duplicate_groups']} | {row['extra_records']} |"
                       for row in report["duplicate_content"]]
    issue_rows = ["| 质量标记 | 记录数 |", "| --- | ---: |"]
    issue_rows += [f"| `{key}` | {value} |" for key, value in report["quality_issue_counts"].items()]
    text = "\n".join([
        "# Phase 3 清洗整理结果", "", "本目录由现有脱敏邮件、案例、附件提取文本和公开资料派生。", "",
        "## 数据口径", "", *rows, "",
        "Mail、Case、Attachment、Knowledge 是不同实体；PUBLIC 保存公开资料的文件级出处。",
        "案例全文与知识摘录分开，旧版本、训练合并数据与 Markdown 只增加来源映射，不增加文档数量。", "",
        "## 去重结果", "", *duplicate_rows, "",
        "相同正文共享内容存储；原记录、来源、标签和关系全部保留。检索默认在当前筛选结果内合并重复正文，",
        "不同实体类型和来源类型不会互相折叠。源标识生成的稳定 ID 不随排序、换机器或新增其他记录变化。", "",
        "## 质量问题", "", *issue_rows, "",
        "缺失关系详见 unresolved_references.jsonl.gz；没有补造邮件。空公开页写入 excluded_records.jsonl.gz，原始来源保留。",
        "短文本、原解析失败及没有工程文本的附件仍保留元数据；OCR 数字间空格仅标注，不拼接或删行。", "",
        "## 清洗边界", "",
        "正文只处理换行、开头 BOM、行尾空格、多余空行和不可见控制符。保留制表符、大小写、全角字符、数值、单位和原文。",
        "SQLite contents 表保存原始正文；normalized_text 仅在有布局变化时填写。案例 fields 中的字符位置始终指向原案例正文。",
        "未知来源日期保持空值，indexed_at 是本轮收录时间，不是邮件发送时间。公开资料的许可和发布者等元数据原样保留。", "",
        "## 文件", "",
        "- catalog.sqlite3：统一目录、原文/规范正文、标签、来源、关系、全文检索。",
        "- catalog.jsonl.gz：可直接消费的规范记录与正文（UTF-8 JSONL，gzip 压缩）。",
        "- source_map.jsonl.gz：原标识、历史版本、训练变体及旧 Markdown 到统一 ID 的映射。",
        "- relationships.jsonl.gz：已解析来源关系；unresolved_references.jsonl.gz：缺失来源。",
        "- duplicate_groups.jsonl.gz、quality_issues.jsonl.gz、excluded_records.jsonl.gz：去重和质量明细。",
        "- report.json、source_snapshots.json、validation.json、artifact_manifest.json：统计、原资产不变证明和产物校验。", "",
        "## 使用", "", "在项目根目录执行（如本目录不是默认 v1，增加 --catalog 参数）：", "", "```powershell",
        "python -X utf8 -B knowledge_catalog.py status",
        'python -X utf8 -B knowledge_catalog.py search "分层" --type KNOWLEDGE',
        "python -X utf8 -B knowledge_catalog.py validate",
        "python -X utf8 -B knowledge_catalog.py versions",
        "python -X utf8 -B knowledge_catalog.py batches",
        "python -X utf8 -B knowledge_catalog.py growth",
        "python -X utf8 -B knowledge_catalog.py diff VER_FROM_ID VER_TO_ID", "",
        "# 将新来源导入另一个独立派生目录，不修改本目录",
        "python -X utf8 -B knowledge_catalog.py ingest --manifest data/new_source/manifest.json --output data/phase3_catalog_v3",
        "```", "",
        "Catalog 通过 python -X utf8 -B knowledge_catalog.py search 或 knowledge_catalog.py chat 在本机查询。来源版本、批次、映射和快照位于 versions/；首次增量导入会在新的派生目录中补建基线版本，不修改本目录或原始来源。", "",
        "growth 在无版本登记时返回 registered=false、status=not_registered、reconciliation_status=not_registered 和 reconciliation=null；已登记时通过 reconciliation_status 表示三项对账是否全部匹配。", "",
    ])
    (staging / "README.md").write_text(text, encoding="utf-8", newline="\n")


def build(root: Path = ROOT, output: Path | None = None, mail_sources=None,
          attachment_sources=None, case_source=None, historical_exclusions="full_record") -> dict:
    if historical_exclusions not in ("full_record", "provenance_only"):
        raise CatalogError("invalid_historical_exclusions_mode")
    root = root.resolve()
    output = checked_output(root, output or root / "data/phase3_catalog_v1")
    emit("source_verification_started")
    before = source_snapshots(root, mail_sources, attachment_sources, case_source)
    verify_source_reports(root, before, mail_sources, attachment_sources, case_source)
    staging = Path(tempfile.mkdtemp(prefix=".phase3_catalog_build_", dir=root / "data")).resolve()
    if staging.parent != (root / "data").resolve() or not staging.is_relative_to(root):
        raise CatalogError("invalid_staging_directory")
    builder = None
    try:
        indexed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        builder = Builder(root, staging, indexed_at, mail_sources, attachment_sources, case_source,
                          historical_exclusions)
        builder.import_mail()
        builder.import_cases()
        builder.import_attachments()
        builder.import_public()
        emit("source_mapping_started", documents=builder.counts["documents"])
        builder.import_source_aliases()
        builder.build_search_index()
        emit("export_started")
        report = builder.export()
        builder.connection.close()
        builder = None
        validation = validate_database(staging)
        if validation["status"] != "passed":
            raise CatalogError("catalog_database_validation_failed")
        after = source_snapshots(root, mail_sources, attachment_sources, case_source)
        if before != after:
            raise CatalogError("input_assets_changed_during_build")
        write_json(staging / "source_snapshots.json", {"unchanged": True, "before": before, "after": after})
        write_json(staging / "validation.json", validation)
        baseline_fingerprint = digest_file(staging / "catalog.sqlite3")
        baseline_version = VersionRegistry(
            version_id="VER_" + digest_text("phase3-catalog-baseline\n" + baseline_fingerprint)[:24].upper(),
            parent_version_id=None,
            source_name=output.name,
            source_type="catalog",
            fingerprint=baseline_fingerprint,
            created_at=indexed_at,
            status="active",
            source_version="baseline",
        )
        write_version_registry(staging, baseline_version)
        totals = report.get("totals", {})
        batch = ImportBatch(
            batch_id="BATCH_BASELINE_20261015",
            version_id=baseline_version.version_id,
            source_records=int(totals.get("documents", 0)),
            new_entities=int(totals.get("documents", 0)),
            existing_entities=0,
            new_relations=int(totals.get("relationships", 0)),
            duplicate_records=int(totals.get("duplicate_records", 0)),
            missing_links=sum(
                int(item.get("missing_references", 0))
                for item in report.get("unresolved_target_counts", [])
            ),
            started_at=indexed_at,
            completed_at=indexed_at,
        )
        write_import_batch(staging, batch)
        write_entity_mapping(staging, {
            "entity_id": "CATALOG_BASELINE",
            "version_id": baseline_version.version_id,
            "source_record_id": "phase3_catalog_v1",
        })
        write_relation_mapping(staging, {
            "relationship_id": "RELATIONSHIP_BASELINE",
            "version_id": baseline_version.version_id,
        })
        write_fingerprint_mapping(staging, {
            "fingerprint": baseline_version.fingerprint,
            "entity_id": "CATALOG_BASELINE",
        })
        paths = version_paths(staging)
        paths["snapshots"].mkdir(parents=True, exist_ok=True)
        write_json(
            paths["snapshots"] / (baseline_version.version_id + ".json"),
            {
                "version_id": baseline_version.version_id,
                "document_count": totals.get("documents", 0),
                "relationship_count": totals.get("relationships", 0),
                "unresolved_references": sum(
                    int(item.get("missing_references", 0))
                    for item in report.get("unresolved_target_counts", [])
                ),
                "indexed_at": indexed_at,
                "source_name": baseline_version.source_name,
                "source_version": baseline_version.source_version,
                "batch_id": batch.batch_id,
            },
        )
        write_readme(staging, report)
        artifacts = {path.name: {"bytes": path.stat().st_size, "sha256": digest_file(path)}
                     for path in sorted(staging.iterdir()) if path.is_file()}
        write_json(staging / "artifact_manifest.json", {"schema_version": 1, "artifacts": artifacts})
        # Both absolute paths are checked before publishing the new generated directory.
        checked_output(root, output)
        if staging.parent != output.parent or not staging.is_relative_to(root / "data"):
            raise CatalogError("unsafe_publish_path")
        staging.rename(output)
        emit("catalog_completed", output=str(output), totals=report["totals"], validation=validation["status"])
        return report
    except Exception as exc:
        if builder is not None:
            builder.connection.close()
        failure = {"status": "failed", "error_type": type(exc).__name__,
                   "error_code": str(exc) if isinstance(exc, CatalogError) else "build_failed",
                   "published": False}
        write_json(staging / "BUILD_FAILED.json", failure)
        emit("catalog_build_failed", staging=str(staging), **failure)
        raise
