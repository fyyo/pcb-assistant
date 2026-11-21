"""Isolated OpenViking shadow-retrieval adapter for the PCB catalog.

This module deliberately does not participate in the default CLI, MCP surface,
catalog build, evidence package, or answer pipeline. It has two jobs:

* export a bounded, retrieval-only Markdown projection of the read-only catalog;
* call OpenViking ``find`` and map returned ``viking://`` URIs back to catalog
  document IDs without returning generated summaries or source text.

The catalog remains the source of truth. OpenViking is only a candidate
retriever; any evidence used by an answer must still be read and validated by
the existing catalog evidence layer.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Sequence
import urllib.error
import urllib.parse
import urllib.request

from catalog_common import CatalogError, DEFAULT_CATALOG, connect_readonly


EXPORT_SCHEMA = "pcb-openviking-shadow-export-v1"
MANIFEST_SCHEMA = "pcb-openviking-shadow-document-v1"
FIND_SCHEMA = "pcb-openviking-shadow-find-v1"
VERIFY_SCHEMA = "pcb-openviking-shadow-verify-v1"
DEFAULT_TARGET_URI = "viking://resources/pcb-shadow"
DEFAULT_ENDPOINT = "http://127.0.0.1:1933"
DOCUMENT_TYPES = ("MAIL", "CASE", "ATTACHMENT", "KNOWLEDGE", "PUBLIC")
DOC_ID_RE = re.compile(r"(?:MAIL|CASE|KNOWLEDGE|ATTACHMENT|PUBLIC)_[0-9a-f]{24,32}")
DOC_ID_FULL_RE = re.compile(r"^(?:MAIL|CASE|KNOWLEDGE|ATTACHMENT|PUBLIC)_[0-9a-f]{24,32}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


class ShadowError(ValueError):
    """A fixed, source-text-free adapter error code."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _jsonl_write(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            stream.write("\n")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_title(value: Any, fallback: str) -> str:
    title = CONTROL_RE.sub(" ", str(value or ""))
    title = re.sub(r"\s+", " ", title).strip()
    return title or fallback


def _validate_limit(value: int, *, name: str = "limit", maximum: int = 100) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise ShadowError(f"invalid_{name}")
    return value


def _validate_target_uri(value: str) -> str:
    if not isinstance(value, str):
        raise ShadowError("invalid_target_uri")
    target = value.strip().rstrip("/")
    if not target.startswith("viking://resources/") or ".." in target.split("/"):
        raise ShadowError("invalid_target_uri")
    return target


def _resolve_scope(scope: str, allow_internal: bool) -> str:
    if scope not in {"public", "internal"}:
        raise ShadowError("invalid_scope")
    if scope == "internal" and not allow_internal:
        raise ShadowError("internal_scope_requires_explicit_allow")
    return scope


def _doc_id_from_uri(uri: Any) -> str | None:
    if not isinstance(uri, str):
        return None
    match = DOC_ID_RE.search(uri)
    return match.group(0) if match else None


def _document_types(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    selected = tuple(dict.fromkeys(value.upper() for value in values))
    if any(value not in DOCUMENT_TYPES for value in selected):
        raise ShadowError("invalid_doc_type")
    return selected


def _export_rows(catalog_dir: Path, *, scope: str, limit: int,
                 doc_types: Sequence[str]) -> list[dict]:
    clauses = [
        "d.content_status='ready'",
        "length(trim(COALESCE(c.normalized_text,c.original_text)))>0",
    ]
    parameters: list[Any] = []
    if scope == "public":
        clauses.append("d.source_type=?")
        parameters.append("public_reference")
    if doc_types:
        placeholders = ",".join("?" for _ in doc_types)
        clauses.append(f"d.type IN ({placeholders})")
        parameters.extend(doc_types)
    where = " AND ".join(clauses)
    sql = f"""
        WITH candidates AS (
            SELECT d.doc_id,d.type,d.source_type,d.title,d.content_id,
                   d.source_version,d.evidence_status,d.independently_verified,
                   d.content_status,d.format,d.duplicate_group_id,
                   c.normalized_sha256,
                   COALESCE(c.normalized_text,c.original_text) AS body_text,
                   COUNT(*) OVER (PARTITION BY d.content_id) AS representation_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.content_id
                       ORDER BY CASE d.type
                           WHEN 'KNOWLEDGE' THEN 0
                           WHEN 'CASE' THEN 1
                           WHEN 'PUBLIC' THEN 2
                           WHEN 'MAIL' THEN 3
                           ELSE 4 END,
                           d.doc_id
                   ) AS content_rank
            FROM documents d
            JOIN contents c USING(content_id)
            WHERE {where}
        )
        SELECT doc_id,type,source_type,title,content_id,source_version,
               evidence_status,independently_verified,content_status,format,
               duplicate_group_id,normalized_sha256,body_text,representation_count
        FROM candidates
        WHERE content_rank=1
        ORDER BY type,doc_id
        LIMIT ?
    """
    with closing(connect_readonly(catalog_dir)) as connection:
        return [dict(row) for row in connection.execute(sql, [*parameters, limit])]


def _tags_for_documents(catalog_dir: Path, doc_ids: Sequence[str]) -> dict[str, list[str]]:
    result = {doc_id: [] for doc_id in doc_ids}
    if not doc_ids:
        return result
    with closing(connect_readonly(catalog_dir)) as connection:
        for start in range(0, len(doc_ids), 500):
            chunk = doc_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""SELECT dt.doc_id,t.tag_id,t.label
                    FROM document_tags dt JOIN tags t USING(tag_id)
                    WHERE dt.doc_id IN ({placeholders})
                    ORDER BY dt.doc_id,t.tag_id""",
                chunk,
            )
            for row in rows:
                result[row["doc_id"]].append(row["tag_id"])
    return result


def _readme_text(target_uri: str) -> str:
    return f"""# PCB OpenViking shadow corpus

This directory is a bounded, retrieval-only projection of the PCB catalog.
It is not an evidence package and must not be used as an engineering source of
truth.

Import only the `corpus` directory:

```bash
ov add-resource "<this-directory>/corpus" --to {target_uri} --wait
```

The adapter maps returned `viking://` URIs back to catalog document IDs. The
catalog remains responsible for exact excerpts, provenance, trust status and
citation validation.

Boundaries:

- No catalog writes.
- No Memory or Skill extraction.
- Generated L0/L1 summaries are navigation aids, never evidence.
- Internal scope is not redacted by this exporter; review data egress and model
  placement before importing it into any OpenViking server.
"""


def export_shadow_corpus(
    catalog_dir: Path | str = DEFAULT_CATALOG,
    output_dir: Path | str = "openviking-shadow",
    *,
    scope: str = "public",
    limit: int = 1000,
    doc_types: Sequence[str] | None = None,
    target_uri: str = DEFAULT_TARGET_URI,
    max_chars: int = 0,
    allow_internal: bool = False,
    dry_run: bool = False,
) -> dict:
    """Create a retrieval-only Markdown projection without changing the catalog."""
    catalog = Path(catalog_dir).resolve()
    output = Path(output_dir).resolve()
    if not (catalog / "catalog.sqlite3").is_file():
        raise ShadowError("catalog_not_found")
    for protected in (catalog, catalog.parent):
        try:
            output.relative_to(protected)
        except ValueError:
            continue
        else:
            raise ShadowError("output_must_be_outside_catalog")
    if type(limit) is not int or limit < 1 or limit > 100000:
        raise ShadowError("invalid_limit")
    if type(max_chars) is not int or max_chars < 0:
        raise ShadowError("invalid_max_chars")
    scope = _resolve_scope(scope, allow_internal)
    target_uri = _validate_target_uri(target_uri)
    selected_types = _document_types(doc_types)

    rows = _export_rows(catalog, scope=scope, limit=limit, doc_types=selected_types)
    doc_ids = [row["doc_id"] for row in rows]
    tags = _tags_for_documents(catalog, doc_ids)
    documents: list[dict] = []
    files: list[tuple[Path, str]] = []
    for row in rows:
        text = str(row["body_text"])
        truncated = bool(max_chars and len(text) > max_chars)
        if truncated:
            text = text[:max_chars]
        title = _clean_title(row["title"], row["doc_id"])
        file_text = f"# {title}\n\n{text.rstrip()}\n"
        relative_path = Path("corpus") / row["type"].lower() / f"{row['doc_id']}.md"
        uri = f"{target_uri}/{row['type'].lower()}/{row['doc_id']}.md"
        document = {
            "schema": MANIFEST_SCHEMA,
            "doc_id": row["doc_id"],
            "content_id": row["content_id"],
            "uri": uri,
            "relative_path": relative_path.as_posix(),
            "title": title,
            "doc_type": row["type"],
            "source_type": row["source_type"],
            "source_version": row["source_version"],
            "evidence_status": row["evidence_status"],
            "independently_verified": bool(row["independently_verified"]),
            "content_status": row["content_status"],
            "format": row["format"],
            "duplicate_group_id": row["duplicate_group_id"],
            "content_representations": int(row["representation_count"]),
            "tags": tags.get(row["doc_id"], []),
            "source_text_sha256": _sha256_text(str(row["body_text"])),
            "exported_text_sha256": _sha256_text(text),
            "shadow_file_sha256": _sha256_text(file_text),
            "content_truncated": truncated,
            "normalized_sha256": row["normalized_sha256"],
        }
        documents.append(document)
        files.append((relative_path, file_text))

    type_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    evidence_counts: dict[str, int] = {}
    for document in documents:
        type_counts[document["doc_type"]] = type_counts.get(document["doc_type"], 0) + 1
        source_counts[document["source_type"]] = source_counts.get(document["source_type"], 0) + 1
        evidence = document["evidence_status"]
        evidence_counts[evidence] = evidence_counts.get(evidence, 0) + 1

    summary = {
        "schema": EXPORT_SCHEMA,
        "status": "completed",
        "created_at": _now(),
        "dry_run": bool(dry_run),
        "catalog": catalog.name,
        "scope": scope,
        "target_uri": target_uri,
        "document_limit": limit,
        "documents": len(documents),
        "documents_by_type": type_counts,
        "documents_by_source_type": source_counts,
        "evidence_status_counts": evidence_counts,
        "deduplicated_by": "content_id",
        "export_format": "markdown-v1",
        "content_truncated": sum(bool(row["content_truncated"]) for row in documents),
        "manifest_sha256": _sha256_text("\n".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) for row in documents)),
        "boundaries": {
            "read_only_catalog": True,
            "catalog_modified": False,
            "openviking_contacted": False,
            "contains_internal_material": scope == "internal",
            "redaction": "none",
            "requires_data_egress_review": scope == "internal",
        },
    }
    if dry_run:
        summary["written_documents"] = 0
        return summary

    if output.exists():
        if not output.is_dir():
            raise ShadowError("output_path_is_not_directory")
        if any(output.iterdir()):
            raise ShadowError("output_directory_not_empty")
    output.mkdir(parents=True, exist_ok=True)
    for relative_path, file_text in files:
        path = output / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_text, encoding="utf-8", newline="\n")
    _jsonl_write(output / "manifest.jsonl", documents)
    _json_write(output / "summary.json", summary)
    (output / "README.md").write_text(_readme_text(target_uri), encoding="utf-8", newline="\n")
    summary["written_documents"] = len(documents)
    _json_write(output / "summary.json", summary)
    return summary


def load_shadow_index(shadow_dir: Path | str) -> dict:
    """Load and validate a shadow export without reading its source text."""
    root = Path(shadow_dir).resolve()
    summary_path = root / "summary.json"
    manifest_path = root / "manifest.jsonl"
    if not summary_path.is_file() or not manifest_path.is_file():
        raise ShadowError("shadow_export_not_found")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ShadowError("invalid_shadow_summary") from None
    if not isinstance(summary, dict) or summary.get("schema") != EXPORT_SCHEMA:
        raise ShadowError("invalid_shadow_summary")
    target_uri = summary.get("target_uri")
    try:
        target_uri = _validate_target_uri(target_uri)
    except ShadowError:
        raise ShadowError("invalid_shadow_summary") from None

    by_doc_id: dict[str, dict] = {}
    by_uri: dict[str, dict] = {}
    try:
        with manifest_path.open("r", encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("schema") != MANIFEST_SCHEMA:
                    raise ShadowError("invalid_shadow_manifest")
                doc_id = record.get("doc_id")
                uri = record.get("uri")
                relative = record.get("relative_path")
                if not isinstance(doc_id, str) or not DOC_ID_FULL_RE.fullmatch(doc_id):
                    raise ShadowError("invalid_shadow_manifest")
                if not isinstance(uri, str) or not uri.startswith(target_uri + "/"):
                    raise ShadowError("invalid_shadow_manifest")
                if not isinstance(relative, str):
                    raise ShadowError("invalid_shadow_manifest")
                relative_path = Path(relative)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    raise ShadowError("invalid_shadow_manifest")
                if doc_id in by_doc_id or uri in by_uri:
                    raise ShadowError("duplicate_shadow_manifest")
                by_doc_id[doc_id] = record
                by_uri[uri] = record
    except ShadowError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise ShadowError("invalid_shadow_manifest") from None
    if summary.get("documents") != len(by_doc_id):
        raise ShadowError("invalid_shadow_summary")
    return {
        "root": root,
        "summary": summary,
        "target_uri": target_uri,
        "by_doc_id": by_doc_id,
        "by_uri": by_uri,
    }


def _catalog_metadata(catalog_dir: Path, doc_ids: Sequence[str]) -> dict[str, dict]:
    if not doc_ids:
        return {}
    result: dict[str, dict] = {}
    with closing(connect_readonly(catalog_dir)) as connection:
        for start in range(0, len(doc_ids), 500):
            chunk = doc_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""SELECT d.doc_id,d.type,d.source_type,d.title,d.content_id,
                           d.evidence_status,d.independently_verified,d.content_status,
                           c.normalized_sha256
                    FROM documents d JOIN contents c USING(content_id)
                    WHERE d.doc_id IN ({placeholders})""",
                chunk,
            )
            for row in rows:
                result[row["doc_id"]] = {
                    "doc_id": row["doc_id"],
                    "doc_type": row["type"],
                    "source_type": row["source_type"],
                    "title": row["title"],
                    "content_id": row["content_id"],
                    "evidence_status": row["evidence_status"],
                    "independently_verified": bool(row["independently_verified"]),
                    "content_status": row["content_status"],
                    "normalized_sha256": row["normalized_sha256"],
                }
    return result


def _unwrap_find_response(value: Any) -> tuple[list[dict], int | None]:
    if not isinstance(value, dict):
        raise ShadowError("invalid_openviking_response")
    payload = value
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    elif isinstance(payload.get("data"), dict):
        payload = payload["data"]
    hits = payload.get("resources")
    if hits is None:
        hits = payload.get("results")
    if not isinstance(hits, list):
        raise ShadowError("invalid_openviking_response")
    total = payload.get("total")
    if type(total) is not int:
        total = None
    return [hit for hit in hits if isinstance(hit, dict)], total


def _endpoint_parts(endpoint: str, allow_remote: bool) -> tuple[str, str]:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ShadowError("invalid_endpoint")
    endpoint = endpoint.rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ShadowError("invalid_endpoint")
    host = parsed.hostname.casefold()
    loopback = host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")
    if not loopback and not allow_remote:
        raise ShadowError("remote_endpoint_requires_explicit_allow")
    return endpoint, "loopback" if loopback else "remote"


def _post_json(url: str, payload: dict, headers: dict[str, str], timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise ShadowError(f"openviking_http_{response.status}")
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ShadowError(f"openviking_http_{exc.code}") from None
    except ShadowError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
        raise ShadowError("openviking_unreachable_or_invalid_response") from None
    if not isinstance(value, dict):
        raise ShadowError("invalid_openviking_response")
    return value


def find_shadow(
    catalog_dir: Path | str,
    shadow_dir: Path | str,
    query: str,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    api_key: str | None = None,
    limit: int = 8,
    timeout: float = 30.0,
    allow_remote: bool = False,
    target_uri: str | None = None,
    request_json: Callable[[str, dict, dict[str, str], float], dict] | None = None,
) -> dict:
    """Query OpenViking and return catalog candidates without generated text."""
    catalog = Path(catalog_dir).resolve()
    if not (catalog / "catalog.sqlite3").is_file():
        raise ShadowError("catalog_not_found")
    if not isinstance(query, str) or not query.strip():
        raise ShadowError("invalid_query")
    limit = _validate_limit(limit, maximum=100)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ShadowError("invalid_timeout")
    index = load_shadow_index(shadow_dir)
    selected_target = _validate_target_uri(target_uri or index["target_uri"])
    if selected_target != index["target_uri"] and not selected_target.startswith(index["target_uri"] + "/"):
        raise ShadowError("target_uri_outside_shadow_export")
    endpoint, endpoint_scope = _endpoint_parts(endpoint, allow_remote)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    payload = {
        "query": query,
        "target_uri": selected_target,
        "context_type": ["resource"],
        "level": [2],
        "limit": limit,
        "include_provenance": False,
        "read_content": False,
    }
    transport = request_json or _post_json
    response = transport(endpoint + "/api/v1/search/find", payload, headers, float(timeout))
    hits, total = _unwrap_find_response(response)
    resolved_doc_ids: list[str] = []
    for hit in hits:
        uri = hit.get("uri")
        if not isinstance(uri, str) or not uri.startswith(selected_target + "/"):
            continue
        doc_id = _doc_id_from_uri(hit.get("uri"))
        if doc_id and doc_id not in resolved_doc_ids:
            resolved_doc_ids.append(doc_id)
    catalog_rows = _catalog_metadata(catalog, resolved_doc_ids)
    results: list[dict] = []
    unmapped: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        uri = hit.get("uri")
        if not isinstance(uri, str):
            unmapped.append({"reason": "missing_uri"})
            continue
        if not uri.startswith(selected_target + "/"):
            unmapped.append({"uri": uri, "reason": "uri_outside_shadow_scope"})
            continue
        record = index["by_uri"].get(uri)
        if record is None:
            doc_id = _doc_id_from_uri(uri)
            record = index["by_doc_id"].get(doc_id) if doc_id else None
        else:
            doc_id = record["doc_id"]
        if record is None or not doc_id:
            unmapped.append({"uri": uri, "reason": "doc_id_not_in_shadow_manifest"})
            continue
        if doc_id in seen:
            continue
        metadata = catalog_rows.get(doc_id)
        if metadata is None:
            unmapped.append({"uri": uri, "reason": "doc_id_not_in_catalog"})
            continue
        seen.add(doc_id)
        score = hit.get("score")
        if not isinstance(score, (int, float)):
            score = None
        level = hit.get("level")
        if type(level) is not int:
            level = None
        results.append({
            "uri": uri,
            "score": score,
            "level": level,
            **metadata,
            "shadow_uri": record["uri"],
            "source_version": record.get("source_version"),
            "tags": record.get("tags", []),
        })
    status = "mapped" if results else "no_matches"
    return {
        "schema": FIND_SCHEMA,
        "status": status,
        "query": query,
        "target_uri": selected_target,
        "endpoint": endpoint,
        "endpoint_scope": endpoint_scope,
        "limit": limit,
        "returned": len(hits),
        "openviking_total": total,
        "mapped": len(results),
        "unmapped": len(unmapped),
        "duplicates_removed": max(0, len(hits) - len(results) - len(unmapped)),
        "results": results,
        "unmapped_hits": unmapped,
        "boundaries": {
            "read_only": True,
            "retrieval_only": True,
            "catalog_modified": False,
            "source_text_is_untrusted_data": True,
            "engineering_conclusion_supported": False,
            "external_services": True,
            "request_transport": "local_http" if endpoint_scope == "loopback" else "remote_http",
            "server_model_egress": "not_verified",
            "data_egress": "not_verified",
        },
    }


def verify_shadow_export(catalog_dir: Path | str, shadow_dir: Path | str) -> dict:
    """Verify manifest/file hashes and catalog document identities."""
    catalog = Path(catalog_dir).resolve()
    index = load_shadow_index(shadow_dir)
    errors: list[dict] = []
    verified = 0
    for doc_id, record in sorted(index["by_doc_id"].items()):
        relative = Path(str(record.get("relative_path")))
        path = index["root"] / relative
        if not path.is_file():
            errors.append({"code": "shadow_file_missing", "doc_id": doc_id})
            continue
        if _sha256_file(path) != record.get("shadow_file_sha256"):
            errors.append({"code": "shadow_file_hash_mismatch", "doc_id": doc_id})
            continue
        verified += 1
    doc_ids = sorted(index["by_doc_id"])
    catalog_rows = _catalog_metadata(catalog, doc_ids)
    for doc_id in doc_ids:
        metadata = catalog_rows.get(doc_id)
        if metadata is None:
            errors.append({"code": "catalog_document_missing", "doc_id": doc_id})
            continue
        if metadata["content_id"] != index["by_doc_id"][doc_id].get("content_id"):
            errors.append({"code": "catalog_content_id_mismatch", "doc_id": doc_id})
    return {
        "schema": VERIFY_SCHEMA,
        "status": "passed" if not errors else "failed",
        "documents": len(doc_ids),
        "verified": verified,
        "errors": errors,
        "boundaries": {
            "read_only": True,
            "catalog_modified": False,
            "openviking_contacted": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated OpenViking shadow retrieval for the PCB catalog.")
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser(
        "export", help="Export a bounded catalog projection for OpenViking")
    export.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    export.add_argument("--output")
    export.add_argument("--scope", choices=("public", "internal"), default="public")
    export.add_argument("--limit", type=int, default=1000)
    export.add_argument("--doc-type", action="append", default=[])
    export.add_argument("--target-uri", default=DEFAULT_TARGET_URI)
    export.add_argument("--max-chars", type=int, default=0)
    export.add_argument("--allow-internal", action="store_true")
    export.add_argument("--dry-run", action="store_true")

    find = commands.add_parser(
        "find", help="Query OpenViking and map hits back to catalog IDs")
    find.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    find.add_argument("--shadow-dir", required=True)
    find.add_argument("--query", required=True)
    find.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    find.add_argument("--api-key", default=os.environ.get("OPENVIKING_API_KEY"))
    find.add_argument("--target-uri")
    find.add_argument("--limit", type=int, default=8)
    find.add_argument("--timeout", type=float, default=30.0)
    find.add_argument("--allow-remote", action="store_true")

    verify = commands.add_parser(
        "verify", help="Verify shadow files, hashes and catalog identities")
    verify.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    verify.add_argument("--shadow-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            if not args.dry_run and not args.output:
                raise ShadowError("output_required")
            result = export_shadow_corpus(
                args.catalog,
                args.output or "openviking-shadow",
                scope=args.scope,
                limit=args.limit,
                doc_types=args.doc_type,
                target_uri=args.target_uri,
                max_chars=args.max_chars,
                allow_internal=args.allow_internal,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "find":
            result = find_shadow(
                args.catalog,
                args.shadow_dir,
                args.query,
                endpoint=args.endpoint,
                api_key=args.api_key,
                limit=args.limit,
                timeout=args.timeout,
                allow_remote=args.allow_remote,
                target_uri=args.target_uri,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "verify":
            result = verify_shadow_export(args.catalog, args.shadow_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["status"] == "passed" else 1
    except (ShadowError, CatalogError) as exc:
        error = {
            "schema": "pcb-openviking-shadow-error-v1",
            "status": "error",
            "error": str(exc),
            "boundaries": {"read_only": True, "catalog_modified": False},
        }
        print(json.dumps(error, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
