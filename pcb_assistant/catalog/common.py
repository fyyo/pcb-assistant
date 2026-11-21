"""Stable identities, conservative text normalization and the Phase 3 schema."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, asdict
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3


from pcb_assistant.paths import ROOT
DEFAULT_CATALOG = ROOT / "data/phase3_catalog_v9"
SCHEMA_VERSION = 1
ID_VERSION = "pcb-catalog-id-v1"
DOCUMENT_TYPES = ("MAIL", "CASE", "ATTACHMENT", "KNOWLEDGE", "PUBLIC")
SOURCE_TYPES = ("mail", "case", "attachment", "public_reference")
TOPICS = {
    "lamination": {"label": "压合", "aliases": ["压合", "壓合", "层压", "層壓", "lamination"]},
    "stackup_materials": {"label": "叠构与材料", "aliases": ["叠构与材料", "疊構與材料", "stackup_materials"]},
    "defects_reliability": {"label": "缺陷与可靠性", "aliases": ["缺陷与可靠性", "缺陷與可靠性", "defects_reliability"]},
    "temperature_cure": {"label": "温度与固化", "aliases": ["温度与固化", "溫度與固化", "temperature_cure"]},
    "pressure_vacuum": {"label": "压力与真空", "aliases": ["压力与真空", "壓力與真空", "pressure_vacuum"]},
    "quality_validation": {"label": "质量与验证", "aliases": ["质量与验证", "質量與驗證", "quality_validation"]},
}
TOPIC_LOOKUP = {alias.casefold(): key for key, topic in TOPICS.items()
                for alias in [key, *topic["aliases"]]}
PUBLIC_TOPIC_PATTERNS = {
    "lamination": r"压合|壓合|层压|層壓|\blaminat(?:ion|ing|e|es|ed)\b",
    "stackup_materials": r"半固化|叠构|疊構|铜箔|銅箔|\bprepreg\b|\bstack.?up\b|\bcopper foil\b",
    "defects_reliability": r"分层|分層|爆板|白斑|翘曲|翹曲|\bdelamination\b|\bwarpage\b|\bmeasling\b|\bblister(?:ing)?\b",
    "temperature_cure": r"固化|保温|保溫|升温|升溫|\bcuring\b|\bcure cycle\b|\bheating rate\b",
    "pressure_vacuum": r"真空|压力|壓力|\bvacuum\b|\bpress pressure\b",
    "quality_validation": r"剥离|剝離|切片|热冲击|熱衝擊|\bpeel strength\b|\bmicrosection\b|\bthermal shock\b",
}
FIELD_LABELS = {
    "phenomenon": "问题现象", "materials": "材料与叠构", "conditions": "工艺条件",
    "analysis": "分析线索", "actions": "处理措施线索", "verification": "验证记录提及",
    "limitations": "适用边界与不确定性",
}
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")

VERSIONS_DIR = "versions"
CATALOG_JSONL_EXPORT_BASES = (
    "catalog.jsonl",
    "source_map.jsonl",
    "relationships.jsonl",
    "unresolved_references.jsonl",
    "quality_issues.jsonl",
    "excluded_records.jsonl",
    "duplicate_groups.jsonl",
)
CATALOG_JSONL_EXPORTS = tuple(name + ".gz" for name in CATALOG_JSONL_EXPORT_BASES)
LEGACY_CATALOG_JSONL_EXPORTS = CATALOG_JSONL_EXPORT_BASES

@dataclass(frozen=True)
class VersionRegistry:
    version_id: str
    parent_version_id: str | None
    source_name: str
    source_type: str
    fingerprint: str
    created_at: str
    status: str = "active"
    source_version: str | None = None

@dataclass(frozen=True)
class ImportBatch:
    batch_id: str
    version_id: str
    source_records: int
    new_entities: int
    existing_entities: int
    new_relations: int
    duplicate_records: int
    missing_links: int
    started_at: str
    completed_at: str
    resolved_references: int = 0

def version_paths(catalog_dir: Path) -> dict[str, Path]:
    root = Path(catalog_dir) / VERSIONS_DIR
    return {
        "root": root,
        "registry": root / "version_registry.jsonl",
        "batches": root / "import_batches.jsonl",
        "snapshots": root / "snapshots",
    }

def append_jsonl(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
        stream.write("\n")


@contextmanager
def write_jsonl_gzip(path: Path):
    """Write a deterministic UTF-8 JSONL export without a path or timestamp header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as stream:
                yield stream


@contextmanager
def open_jsonl_text(path: Path):
    """Open a plain or gzip-compressed JSONL file as text."""
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8-sig") as stream:
            yield stream
    else:
        with path.open("r", encoding="utf-8-sig") as stream:
            yield stream

def write_version_registry(catalog_dir: Path, version: VersionRegistry) -> None:
    append_jsonl(version_paths(catalog_dir)["registry"], asdict(version))

def write_import_batch(catalog_dir: Path, batch: ImportBatch) -> None:
    append_jsonl(version_paths(catalog_dir)["batches"], asdict(batch))

def write_version_snapshot(catalog_dir: Path, version_id: str, snapshot: dict) -> None:
    paths = version_paths(catalog_dir)
    paths["snapshots"].mkdir(parents=True, exist_ok=True)
    target = paths["snapshots"] / (version_id + ".json")
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

def write_entity_mapping(catalog_dir: Path, mapping: dict) -> None:
    append_jsonl(version_paths(catalog_dir)["root"] / "entity_mappings.jsonl", mapping)

def write_relation_mapping(catalog_dir: Path, mapping: dict) -> None:
    append_jsonl(version_paths(catalog_dir)["root"] / "relation_mappings.jsonl", mapping)

def write_fingerprint_mapping(catalog_dir: Path, mapping: dict) -> None:
    append_jsonl(version_paths(catalog_dir)["root"] / "fingerprint_index.jsonl", mapping)


class CatalogError(ValueError):
    """Messages are fixed error codes, never source text."""


_VERSION_ID_PATTERN = re.compile(r"VER_[A-Za-z0-9][A-Za-z0-9_-]{0,149}")
_BATCH_ID_PATTERN = re.compile(r"BATCH_[A-Za-z0-9][A-Za-z0-9_-]{0,149}")
_VERSION_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")


def is_version_id(value) -> bool:
    return isinstance(value, str) and bool(_VERSION_ID_PATTERN.fullmatch(value))


def _metadata_error(errors: list[dict], code: str, filename: str, line: int | None = None) -> None:
    entry = {"code": code, "file": filename}
    if line is not None:
        entry["line"] = line
    errors.append(entry)


def _read_version_jsonl(path: Path, filename: str, errors: list[dict]) -> list[dict]:
    if not path.is_file():
        _metadata_error(errors, "version_metadata_file_missing", filename)
        return []
    rows = []
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    _metadata_error(errors, "invalid_version_metadata_jsonl", filename, line_number)
                    continue
                if not isinstance(value, dict):
                    _metadata_error(errors, "version_metadata_record_must_be_object", filename, line_number)
                    continue
                rows.append(value)
    except (OSError, UnicodeError):
        _metadata_error(errors, "version_metadata_file_unreadable", filename)
    return rows


def _is_nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_nonnegative_integer(value) -> bool:
    return type(value) is int and value >= 0


def validate_version_metadata(catalog_dir: Path) -> dict:
    """Validate optional version registry, import batches and snapshots.

    Catalogs produced before version registration existed have no ``versions``
    directory.  That is a valid legacy state rather than a validation failure.
    The function returns fixed, source-text-free error codes suitable for both
    the CLI and the local read-only retrieval layer.
    """
    paths = version_paths(Path(catalog_dir))
    if not paths["root"].is_dir():
        return {"status": "not_present", "versions": 0, "batches": 0,
                "snapshots": 0, "errors": []}

    errors: list[dict] = []
    registry_rows = _read_version_jsonl(paths["registry"], "version_registry.jsonl", errors)
    batch_rows = _read_version_jsonl(paths["batches"], "import_batches.jsonl", errors)

    versions: dict[str, dict] = {}
    registry_indexes: dict[str, int] = {}
    source_versions: set[tuple[str, str]] = set()
    for index, row in enumerate(registry_rows, 1):
        version_id = row.get("version_id")
        if not isinstance(version_id, str) or not _VERSION_ID_PATTERN.fullmatch(version_id):
            _metadata_error(errors, "invalid_version_id", "version_registry.jsonl", index)
            continue
        if version_id in versions:
            _metadata_error(errors, "duplicate_version_id", "version_registry.jsonl", index)
            continue
        versions[version_id] = row
        registry_indexes[version_id] = index
        source_name = row.get("source_name")
        source_version = row.get("source_version")
        if not _is_nonempty_text(source_name):
            _metadata_error(errors, "invalid_source_name", "version_registry.jsonl", index)
        if not _is_nonempty_text(source_version):
            _metadata_error(errors, "invalid_source_version", "version_registry.jsonl", index)
        else:
            key = (source_name if isinstance(source_name, str) else "", source_version)
            if key in source_versions:
                _metadata_error(errors, "duplicate_source_version", "version_registry.jsonl", index)
            source_versions.add(key)
        source_type = row.get("source_type")
        if source_type not in (*SOURCE_TYPES, "catalog"):
            _metadata_error(errors, "invalid_version_source_type", "version_registry.jsonl", index)
        fingerprint = row.get("fingerprint")
        if not isinstance(fingerprint, str) or not _VERSION_FINGERPRINT_PATTERN.fullmatch(fingerprint):
            _metadata_error(errors, "invalid_version_fingerprint", "version_registry.jsonl", index)
        if not _is_nonempty_text(row.get("created_at")):
            _metadata_error(errors, "invalid_version_created_at", "version_registry.jsonl", index)
        if not _is_nonempty_text(row.get("status")):
            _metadata_error(errors, "invalid_version_status", "version_registry.jsonl", index)
        parent = row.get("parent_version_id")
        if parent is not None and (not isinstance(parent, str) or not _VERSION_ID_PATTERN.fullmatch(parent)):
            _metadata_error(errors, "invalid_parent_version_id", "version_registry.jsonl", index)

    for index, row in enumerate(registry_rows, 1):
        parent = row.get("parent_version_id")
        if isinstance(parent, str) and _VERSION_ID_PATTERN.fullmatch(parent) and parent not in versions:
            _metadata_error(errors, "parent_version_not_registered", "version_registry.jsonl", index)

    reported_cycles: set[tuple[str, ...]] = set()
    for version_id in versions:
        path_ids: list[str] = []
        positions: dict[str, int] = {}
        current = version_id
        while current in versions and current not in positions:
            positions[current] = len(path_ids)
            path_ids.append(current)
            parent = versions[current].get("parent_version_id")
            if not isinstance(parent, str) or not _VERSION_ID_PATTERN.fullmatch(parent):
                current = None
                break
            current = parent
        if current is not None and current in positions:
            cycle = tuple(sorted(path_ids[positions[current]:]))
            if cycle not in reported_cycles:
                reported_cycles.add(cycle)
                _metadata_error(errors, "version_parent_cycle", "version_registry.jsonl",
                                registry_indexes.get(current))

    batch_versions: dict[str, str] = {}
    for index, row in enumerate(batch_rows, 1):
        batch_id = row.get("batch_id")
        if not isinstance(batch_id, str) or not _BATCH_ID_PATTERN.fullmatch(batch_id):
            _metadata_error(errors, "invalid_batch_id", "import_batches.jsonl", index)
        elif batch_id in batch_versions:
            _metadata_error(errors, "duplicate_batch_id", "import_batches.jsonl", index)
        version_id = row.get("version_id")
        if not isinstance(version_id, str) or not _VERSION_ID_PATTERN.fullmatch(version_id):
            _metadata_error(errors, "invalid_batch_version_id", "import_batches.jsonl", index)
        elif version_id not in versions:
            _metadata_error(errors, "batch_version_not_registered", "import_batches.jsonl", index)
        elif isinstance(batch_id, str) and _BATCH_ID_PATTERN.fullmatch(batch_id) and batch_id not in batch_versions:
            batch_versions[batch_id] = version_id
        for key in ("source_records", "new_entities", "existing_entities", "new_relations",
                    "duplicate_records", "missing_links", "resolved_references"):
            value = row.get(key, 0 if key == "resolved_references" else None)
            if not _is_nonnegative_integer(value):
                _metadata_error(errors, "invalid_batch_count", "import_batches.jsonl", index)
        for key in ("started_at", "completed_at"):
            if not _is_nonempty_text(row.get(key)):
                _metadata_error(errors, "invalid_batch_timestamp", "import_batches.jsonl", index)

    snapshot_count = 0
    for version_id, version_row in versions.items():
        snapshot_path = paths["snapshots"] / (version_id + ".json")
        if not snapshot_path.is_file():
            _metadata_error(errors, "version_snapshot_missing", "snapshots", None)
            continue
        try:
            snapshot = read_json(snapshot_path)
        except (OSError, ValueError, UnicodeError):
            _metadata_error(errors, "invalid_version_snapshot", "snapshots")
            continue
        if not isinstance(snapshot, dict):
            _metadata_error(errors, "version_snapshot_must_be_object", "snapshots")
            continue
        snapshot_count += 1
        if snapshot.get("version_id") != version_id:
            _metadata_error(errors, "version_snapshot_id_mismatch", "snapshots")
        for key in ("document_count", "relationship_count", "unresolved_references"):
            if not _is_nonnegative_integer(snapshot.get(key)):
                _metadata_error(errors, "invalid_version_snapshot_count", "snapshots")
        for key, code in (
            ("source_name", "version_snapshot_source_name_mismatch"),
            ("source_version", "version_snapshot_source_version_mismatch"),
        ):
            if snapshot.get(key) != version_row.get(key):
                _metadata_error(errors, code, "snapshots")
        if not _is_nonempty_text(snapshot.get("indexed_at")):
            _metadata_error(errors, "invalid_version_snapshot_timestamp", "snapshots")
        batch_id = snapshot.get("batch_id")
        if not isinstance(batch_id, str) or not _BATCH_ID_PATTERN.fullmatch(batch_id):
            _metadata_error(errors, "invalid_version_snapshot_batch_id", "snapshots")
        elif batch_id not in batch_versions:
            _metadata_error(errors, "version_snapshot_batch_not_registered", "snapshots")
        elif batch_versions[batch_id] != version_id:
            _metadata_error(errors, "version_snapshot_batch_version_mismatch", "snapshots")

    return {"status": "failed" if errors else "passed", "versions": len(versions),
            "batches": len(batch_rows), "snapshots": snapshot_count, "errors": errors}

def json_text(value, *, pretty=False) -> str:
    """Serialize stable UTF-8 JSON for identities, fingerprints and exports."""
    options = {"ensure_ascii": False, "sort_keys": True, "allow_nan": False}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return json.dumps(value, **options)


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stable_id(kind: str, *keys: str) -> str:
    if not keys or any(not isinstance(key, str) or not key for key in keys):
        raise CatalogError("invalid_identity_key")
    return kind + "_" + digest_text(json_text([ID_VERSION, kind, *keys]))[:32]


def normalize_text(text: str) -> tuple[str, list[str]]:
    """Only layout/control cleanup; numbers, units, tabs and source wording survive."""
    if not isinstance(text, str):
        raise CatalogError("text_must_be_string")
    flags = []
    normalized = text
    if normalized.startswith("\ufeff"):
        normalized = normalized.lstrip("\ufeff")
        flags.append("leading_bom_removed")
    if "\r" in normalized:
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        flags.append("line_endings_normalized")
    if CONTROL_CHARACTERS.search(normalized):
        # Preserve a separator rather than joining engineering values across a control.
        normalized = CONTROL_CHARACTERS.sub(" ", normalized)
        flags.append("control_characters_replaced_with_space")
    trimmed = "\n".join(line.rstrip(" ") for line in normalized.split("\n"))
    if trimmed != normalized:
        flags.append("line_end_spaces_removed")
        normalized = trimmed
    bounded = re.sub(r"\n{3,}", "\n\n", normalized).strip("\n")
    if bounded != normalized:
        flags.append("blank_lines_normalized")
    return bounded, flags


def normalize_title(title: str, fallback: str) -> tuple[str, bool]:
    if not isinstance(title, str):
        raise CatalogError("title_must_be_string")
    title = CONTROL_CHARACTERS.sub(" ", title.lstrip("\ufeff"))
    title = re.sub(r"\s+", " ", title).strip()
    return (title, False) if title else (fallback, True)


def canonical_topic(label: str) -> tuple[str, str]:
    if not isinstance(label, str):
        raise CatalogError("topic_must_be_string")
    label = re.sub(r"\s+", " ", label).strip()
    if not label:
        raise CatalogError("empty_topic")
    key = TOPIC_LOOKUP.get(label.casefold())
    return (key, TOPICS[key]["label"]) if key else (stable_id("topic", label), label)


def infer_public_topics(text: str) -> list[str]:
    return [key for key, pattern in PUBLIC_TOPIC_PATTERNS.items() if re.search(pattern, text, re.I)]


def jsonl_records(path: Path):
    with open_jsonl_text(path) as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise CatalogError("invalid_source_jsonl") from exc
            if not isinstance(row, dict):
                raise CatalogError("source_record_must_be_object")
            yield line_number, row


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def connect_readonly(directory: Path) -> sqlite3.Connection:
    path = Path(directory).resolve() / "catalog.sqlite3"
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if not row or json.loads(row[0]) != SCHEMA_VERSION:
            raise CatalogError("unsupported_catalog_schema")
    except Exception:
        connection.close()
        raise
    return connection


SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=DELETE;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE contents (
    content_id TEXT PRIMARY KEY, original_text TEXT NOT NULL, normalized_text TEXT,
    normalized_sha256 TEXT NOT NULL, normalization_json TEXT NOT NULL
);
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('MAIL','CASE','ATTACHMENT','KNOWLEDGE','PUBLIC')),
    source_type TEXT NOT NULL, source_id TEXT NOT NULL, source_version TEXT NOT NULL,
    source_path TEXT NOT NULL, locator_json TEXT NOT NULL,
    title TEXT NOT NULL, original_title TEXT NOT NULL,
    content_id TEXT NOT NULL REFERENCES contents(content_id), duplicate_group_id TEXT NOT NULL,
    content_status TEXT NOT NULL, evidence_status TEXT NOT NULL,
    independently_verified INTEGER NOT NULL CHECK(independently_verified IN (0,1)),
    format TEXT, extraction_method TEXT, source_created_at TEXT, indexed_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX document_type ON documents(type, source_type);
CREATE INDEX document_group ON documents(duplicate_group_id, doc_id);
CREATE INDEX document_content ON documents(content_id);
CREATE TABLE tags (tag_id TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE document_tags (
    doc_id TEXT NOT NULL REFERENCES documents(doc_id), tag_id TEXT NOT NULL REFERENCES tags(tag_id),
    origin TEXT NOT NULL, PRIMARY KEY(doc_id, tag_id, origin)
);
CREATE INDEX tag_documents ON document_tags(tag_id, doc_id);
CREATE TABLE source_map (
    mapping_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(doc_id),
    role TEXT NOT NULL, source_id TEXT NOT NULL, source_version TEXT NOT NULL,
    source_path TEXT NOT NULL, locator_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
    text_sha256 TEXT, metadata_json TEXT NOT NULL
);
CREATE INDEX source_document ON source_map(doc_id, role);
CREATE INDEX source_identity ON source_map(source_version, source_id);
CREATE TABLE relationships (
    relationship_id TEXT PRIMARY KEY, from_id TEXT NOT NULL REFERENCES documents(doc_id),
    to_id TEXT NOT NULL REFERENCES documents(doc_id), relation TEXT NOT NULL,
    metadata_json TEXT NOT NULL, UNIQUE(from_id, to_id, relation)
);
CREATE INDEX relationships_from ON relationships(from_id, relation);
CREATE INDEX relationships_to ON relationships(to_id, relation);
CREATE TABLE unresolved_references (
    reference_id TEXT PRIMARY KEY, from_id TEXT NOT NULL REFERENCES documents(doc_id),
    target_type TEXT NOT NULL, target_source_id TEXT NOT NULL, relation TEXT NOT NULL,
    reason TEXT NOT NULL, source_path TEXT NOT NULL, locator_json TEXT NOT NULL
);
CREATE TABLE quality_issues (
    issue_id TEXT PRIMARY KEY, doc_id TEXT REFERENCES documents(doc_id), code TEXT NOT NULL,
    severity TEXT NOT NULL, source_path TEXT NOT NULL, locator_json TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX quality_document ON quality_issues(doc_id, code);
CREATE TABLE excluded_records (
    exclusion_id TEXT PRIMARY KEY, reason TEXT NOT NULL, source_path TEXT NOT NULL,
    source_id TEXT NOT NULL, locator_json TEXT NOT NULL, original_record_json TEXT NOT NULL
);
CREATE VIRTUAL TABLE document_search USING fts5(
    doc_id UNINDEXED, title_tokens, body_tokens, tag_tokens
);
"""


def validate_database(directory: Path) -> dict:
    with closing(connect_readonly(directory)) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        documents = {row[0] for row in connection.execute("SELECT doc_id FROM documents")}
        search_ids = [row[0] for row in connection.execute("SELECT doc_id FROM document_search")]
        source_gaps = connection.execute("""SELECT COUNT(*) FROM documents d WHERE NOT EXISTS
            (SELECT 1 FROM source_map s WHERE s.doc_id=d.doc_id AND s.role='primary')""").fetchone()[0]
        invalid_hashes = 0
        for row in connection.execute("SELECT * FROM contents"):
            clean, flags = normalize_text(row["original_text"])
            stored = row["normalized_text"] if row["normalized_text"] is not None else row["original_text"]
            if (digest_text(row["original_text"]) != row["content_id"] or clean != stored
                    or digest_text(stored) != row["normalized_sha256"]
                    or flags != json.loads(row["normalization_json"])):
                invalid_hashes += 1
        result = {"integrity_check": integrity, "foreign_key_errors": foreign_keys,
                  "documents": len(documents), "search_rows": len(search_ids),
                  "search_references_match": documents == set(search_ids),
                  "duplicate_search_ids": len(search_ids) - len(set(search_ids)),
                  "documents_without_primary_source": source_gaps, "invalid_content_hashes": invalid_hashes}
        result["status"] = "passed" if (integrity == ["ok"] and not foreign_keys
            and documents == set(search_ids) and len(search_ids) == len(documents)
            and not source_gaps and not invalid_hashes) else "failed"
        return result
