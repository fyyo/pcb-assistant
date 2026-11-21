"""Register source versions and ingest incremental JSONL into independent catalogs."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid

from pcb_assistant.catalog.build import Builder, checked_output, emit
from pcb_assistant.catalog.common import (
    CATALOG_JSONL_EXPORTS, CatalogError, DOCUMENT_TYPES, ImportBatch,
    LEGACY_CATALOG_JSONL_EXPORTS, SOURCE_TYPES, VersionRegistry,
    digest_file, digest_text, json_text, jsonl_records, read_json, stable_id,
    validate_database, validate_version_metadata, version_paths, write_entity_mapping,
    write_fingerprint_mapping, write_import_batch, write_relation_mapping, write_version_registry,
    write_version_snapshot,
)


RELATION_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
SIMPLE_ID_TYPES = {"MAIL", "CASE", "ATTACHMENT"}
DEFAULT_PARENT_TYPE = {"mail": "MAIL", "case": "CASE", "attachment": "ATTACHMENT",
                       "public_reference": "PUBLIC"}
DEFAULT_RELATION = {"ATTACHMENT": "ATTACHED_TO", "CASE": "DERIVED_FROM",
                    "KNOWLEDGE": "EXTRACTED_FROM", "PUBLIC": "EXTRACTED_FROM"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_json_replace(path: Path, value) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise CatalogError("version_metadata_record_must_be_object")
                rows.append(value)
    return rows


def _infer_root(catalog: Path) -> Path:
    catalog = catalog.resolve()
    return catalog.parent.parent if catalog.parent.name == "data" else catalog.parent


def _required_text(value, code: str, *, limit=1000) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or CONTROL_PATTERN.search(value):
        raise CatalogError(code)
    return value


def _safe_source_path(value, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CatalogError(code)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise CatalogError(code)
    return path.as_posix()


def load_manifest(manifest_path: Path, root: Path | None = None) -> dict:
    """Read and validate the small source manifest before copying the base catalog."""
    manifest_path = Path(manifest_path).resolve()
    if not manifest_path.is_file():
        raise CatalogError("source_manifest_not_found")
    if root is not None and not manifest_path.is_relative_to(root.resolve()):
        raise CatalogError("source_manifest_outside_project")
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError) as exc:
        raise CatalogError("invalid_source_manifest") from exc
    if not isinstance(manifest, dict):
        raise CatalogError("source_manifest_must_be_object")
    source_name = _required_text(manifest.get("source_name"), "invalid_source_name", limit=200)
    source_type = manifest.get("source_type")
    if source_type not in SOURCE_TYPES:
        raise CatalogError("invalid_source_type")
    source_version = _required_text(manifest.get("source_version"), "invalid_source_version", limit=200)
    fingerprint = manifest.get("fingerprint")
    if not isinstance(fingerprint, str) or not FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise CatalogError("invalid_source_fingerprint")
    records_name = _safe_source_path(manifest.get("records"), "invalid_records_path")
    records_path = (manifest_path.parent / records_name).resolve()
    if not records_path.is_relative_to(manifest_path.parent) or not records_path.is_file():
        raise CatalogError("source_records_not_found_or_outside_manifest")
    if root is not None and not records_path.is_relative_to(root.resolve()):
        raise CatalogError("source_records_outside_project")
    if digest_file(records_path) != fingerprint:
        raise CatalogError("source_fingerprint_mismatch")
    return {"manifest": manifest, "manifest_path": manifest_path, "records_path": records_path,
            "source_name": source_name, "source_type": source_type,
            "source_version": source_version, "fingerprint": fingerprint}


def _metadata(value, code: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CatalogError(code)
    try:
        json_text(value)
    except (TypeError, ValueError) as exc:
        raise CatalogError(code) from exc
    return value


def _relation_rows(row: dict, manifest: dict, line: int) -> list[dict]:
    relations = row.get("relationships", [])
    if relations is None:
        relations = []
    if not isinstance(relations, list):
        raise CatalogError("relationships_must_be_list")
    result = []
    for item in relations:
        if not isinstance(item, dict):
            raise CatalogError("relationship_must_be_object")
        target_type = item.get("target_type")
        if target_type not in DOCUMENT_TYPES:
            raise CatalogError("invalid_relationship_target_type")
        target_source_id = _required_text(item.get("target_source_id"),
                                          "invalid_relationship_target_source_id", limit=500)
        relation = item.get("relation")
        if not isinstance(relation, str) or not RELATION_PATTERN.fullmatch(relation):
            raise CatalogError("invalid_relationship_type")
        target_source_type = item.get("target_source_type")
        if target_source_type is not None and target_source_type not in SOURCE_TYPES:
            raise CatalogError("invalid_relationship_target_source_type")
        result.append({"target_type": target_type, "target_source_id": target_source_id,
                       "target_source_type": target_source_type, "relation": relation,
                       "metadata": _metadata(item.get("metadata"), "invalid_relationship_metadata"),
                       "line": line})

    parents = row.get("parent_source_ids", [])
    if parents is None:
        parents = []
    if not isinstance(parents, list):
        raise CatalogError("parent_source_ids_must_be_list")
    entity_type = row["entity_type"]
    parent_type = row.get("parent_target_type") or DEFAULT_PARENT_TYPE[manifest["source_type"]]
    if parent_type not in DOCUMENT_TYPES:
        raise CatalogError("invalid_parent_target_type")
    relation = DEFAULT_RELATION.get(entity_type)
    if parents and not relation:
        raise CatalogError("parent_relation_required")
    for parent in parents:
        target_source_id = _required_text(parent, "invalid_parent_source_id", limit=500)
        result.append({"target_type": parent_type, "target_source_id": target_source_id,
                       "target_source_type": None, "relation": relation,
                       "metadata": {"parent_link": True}, "line": line})
    unique = {}
    for item in result:
        key = (item["target_type"], item["target_source_id"], item["target_source_type"], item["relation"])
        unique.setdefault(key, item)
    return list(unique.values())


def load_records(manifest: dict) -> tuple[list[dict], int, int]:
    """Validate all records and collapse exact duplicate identities before any writes."""
    records = []
    seen = {}
    duplicate_records = 0
    total_records = 0
    for line, row in jsonl_records(manifest["records_path"]):
        total_records += 1
        entity_type = row.get("entity_type")
        if entity_type not in DOCUMENT_TYPES:
            raise CatalogError("invalid_entity_type")
        source_id = _required_text(row.get("source_id"), "invalid_source_record_id", limit=500)
        title = row.get("title")
        if not isinstance(title, str):
            raise CatalogError("record_title_must_be_string")
        text = row.get("text")
        if not isinstance(text, str):
            raise CatalogError("record_text_must_be_string")
        topics = row.get("topics", [])
        if not isinstance(topics, list) or any(not isinstance(topic, str) or not topic for topic in topics):
            raise CatalogError("topics_must_be_nonempty_string_list")
        metadata = _metadata(row.get("metadata"), "invalid_record_metadata")
        evidence = row.get("evidence_status")
        if evidence is not None and not isinstance(evidence, str):
            raise CatalogError("evidence_status_must_be_string")
        independently_verified = row.get("independently_verified")
        if independently_verified is not None and type(independently_verified) is not bool:
            raise CatalogError("independently_verified_must_be_boolean")
        content_status = row.get("content_status")
        if content_status not in (None, "ready", "metadata_only"):
            raise CatalogError("invalid_content_status")
        file_format = row.get("file_format")
        extraction = row.get("extraction_method")
        for value, code in ((file_format, "file_format_must_be_string"),
                            (extraction, "extraction_method_must_be_string")):
            if value is not None and not isinstance(value, str):
                raise CatalogError(code)
        normalized = {
            "entity_type": entity_type, "source_id": source_id, "title": title, "text": text,
            "topics": topics, "metadata": metadata, "evidence_status": evidence,
            "independently_verified": independently_verified, "content_status": content_status,
            "file_format": file_format, "extraction_method": extraction,
            "relationships": _relation_rows(row, manifest, line), "line": line, "raw": row,
        }
        key = (entity_type, source_id)
        raw_fingerprint = json_text(row)
        if key in seen:
            if seen[key]["raw_fingerprint"] != raw_fingerprint:
                raise CatalogError("conflicting_duplicate_source_identity")
            duplicate_records += 1
            continue
        normalized["raw_fingerprint"] = raw_fingerprint
        seen[key] = normalized
        records.append(normalized)
    if not records:
        raise CatalogError("source_has_no_records")
    return records, total_records, duplicate_records


def _identity(record: dict, manifest: dict) -> tuple[str, ...]:
    entity_type = record["entity_type"]
    if entity_type in SIMPLE_ID_TYPES:
        return (record["source_id"],)
    if entity_type == "KNOWLEDGE":
        return (manifest["source_type"], record["source_id"])
    return (manifest["source_name"], record["source_id"])

class EntityIndex:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.by_source = defaultdict(set)
        for row in connection.execute("""SELECT d.doc_id,d.type,d.source_type,s.source_id
            FROM source_map s JOIN documents d ON d.doc_id=s.doc_id"""):
            self.add(row["type"], row["source_id"], row["doc_id"], row["source_type"])
        for row in connection.execute("SELECT doc_id,type,source_type,source_id FROM documents"):
            self.add(row["type"], row["source_id"], row["doc_id"], row["source_type"])

    def add(self, entity_type: str, source_id: str, doc_id: str, source_type: str | None = None) -> None:
        self.by_source[(entity_type, source_id)].add((doc_id, source_type))

    def matching_doc_ids(self, entity_type: str, source_id: str,
                         source_type: str | None = None) -> set[str]:
        candidates = self.by_source.get((entity_type, source_id), set())
        if source_type is not None:
            candidates = {item for item in candidates if item[1] == source_type}
        return {item[0] for item in candidates}

    def resolve(self, target_type: str, target_source_id: str,
                target_source_type: str | None = None) -> str | None:
        if target_type in SIMPLE_ID_TYPES:
            candidate = stable_id(target_type, target_source_id)
            if self.connection.execute("SELECT 1 FROM documents WHERE doc_id=?", (candidate,)).fetchone():
                return candidate
        doc_ids = self.matching_doc_ids(target_type, target_source_id, target_source_type)
        return next(iter(doc_ids)) if len(doc_ids) == 1 else None


def _count_snapshot(connection: sqlite3.Connection) -> dict:
    return {
        "document_count": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "relationship_count": connection.execute("SELECT COUNT(*) FROM relationships").fetchone()[0],
        "unresolved_references": connection.execute("SELECT COUNT(*) FROM unresolved_references").fetchone()[0],
    }


def _ensure_baseline(staging: Path, connection: sqlite3.Connection, base_name: str) -> None:
    paths = version_paths(staging)
    registry = _read_jsonl(paths["registry"])
    if registry:
        return
    metadata = {row[0]: json.loads(row[1]) for row in connection.execute("SELECT key,value FROM metadata")}
    indexed_at = metadata.get("indexed_at") or _now()
    fingerprint = digest_file(staging / "catalog.sqlite3")
    version_id = "VER_" + digest_text("phase3-catalog-baseline\n" + fingerprint)[:24].upper()
    counts = _count_snapshot(connection)
    duplicate_extra = connection.execute("""SELECT COALESCE(SUM(members-1),0) FROM
        (SELECT COUNT(*) AS members FROM documents WHERE content_status='ready'
         GROUP BY type,source_type,duplicate_group_id HAVING COUNT(*)>1)""").fetchone()[0]
    version = VersionRegistry(
        version_id=version_id, parent_version_id=None, source_name=base_name,
        source_type="catalog", fingerprint=fingerprint, created_at=indexed_at, status="active",
        source_version="baseline")
    batch = ImportBatch(
        batch_id="BATCH_BASELINE_" + digest_text(version_id + "\n" + fingerprint)[:24].upper(),
        version_id=version_id, source_records=counts["document_count"],
        new_entities=counts["document_count"], existing_entities=0,
        new_relations=counts["relationship_count"], duplicate_records=duplicate_extra,
        missing_links=counts["unresolved_references"], started_at=indexed_at, completed_at=indexed_at)
    write_version_registry(staging, version)
    write_import_batch(staging, batch)
    write_version_snapshot(staging, version_id, {
        "version_id": version_id, **counts, "indexed_at": indexed_at,
        "source_name": base_name, "source_version": "baseline", "batch_id": batch.batch_id,
        "new_entities": batch.new_entities, "existing_entities": 0,
        "new_relations": batch.new_relations, "resolved_references": 0,
        "missing_links": batch.missing_links,
    })
    write_entity_mapping(staging, {"entity_id": "CATALOG_BASELINE", "version_id": version_id,
                                   "source_record_id": base_name})
    write_relation_mapping(staging, {"relationship_id": "RELATIONSHIP_BASELINE", "version_id": version_id})
    write_fingerprint_mapping(staging, {"fingerprint": fingerprint, "entity_id": "CATALOG_BASELINE"})


def _register_version(staging: Path, manifest: dict, created_at: str) -> tuple[VersionRegistry, bool]:
    paths = version_paths(staging)
    registry = _read_jsonl(paths["registry"])
    matches = [row for row in registry if row.get("source_name") == manifest["source_name"]
               and row.get("source_version") == manifest["source_version"]]
    if len(matches) > 1:
        raise CatalogError("duplicate_source_version_registry")
    if matches:
        if matches[0].get("fingerprint") != manifest["fingerprint"]:
            raise CatalogError("source_version_fingerprint_conflict")
        if matches[0].get("source_type") != manifest["source_type"]:
            raise CatalogError("source_version_type_conflict")
        existing = dict(matches[0])
        existing.setdefault("source_version", manifest["source_version"])
        try:
            return VersionRegistry(**existing), False
        except TypeError as exc:
            raise CatalogError("invalid_version_registry") from exc
    parent = registry[-1]["version_id"] if registry else None
    version_id = "VER_" + digest_text(manifest["source_name"] + "\n" + manifest["source_version"])[:24].upper()
    if any(row.get("version_id") == version_id for row in registry):
        raise CatalogError("version_id_collision")
    version = VersionRegistry(
        version_id=version_id, parent_version_id=parent, source_name=manifest["source_name"],
        source_type=manifest["source_type"], fingerprint=manifest["fingerprint"],
        created_at=created_at, status="active", source_version=manifest["source_version"])
    write_version_registry(staging, version)
    return version, True


def _new_batch_id(existing: list[dict]) -> str:
    known = {row.get("batch_id") for row in existing}
    while True:
        value = "BATCH_" + uuid.uuid4().hex.upper()
        if value not in known:
            return value


def _existing_document(connection: sqlite3.Connection, doc_id: str):
    return connection.execute("""SELECT d.*,c.original_text FROM documents d
        JOIN contents c USING(content_id) WHERE d.doc_id=?""", (doc_id,)).fetchone()


def _source_metadata(manifest: dict, line: int) -> dict:
    return {"source_name": manifest["source_name"], "source_version": manifest["source_version"],
            "manifest": manifest["manifest_path"].name, "line": line}


def _import_entities(connection: sqlite3.Connection, staging: Path, root: Path,
                     manifest: dict, records: list[dict], indexed_at: str,
                     version: VersionRegistry) -> tuple[Counter, EntityIndex, dict]:
    worker = object.__new__(Builder)
    worker.root, worker.staging, worker.indexed_at = root, staging, indexed_at
    worker.connection = connection
    worker.counts = Counter()
    report_row = connection.execute("SELECT value FROM metadata WHERE key='statistics'").fetchone()
    if report_row:
        worker.counts.update(json.loads(report_row[0]).get("processing", {}))
    worker.counts["documents"] = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    for entity_type in DOCUMENT_TYPES:
        worker.counts[entity_type] = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE type=?", (entity_type,)).fetchone()[0]

    index = EntityIndex(connection)
    before_documents = worker.counts["documents"]
    existing_entities = 0
    record_ids: dict[tuple[str, str], str] = {}
    records_path = manifest["records_path"].relative_to(root).as_posix()
    manifest_path = manifest["manifest_path"].relative_to(root).as_posix()
    for record in records:
        entity_type, source_id = record["entity_type"], record["source_id"]
        canonical_id = stable_id(entity_type, *_identity(record, manifest))
        doc_id = canonical_id
        existing = _existing_document(connection, canonical_id)
        if existing is None:
            aliases = index.matching_doc_ids(entity_type, source_id, manifest["source_type"])
            if len(aliases) > 1:
                raise CatalogError("ambiguous_source_identity")
            if aliases:
                doc_id = next(iter(aliases))
                existing = _existing_document(connection, doc_id)
        if existing is not None:
            if existing["content_id"] != digest_text(record["text"]) or existing["original_title"] != record["title"]:
                raise CatalogError("conflicting_source_identity")
            existing_entities += 1
            worker.counts["incremental_existing_entities"] += 1
        else:
            doc_id = worker.document(
                kind=entity_type, identity=_identity(record, manifest), source_type=manifest["source_type"],
                source_id=source_id, version=manifest["source_version"], path=records_path,
                locator={"line": record["line"], "manifest": manifest_path}, row=record["raw"],
                title=record["title"], text=record["text"],
                metadata={**record["metadata"], "incremental_manifest": manifest_path},
                evidence=record["evidence_status"], file_format=record["file_format"],
                extraction=record["extraction_method"], content_status=record["content_status"],
                topics=record["topics"])
        record_ids[(entity_type, source_id)] = doc_id
        worker.add_source(doc_id, "incremental_version", source_id, manifest["source_version"],
                          records_path, {"line": record["line"], "manifest": manifest_path},
                          record["raw"], record["text"], _source_metadata(manifest, record["line"]))
        worker.add_tags(doc_id, record["topics"], "source_metadata")
        index.add(entity_type, source_id, doc_id, manifest["source_type"])
        write_entity_mapping(staging, {
            "entity_id": doc_id, "version_id": version.version_id, "source_record_id": source_id,
            "entity_type": entity_type, "source_name": manifest["source_name"],
            "source_version": manifest["source_version"],
            "status": "new" if existing is None else "existing",
        })
        write_fingerprint_mapping(staging, {
            "fingerprint": digest_text(record["text"]), "entity_id": doc_id,
            "source_name": manifest["source_name"], "source_version": manifest["source_version"],
        })
    new_entities = worker.counts["documents"] - before_documents
    return worker, index, {"new_entities": new_entities, "existing_entities": existing_entities,
                           "records_path": records_path, "manifest_path": manifest_path,
                           "record_ids": record_ids, "doc_ids": list(record_ids.values())}


def _reindex_documents(connection: sqlite3.Connection, doc_ids: set[str]) -> None:
    from pcb_assistant.knowledge import tokens

    for doc_id in sorted(doc_ids):
        connection.execute("DELETE FROM document_search WHERE doc_id=?", (doc_id,))
        row = connection.execute("""SELECT d.doc_id,d.title,COALESCE(c.normalized_text,c.original_text) AS body
            FROM documents d JOIN contents c USING(content_id) WHERE d.doc_id=?""", (doc_id,)).fetchone()
        if row is None:
            continue
        tags = [item[0] for item in connection.execute("""SELECT DISTINCT t.label
            FROM document_tags dt JOIN tags t USING(tag_id) WHERE dt.doc_id=? ORDER BY t.label""", (doc_id,))]
        connection.execute("INSERT INTO document_search VALUES (?, ?, ?, ?)",
            (doc_id, " ".join(tokens(row["title"])), " ".join(tokens(row["body"])),
             " ".join(tokens(" ".join(tags)))))

def _insert_relationship(connection: sqlite3.Connection, from_id: str, to_id: str, relation: str,
                         metadata: dict) -> tuple[str, bool]:
    relationship_id = stable_id("REL", from_id, to_id, relation)
    cursor = connection.execute("INSERT OR IGNORE INTO relationships VALUES (?, ?, ?, ?, ?)",
        (relationship_id, from_id, to_id, relation, json_text(metadata)))
    return relationship_id, cursor.rowcount == 1


def _resolve_existing_references(connection: sqlite3.Connection, index: EntityIndex,
                                 staging: Path, version: VersionRegistry,
                                 manifest: dict) -> tuple[int, int]:
    resolved = 0
    new_relations = 0
    rows = list(connection.execute("SELECT * FROM unresolved_references ORDER BY reference_id"))
    for row in rows:
        target_id = index.resolve(row["target_type"], row["target_source_id"])
        if not target_id:
            continue
        metadata = {
            "resolved_from_unresolved_reference": row["reference_id"],
            "source_path": row["source_path"],
            "locator": json.loads(row["locator_json"]),
        }
        relationship_id, inserted = _insert_relationship(
            connection, row["from_id"], target_id, row["relation"], metadata)
        if inserted:
            new_relations += 1
        connection.execute("DELETE FROM unresolved_references WHERE reference_id=?", (row["reference_id"],))
        resolved += 1
        write_relation_mapping(staging, {
            "relationship_id": relationship_id, "version_id": version.version_id,
            "source_name": manifest["source_name"], "source_version": manifest["source_version"],
            "from_id": row["from_id"], "to_id": target_id, "relation": row["relation"],
            "target_source_id": row["target_source_id"], "status": "resolved_existing_reference",
        })
    return resolved, new_relations


def _import_relationships(connection: sqlite3.Connection, staging: Path, index: EntityIndex,
                          version: VersionRegistry, manifest: dict, records: list[dict],
                          record_ids: dict[tuple[str, str], str],
                          records_path: str) -> tuple[int, int, int, int]:
    resolved, resolved_relations = _resolve_existing_references(connection, index, staging, version, manifest)
    new_relations = resolved_relations
    missing_links = 0
    declared = 0
    for record in records:
        from_id = record_ids[(record["entity_type"], record["source_id"])]
        for item in record["relationships"]:
            declared += 1
            target_id = index.resolve(item["target_type"], item["target_source_id"],
                                      item["target_source_type"])
            if target_id:
                relationship_id, inserted = _insert_relationship(
                    connection, from_id, target_id, item["relation"], item["metadata"])
                if inserted:
                    new_relations += 1
                write_relation_mapping(staging, {
                    "relationship_id": relationship_id, "version_id": version.version_id,
                    "source_name": manifest["source_name"], "source_version": manifest["source_version"],
                    "from_id": from_id, "to_id": target_id, "relation": item["relation"],
                    "target_source_id": item["target_source_id"], "line": item["line"],
                    "status": "resolved",
                })
            else:
                placeholder = stable_id(item["target_type"], item["target_source_id"])
                reference_id = stable_id("REL", from_id, placeholder, item["relation"])
                cursor = connection.execute("""INSERT OR IGNORE INTO unresolved_references
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
                    reference_id, from_id, item["target_type"], item["target_source_id"],
                    item["relation"], "target_not_in_retained_complete_sources",
                    records_path,
                    json_text({"line": item["line"], "source_version": manifest["source_version"]})))
                if cursor.rowcount == 1:
                    missing_links += 1
                write_relation_mapping(staging, {
                    "reference_id": reference_id, "version_id": version.version_id,
                    "source_name": manifest["source_name"], "source_version": manifest["source_version"],
                    "from_id": from_id, "target_type": item["target_type"],
                    "target_source_id": item["target_source_id"], "relation": item["relation"],
                    "line": item["line"], "status": "unresolved",
                })
    return new_relations, missing_links, declared, resolved


def _refresh_artifacts(staging: Path) -> dict:
    target = staging / "artifact_manifest.json"
    if target.exists():
        target.unlink()
    artifacts = {}
    for path in sorted(staging.iterdir()):
        if path.is_file() and path.name != "artifact_manifest.json":
            artifacts[path.name] = {"bytes": path.stat().st_size, "sha256": digest_file(path)}
    _write_json_replace(target, {"schema_version": 1, "artifacts": artifacts})
    return artifacts


def ingest(base_catalog: Path, manifest_path: Path, output: Path,
           root: Path | None = None) -> dict:
    """Copy a completed catalog, apply one source manifest and atomically publish a derivative."""
    base = Path(base_catalog).resolve()
    root = Path(root).resolve() if root is not None else _infer_root(base)
    output = checked_output(root, Path(output))
    if not base.is_dir() or not (base / "catalog.sqlite3").is_file():
        raise CatalogError("base_catalog_not_found")
    manifest = load_manifest(Path(manifest_path), root)
    records, source_records, duplicate_records = load_records(manifest)
    base_fingerprint = digest_file(base / "catalog.sqlite3")
    base_mtime = (base / "catalog.sqlite3").stat().st_mtime_ns
    started_at = _now()
    staging = Path(tempfile.mkdtemp(prefix=".phase3_catalog_ingest_", dir=base.parent)).resolve()
    if staging.parent != base.parent or not staging.is_relative_to(root / "data"):
        raise CatalogError("invalid_staging_directory")
    connection = None
    try:
        emit("incremental_copy_started", base=str(base), output=str(output))
        shutil.copytree(base, staging, dirs_exist_ok=True)
        if digest_file(staging / "catalog.sqlite3") != base_fingerprint:
            raise CatalogError("base_catalog_copy_mismatch")
        connection = sqlite3.connect(staging / "catalog.sqlite3")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        _ensure_baseline(staging, connection, base.name)
        version, version_is_new = _register_version(staging, manifest, started_at)
        worker, index, entity_counts = _import_entities(
            connection, staging, root, manifest, records, started_at, version)
        record_ids = entity_counts["record_ids"]
        new_relations, missing_links, declared, resolved = _import_relationships(
            connection, staging, index, version, manifest, records, record_ids,
            entity_counts["records_path"])
        worker.counts["incremental_resolved_references"] += resolved
        touched_ids = set(entity_counts["doc_ids"])
        touched_ids.update(record_ids.values())
        _reindex_documents(connection, touched_ids)
        batches = _read_jsonl(version_paths(staging)["batches"])
        batch = ImportBatch(
            batch_id=_new_batch_id(batches), version_id=version.version_id,
            source_records=source_records, new_entities=entity_counts["new_entities"],
            existing_entities=entity_counts["existing_entities"], new_relations=new_relations,
            duplicate_records=duplicate_records, missing_links=missing_links,
            started_at=started_at, completed_at=_now(),
            resolved_references=resolved)
        write_import_batch(staging, batch)
        counts = _count_snapshot(connection)
        # Keep the snapshot current even when the same source version is
        # ingested again; a batch is evidence of a new import, not a new
        # version.  The version row itself remains immutable.
        write_version_snapshot(staging, version.version_id, {
            "version_id": version.version_id, **counts, "indexed_at": started_at,
            "source_name": manifest["source_name"], "source_version": manifest["source_version"],
            "batch_id": batch.batch_id, "new_entities": batch.new_entities,
            "existing_entities": batch.existing_entities, "new_relations": batch.new_relations,
            "resolved_references": batch.resolved_references, "missing_links": batch.missing_links,
            "new_version": version_is_new,
        })
        connection.commit()
        for name in (*LEGACY_CATALOG_JSONL_EXPORTS, *CATALOG_JSONL_EXPORTS,
                     "taxonomy.json", "report.json"):
            path = staging / name
            if path.exists():
                path.unlink()
        report = worker.export()
        connection.close()
        connection = None
        database_validation = validate_database(staging)
        version_validation = validate_version_metadata(staging)
        validation = {**database_validation, "version_metadata": version_validation}
        if database_validation["status"] != "passed" or version_validation["status"] != "passed":
            validation["status"] = "failed"
        _write_json_replace(staging / "validation.json", validation)
        if validation["status"] != "passed":
            raise CatalogError("catalog_database_validation_failed")
        if digest_file(base / "catalog.sqlite3") != base_fingerprint or (
                base / "catalog.sqlite3").stat().st_mtime_ns != base_mtime:
            raise CatalogError("base_catalog_changed_during_ingest")
        source_snapshot = {
            "manifest": manifest["manifest_path"].relative_to(root).as_posix(),
            "manifest_sha256": digest_file(manifest["manifest_path"]),
            "records": manifest["records_path"].relative_to(root).as_posix(),
            "records_sha256": manifest["fingerprint"], "source_name": manifest["source_name"],
            "source_version": manifest["source_version"], "source_type": manifest["source_type"],
            "base_catalog_sha256": base_fingerprint,
            "base_catalog_mtime_ns": base_mtime, "unchanged": True,
        }
        _write_json_replace(staging / "incremental_source_snapshot.json", source_snapshot)
        ingestion = {
            "status": "completed", "created_at": batch.completed_at,
            "base_catalog": str(base), "output": str(output),
            "version": {"version_id": version.version_id, "parent_version_id": version.parent_version_id,
                        "source_name": version.source_name, "source_version": manifest["source_version"],
                        "source_type": version.source_type, "fingerprint": version.fingerprint,
                        "new_version": version_is_new},
            "batch": {key: getattr(batch, key) for key in batch.__dataclass_fields__},
            "declared_relationships": declared, "totals": report["totals"],
        }
        _write_json_replace(staging / "ingestion.json", ingestion)
        _refresh_artifacts(staging)
        if digest_file(base / "catalog.sqlite3") != base_fingerprint or (
                base / "catalog.sqlite3").stat().st_mtime_ns != base_mtime:
            raise CatalogError("base_catalog_changed_during_ingest")
        if staging.parent != output.parent or not staging.is_relative_to(root / "data"):
            raise CatalogError("unsafe_publish_path")
        staging.rename(output)
        emit("incremental_completed", output=str(output), version_id=version.version_id,
             batch_id=batch.batch_id, new_entities=batch.new_entities,
             existing_entities=batch.existing_entities, new_relations=batch.new_relations,
             resolved_references=batch.resolved_references, missing_links=batch.missing_links)
        return {"status": "completed", "output": str(output), "version": ingestion["version"],
                "batch": ingestion["batch"], "declared_relationships": declared,
                "totals": report["totals"], "validation": validation["status"]}
    except Exception as exc:
        if connection is not None:
            connection.close()
        failure = {"status": "failed", "error_type": type(exc).__name__,
                   "error_code": str(exc) if isinstance(exc, CatalogError) else "ingest_failed",
                   "published": False}
        try:
            _write_json_replace(staging / "INGEST_FAILED.json", failure)
        except OSError:
            pass
        emit("incremental_failed", staging=str(staging), **failure)
        raise
