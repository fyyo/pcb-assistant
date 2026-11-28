"""Build and query the local Phase 3 catalog from retained PCB source assets."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sys

from pcb_assistant.catalog.common import (
    CATALOG_JSONL_EXPORTS, CatalogError, DEFAULT_CATALOG, DOCUMENT_TYPES,
    LEGACY_CATALOG_JSONL_EXPORTS, ROOT, SOURCE_TYPES, canonical_topic,
    connect_readonly, digest_file, is_version_id, read_json, validate_database,
    validate_version_metadata, version_paths,
)

def _read_jsonl(path):
    if not path.is_file():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows

def versions(directory: Path = DEFAULT_CATALOG) -> dict:
    paths = version_paths(directory)
    return {"versions": _read_jsonl(paths["registry"])}

def version(directory: Path, version_id: str) -> dict:
    if not is_version_id(version_id):
        raise CatalogError("invalid_version_id")
    for row in versions(directory)["versions"]:
        if row.get("version_id") == version_id:
            return row
    raise CatalogError("version_not_found")

def batches(directory: Path = DEFAULT_CATALOG) -> dict:
    paths = version_paths(directory)
    return {"batches": _read_jsonl(paths["batches"])}

def _snapshot_counts(snapshot) -> dict:
    if not isinstance(snapshot, dict):
        raise CatalogError("invalid_version_snapshot")
    counts = {}
    for key in ("document_count", "relationship_count", "unresolved_references"):
        value = snapshot.get(key)
        if type(value) is not int or value < 0:
            raise CatalogError("invalid_version_snapshot")
        counts[key] = value
    return counts


def _registered_snapshot(directory: Path, version_id: str, role: str) -> dict:
    if not is_version_id(version_id):
        raise CatalogError("invalid_" + role + "_version_id")
    registered = next((row for row in versions(directory)["versions"]
                       if row.get("version_id") == version_id), None)
    if registered is None:
        raise CatalogError(role + "_version_not_found")
    path = version_paths(directory)["snapshots"] / (version_id + ".json")
    if not path.is_file():
        raise CatalogError(role + "_version_snapshot_missing")
    try:
        snapshot = read_json(path)
    except (OSError, ValueError, UnicodeError) as exc:
        raise CatalogError("invalid_version_snapshot") from exc
    if (not isinstance(snapshot, dict) or snapshot.get("version_id") != version_id
            or snapshot.get("source_name") != registered.get("source_name")
            or snapshot.get("source_version") != registered.get("source_version")
            or not isinstance(snapshot.get("indexed_at"), str)
            or not snapshot.get("indexed_at").strip()):
        raise CatalogError("invalid_version_snapshot")
    _snapshot_counts(snapshot)
    return snapshot

def version_diff(directory: Path, from_version: str, to_version: str) -> dict:
    left = _registered_snapshot(directory, from_version, "from")
    right = _registered_snapshot(directory, to_version, "to")
    return {
        "from_version": from_version,
        "to_version": to_version,
        "document_delta": int(right.get("document_count", 0)) - int(left.get("document_count", 0)),
        "relationship_delta": int(right.get("relationship_count", 0)) - int(left.get("relationship_count", 0)),
        "unresolved_reference_delta": int(right.get("unresolved_references", 0)) - int(left.get("unresolved_references", 0)),
        "indexed_at": right.get("indexed_at"),
        "from_snapshot": left,
        "to_snapshot": right,
    }


def growth(directory: Path = DEFAULT_CATALOG) -> dict:
    """Reconcile cumulative catalog growth with registered import batches."""
    version_rows = versions(directory)["versions"]
    batch_rows = batches(directory)["batches"]
    snapshots = {}
    for item in version_rows:
        version_id = item.get("version_id")
        if not is_version_id(version_id):
            continue
        path = version_paths(directory)["snapshots"] / (version_id + ".json")
        if path.is_file():
            try:
                snapshot = read_json(path)
            except (OSError, ValueError, UnicodeError) as exc:
                raise CatalogError("invalid_version_snapshot") from exc
            if not isinstance(snapshot, dict) or snapshot.get("version_id") != version_id:
                raise CatalogError("invalid_version_snapshot")
            _snapshot_counts(snapshot)
            snapshots[version_id] = snapshot
    cumulative = []
    documents = relationships = unresolved = 0
    for batch in batch_rows:
        documents += int(batch.get("new_entities", 0))
        relationships += int(batch.get("new_relations", 0))
        unresolved += int(batch.get("missing_links", 0)) - int(batch.get("resolved_references", 0))
        cumulative.append({
            **batch,
            "cumulative_documents": documents,
            "cumulative_relationships": relationships,
            "cumulative_unresolved_references": unresolved,
        })
    with closing(connect_readonly(directory)) as connection:
        current = {
            "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "relationships": connection.execute("SELECT COUNT(*) FROM relationships").fetchone()[0],
            "unresolved_references": connection.execute("SELECT COUNT(*) FROM unresolved_references").fetchone()[0],
        }
    totals = {
        "source_records": sum(int(item.get("source_records", 0)) for item in batch_rows),
        "new_entities": sum(int(item.get("new_entities", 0)) for item in batch_rows),
        "existing_entities": sum(int(item.get("existing_entities", 0)) for item in batch_rows),
        "new_relations": sum(int(item.get("new_relations", 0)) for item in batch_rows),
        "duplicate_records": sum(int(item.get("duplicate_records", 0)) for item in batch_rows),
        "missing_links": sum(int(item.get("missing_links", 0)) for item in batch_rows),
        "resolved_references": sum(int(item.get("resolved_references", 0)) for item in batch_rows),
    }
    registered = bool(version_rows)
    reconciliation = None
    reconciliation_status = "not_registered"
    if registered:
        reconciliation = {
            "documents_match": totals["new_entities"] == current["documents"],
            "relationships_match": totals["new_relations"] == current["relationships"],
            "unresolved_match": totals["missing_links"] - totals["resolved_references"]
                               == current["unresolved_references"],
        }
        reconciliation_status = ("matched" if all(reconciliation.values())
                                  else "mismatch")
    return {
        "registered": registered,
        "status": "registered" if registered else "not_registered",
        "reconciliation_status": reconciliation_status,
        "versions": version_rows,
        "batches": batch_rows,
        "snapshots": snapshots,
        "cumulative": cumulative,
        "current": current,
        "totals": totals,
        "reconciliation": reconciliation,
    }


def status(directory: Path = DEFAULT_CATALOG) -> dict:
    with closing(connect_readonly(directory)) as connection:
        state = json.loads(connection.execute("SELECT value FROM metadata WHERE key='status'").fetchone()[0])
        report = json.loads(connection.execute("SELECT value FROM metadata WHERE key='statistics'").fetchone()[0])
    return {"status": "ready" if state == "completed" else state,
            **{key: report[key] for key in ("indexed_at", "totals", "documents_by_type",
                                          "duplicate_content", "unresolved_target_counts")}}


_PREVIEW_WIDTH = 240
_PREVIEW_LEAD = 80
_HIGHLIGHT_TERM_LIMIT = 64


def _unique_terms(values):
    unique = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        term = value.strip()
        if not term or len(term) > 80:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
        if len(unique) >= _HIGHLIGHT_TERM_LIMIT:
            break
    return unique


def _search_match_terms(query, query_mode, query_terms):
    query = query.strip()
    if not query:
        return []
    terms = [query]
    if query_mode == "fulltext":
        from pcb_assistant.knowledge import SYNONYMS
        for term in query_terms:
            if term.startswith("concept"):
                terms.extend(SYNONYMS.get(term[7:], ()))
            else:
                terms.append(term)
        terms.extend(re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}", query))
    return _unique_terms(terms)


def _context_preview(text, terms):
    if not text:
        return ""
    text = str(text)
    match = None
    for term in terms:
        found = re.search(re.escape(term), text, flags=re.IGNORECASE)
        if found and (match is None or found.start() < match.start()):
            match = found
    start = 0
    if match is not None:
        start = max(0, match.start() - _PREVIEW_LEAD)
    end = min(len(text), start + _PREVIEW_WIDTH)
    if match is not None and end - start < _PREVIEW_WIDTH:
        start = max(0, end - _PREVIEW_WIDTH)
    core = re.sub(r"\s+", " ", text[start:end]).strip()
    if not core:
        return ""
    return ("\u2026" if start > 0 else "") + core + ("\u2026" if end < len(text) else "")


def _search_scope(query="", *, doc_type=None, source_type=None, tags=(), query_mode="fulltext",
                  source_version=None, content_status=None, duplicate_group=None):
    """Build the shared document scope used by search and filtered reports."""
    if not isinstance(query, str) or len(query) > 1000 or query_mode not in ("fulltext", "metadata"):
        raise CatalogError("invalid_search_parameters")
    if doc_type is not None and doc_type not in DOCUMENT_TYPES:
        raise CatalogError("invalid_document_type")
    if source_type is not None and source_type not in SOURCE_TYPES:
        raise CatalogError("invalid_source_type")
    if source_version is not None and (not isinstance(source_version, str) or not 1 <= len(source_version) <= 150):
        raise CatalogError("invalid_source_version")
    if content_status is not None and content_status not in ("ready", "metadata_only"):
        raise CatalogError("invalid_content_status")
    if duplicate_group is not None and (not isinstance(duplicate_group, str)
            or not re.fullmatch(r"GROUP_[0-9a-f]{32}", duplicate_group)):
        raise CatalogError("invalid_duplicate_group")
    if isinstance(tags, str):
        tags = (tags,)
    if not isinstance(tags, (list, tuple)) or len(tags) > 20:
        raise CatalogError("too_many_tag_filters")
    if any(not isinstance(tag, str) or not 1 <= len(tag) <= 200 for tag in tags):
        raise CatalogError("invalid_tag_filter")

    conditions, parameters = [], []
    join, score = "", "0.0"
    query_terms = []
    if query.strip() and query_mode == "fulltext":
        from pcb_assistant.knowledge import tokens
        query_terms = list(dict.fromkeys(tokens(query)))[:40]
    if query.strip() and query_mode == "metadata":
        # A literal metadata search is independent of tokenization and source prose.
        conditions.append("(" + " OR ".join(f"instr(lower(d.{name}),?)>0" for name in
                          ("title", "doc_id", "source_id", "source_path")) + ")")
        parameters.extend([query.strip().lower()] * 4)
    elif query.strip():
        terms = query_terms
        if not terms:
            if not re.search(r"[A-Za-z0-9\u3400-\u9fff]{2,}", query):
                conditions.append("0=1")
            # Existing segmentation can discard both characters of an unseen PCB term.
            # Use a bounded literal phrase rather than silently reporting no evidence.
            join = "JOIN contents lookup_content ON lookup_content.content_id=d.content_id"
            conditions.append("(instr(d.title,?)>0 OR instr(COALESCE(lookup_content.normalized_text,lookup_content.original_text),?)>0)")
            parameters.extend((query.strip(), query.strip()))
        else:
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            join = "JOIN document_search ON document_search.doc_id=d.doc_id"
            score = "bm25(document_search,0.0,5.0,1.0,2.0)"
            conditions.append("document_search MATCH ?")
            parameters.append(expression)
    if doc_type:
        conditions.append("d.type=?")
        parameters.append(doc_type)
    if source_type:
        conditions.append("d.source_type=?")
        parameters.append(source_type)
    for column, value in (("source_version", source_version), ("content_status", content_status),
                          ("duplicate_group_id", duplicate_group)):
        if value is not None:
            conditions.append(f"d.{column}=?")
            parameters.append(value)
    for tag in tags:
        tag_id = tag if re.fullmatch(r"topic_[0-9a-f]{32}", tag) else canonical_topic(tag)[0]
        conditions.append("EXISTS(SELECT 1 FROM document_tags dt WHERE dt.doc_id=d.doc_id AND dt.tag_id=?)")
        parameters.append(tag_id)
    where = " AND ".join(conditions) if conditions else "1=1"
    return {
        "join": join,
        "score": score,
        "where": where,
        "parameters": parameters,
        "match_terms": _search_match_terms(query, query_mode, query_terms),
        "filters": {"query": query, "query_mode": query_mode, "doc_type": doc_type,
                    "source_type": source_type, "source_version": source_version,
                    "content_status": content_status, "tags": list(tags), "duplicate_group": duplicate_group},
    }


def search(directory: Path, query: str = "", *, doc_type=None, source_type=None,
           tags=(), limit=20, offset=0, deduplicate=True, query_mode="fulltext",
           source_version=None, content_status=None, duplicate_group=None) -> dict:
    if (type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int
            or not 0 <= offset <= 10_000_000 or type(deduplicate) is not bool):
        raise CatalogError("invalid_search_parameters")
    scope = _search_scope(query, doc_type=doc_type, source_type=source_type, tags=tags,
                          query_mode=query_mode, source_version=source_version,
                          content_status=content_status, duplicate_group=duplicate_group)
    join, score, where = scope["join"], scope["score"], scope["where"]
    parameters = scope["parameters"]
    match_terms = scope["match_terms"]
    preview_sql = ("COALESCE(c.normalized_text,c.original_text) AS search_text"
                   if query.strip() and query_mode == "fulltext"
                   else "substr(COALESCE(c.normalized_text,c.original_text),1,240) AS preview")
    matched = f"""WITH matched AS MATERIALIZED (
        SELECT d.doc_id,d.duplicate_group_id,{score} AS score FROM documents d {join} WHERE {where})"""
    aggregate = "COUNT(DISTINCT duplicate_group_id)" if deduplicate else "COUNT(*)"
    with closing(connect_readonly(directory)) as connection:
        total = connection.execute(matched + " SELECT " + aggregate + " FROM matched", parameters).fetchone()[0]
        rank_filter = "r.position=1" if deduplicate else "1=1"
        sql = matched + f""", ranked AS (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY duplicate_group_id ORDER BY score,doc_id) AS position,
            COUNT(*) OVER(PARTITION BY duplicate_group_id) AS matching_duplicates FROM matched)
            SELECT d.doc_id,d.type,d.source_type,d.title,d.source_version,d.source_path,d.locator_json,
                   d.evidence_status,d.independently_verified,d.content_status,d.format,d.extraction_method,
                   d.duplicate_group_id,r.matching_duplicates,r.score,
                   {preview_sql}
            FROM ranked r JOIN documents d ON d.doc_id=r.doc_id JOIN contents c USING(content_id)
            WHERE {rank_filter} ORDER BY r.score,d.doc_id LIMIT ? OFFSET ?"""
        results = []
        for row in connection.execute(sql, [*parameters, limit, offset]):
            entry = dict(row)
            if "search_text" in entry:
                entry["preview"] = _context_preview(entry.pop("search_text"), match_terms)
            entry["locator"] = json.loads(entry.pop("locator_json"))
            entry["independently_verified"] = bool(entry["independently_verified"])
            entry["tags"] = [dict(tag) for tag in connection.execute("""SELECT DISTINCT t.tag_id,t.label
                FROM document_tags dt JOIN tags t USING(tag_id) WHERE dt.doc_id=? ORDER BY t.tag_id""", (row["doc_id"],))]
            results.append(entry)
    return {"total": total, "limit": limit, "offset": offset, "deduplicated": deduplicate,
            "match_terms": match_terms, "filters": scope["filters"], "results": results}


def _check_document_id(doc_id):
    if not isinstance(doc_id, str) or not re.fullmatch(r"(?:MAIL|CASE|ATTACHMENT|KNOWLEDGE|PUBLIC)_[0-9a-f]{32}", doc_id):
        raise CatalogError("invalid_document_id")


def _check_page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 10_000_000:
        raise CatalogError("invalid_pagination")


def _require_document(connection, doc_id):
    if not connection.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone():
        raise CatalogError("document_not_found")


def _decode_row(row):
    entry = dict(row)
    for name in list(entry):
        if name.endswith("_json"):
            entry[name[:-5]] = json.loads(entry.pop(name))
    return entry


def document(directory: Path, doc_id: str) -> dict:
    """Read full retained text and metadata, keeping quote offsets tied to original_text."""
    _check_document_id(doc_id)
    with closing(connect_readonly(directory)) as connection:
        row = connection.execute("""SELECT d.*,c.original_text,c.normalized_text,
            c.normalized_sha256,c.normalization_json FROM documents d JOIN contents c USING(content_id)
            WHERE d.doc_id=?""", (doc_id,)).fetchone()
        if row is None:
            raise CatalogError("document_not_found")
        entry = _decode_row(row)
        entry["independently_verified"] = bool(entry["independently_verified"])
        entry["normalized_text"] = entry["normalized_text"] if entry["normalized_text"] is not None else entry["original_text"]
        entry["tags"] = [dict(tag) for tag in connection.execute("""SELECT t.tag_id,t.label,dt.origin
            FROM document_tags dt JOIN tags t USING(tag_id) WHERE dt.doc_id=? ORDER BY t.tag_id,dt.origin""", (doc_id,))]
        entry["source_count"] = connection.execute("SELECT COUNT(*) FROM source_map WHERE doc_id=?", (doc_id,)).fetchone()[0]
        entry["quality_issue_count"] = connection.execute("SELECT COUNT(*) FROM quality_issues WHERE doc_id=?", (doc_id,)).fetchone()[0]
        entry["duplicate_count"] = connection.execute("SELECT COUNT(*) FROM documents WHERE duplicate_group_id=?",
                                                      (entry["duplicate_group_id"],)).fetchone()[0]
    return entry


def document_records(directory: Path, doc_id: str, *, kind="sources", limit=20, offset=0) -> dict:
    """Page provenance and quality records without exposing filesystem read endpoints."""
    _check_document_id(doc_id)
    _check_page(limit, offset)
    if kind not in ("sources", "issues"):
        raise CatalogError("invalid_record_kind")
    table, order = (("source_map", "CASE WHEN role='primary' THEN 0 ELSE 1 END,mapping_id")
                    if kind == "sources" else ("quality_issues", "severity,code,issue_id"))
    with closing(connect_readonly(directory)) as connection:
        _require_document(connection, doc_id)
        total = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE doc_id=?", (doc_id,)).fetchone()[0]
        rows = connection.execute(f"SELECT * FROM {table} WHERE doc_id=? ORDER BY {order} LIMIT ? OFFSET ?",
                                  (doc_id, limit, offset))
        records = [_decode_row(row) for row in rows]
    return {"doc_id": doc_id, "total": total, "limit": limit, "offset": offset, "results": records}


def facets(directory: Path = DEFAULT_CATALOG) -> dict:
    """All counts here describe retained entities, before search-time deduplication."""
    with closing(connect_readonly(directory)) as connection:
        groups = lambda sql: [dict(row) for row in connection.execute(sql)]
        return {
            "document_types": groups("SELECT type,COUNT(*) AS documents FROM documents GROUP BY type ORDER BY type"),
            "source_types": groups("SELECT source_type,COUNT(*) AS documents FROM documents GROUP BY source_type ORDER BY source_type"),
            "source_versions": groups("SELECT source_version,COUNT(*) AS documents FROM documents GROUP BY source_version ORDER BY source_version"),
            "tags": groups("""SELECT t.tag_id,t.label,COUNT(DISTINCT dt.doc_id) AS documents
                FROM tags t JOIN document_tags dt USING(tag_id) GROUP BY t.tag_id,t.label ORDER BY documents DESC,t.tag_id"""),
        }


def statistics(directory: Path = DEFAULT_CATALOG) -> dict:
    with closing(connect_readonly(directory)) as connection:
        report = json.loads(connection.execute("SELECT value FROM metadata WHERE key='statistics'").fetchone()[0])
        report["scope"] = "all_catalog_records_before_deduplication"
        report["content_status_counts"] = [dict(row) for row in connection.execute("""SELECT type,content_status,
            COUNT(*) AS documents FROM documents GROUP BY type,content_status ORDER BY type,content_status""")]
        report["evidence_status_counts"] = [dict(row) for row in connection.execute("""SELECT evidence_status,
            independently_verified,COUNT(*) AS documents FROM documents
            GROUP BY evidence_status,independently_verified ORDER BY evidence_status,independently_verified""")]
        report["ingestion_days"] = [dict(row) for row in connection.execute("""SELECT substr(indexed_at,1,10) AS day,
            COUNT(*) AS documents FROM documents GROUP BY day ORDER BY day""")]
    return report


def filtered_statistics(directory: Path = DEFAULT_CATALOG, query: str = "", *, doc_type=None,
                        source_type=None, source_version=None, content_status=None, tags=(),
                        query_mode="fulltext") -> dict:
    """Report retained entities in the same document scope used by search."""
    scope = _search_scope(query, doc_type=doc_type, source_type=source_type, tags=tags,
                          query_mode=query_mode, source_version=source_version,
                          content_status=content_status)
    filters = scope["filters"]
    if (not query.strip() and doc_type is None and source_type is None
            and source_version is None and content_status is None and not filters["tags"]):
        return statistics(directory)

    matched = f"""WITH matched AS MATERIALIZED (
        SELECT d.doc_id,d.type,d.source_type,d.content_status,d.content_id,d.duplicate_group_id,
               d.evidence_status,d.independently_verified,d.source_created_at,d.indexed_at
        FROM documents d {scope['join']} WHERE {scope['where']})"""
    filtered_relationships = matched + """, filtered_relationships AS (
        SELECT r.relationship_id,r.relation
        FROM relationships r JOIN matched m ON m.doc_id=r.from_id
        UNION
        SELECT r.relationship_id,r.relation
        FROM relationships r JOIN matched m ON m.doc_id=r.to_id)"""
    parameters = scope["parameters"]

    with closing(connect_readonly(directory)) as connection:
        base = json.loads(connection.execute("SELECT value FROM metadata WHERE key='statistics'").fetchone()[0])

        def rows(sql):
            return [dict(row) for row in connection.execute(matched + " " + sql, parameters)]

        def one(sql):
            return connection.execute(matched + " " + sql, parameters).fetchone()[0]

        documents, contents = connection.execute(
            matched + " SELECT COUNT(*),COUNT(DISTINCT content_id) FROM matched", parameters).fetchone()
        relationships_by_type = dict(connection.execute(filtered_relationships + """
            SELECT relation,COUNT(*) FROM filtered_relationships GROUP BY relation""", parameters))
        source_maps_by_role = dict(connection.execute(matched + """ SELECT s.role,COUNT(*)
            FROM source_map s WHERE EXISTS(SELECT 1 FROM matched m WHERE m.doc_id=s.doc_id)
            GROUP BY s.role""", parameters))
        unresolved_target_counts = rows("""SELECT u.target_type,
            COUNT(DISTINCT u.target_source_id) AS unique_missing_targets,
            COUNT(*) AS missing_references,COUNT(DISTINCT u.from_id) AS affected_documents
            FROM unresolved_references u
            WHERE EXISTS(SELECT 1 FROM matched m WHERE m.doc_id=u.from_id)
            GROUP BY u.target_type""")
        report = {
            "schema_version": base.get("schema_version"),
            "status": base.get("status", "completed"),
            "indexed_at": base.get("indexed_at"),
            "identity_version": base.get("identity_version"),
            "scope": "filtered_catalog_records_before_deduplication",
            "filters": filters,
            "totals": {
                "documents": documents,
                "contents": contents,
                "relationships": sum(relationships_by_type.values()),
                "source_map": sum(source_maps_by_role.values()),
                "unresolved_references": sum(item["missing_references"] for item in unresolved_target_counts),
                "excluded_records": 0,
            },
            "documents_by_type": rows("""SELECT type,source_type,COUNT(*) AS documents,
                SUM(content_status='ready') AS documents_with_text FROM matched
                GROUP BY type,source_type ORDER BY type,source_type"""),
            "duplicate_content": rows("""SELECT type,source_type,COUNT(*) AS duplicate_groups,
                SUM(members-1) AS extra_records FROM
                (SELECT type,source_type,duplicate_group_id,COUNT(*) AS members FROM matched
                 WHERE content_status='ready' GROUP BY type,source_type,duplicate_group_id
                 HAVING COUNT(*)>1) GROUP BY type,source_type ORDER BY type,source_type"""),
            "relationships_by_type": relationships_by_type,
            "source_maps_by_role": source_maps_by_role,
            "quality_issue_counts": dict(connection.execute(matched + """ SELECT q.code,COUNT(*)
                FROM quality_issues q JOIN matched m ON m.doc_id=q.doc_id GROUP BY q.code""", parameters)),
            "excluded_by_reason": {},
            "unresolved_target_counts": unresolved_target_counts,
            "tag_counts": rows("""SELECT t.tag_id,t.label,d.type,COUNT(DISTINCT d.doc_id) AS documents
                FROM tags t JOIN document_tags dt USING(tag_id)
                JOIN matched d ON d.doc_id=dt.doc_id
                GROUP BY t.tag_id,t.label,d.type ORDER BY t.tag_id,d.type"""),
            "source_created_at_unknown": one("SELECT COUNT(*) FROM matched WHERE source_created_at IS NULL"),
            "processing": base.get("processing", {}),
            "processing_scope": "all_catalog_records",
            "original_assets_modified": False,
            "content_deduplication": base.get("content_deduplication"),
            "timestamp_policy": base.get("timestamp_policy"),
            "text_policy": base.get("text_policy"),
            "content_status_counts": rows("""SELECT type,content_status,COUNT(*) AS documents
                FROM matched GROUP BY type,content_status ORDER BY type,content_status"""),
            "evidence_status_counts": rows("""SELECT evidence_status,independently_verified,
                COUNT(*) AS documents FROM matched
                GROUP BY evidence_status,independently_verified
                ORDER BY evidence_status,independently_verified"""),
            "ingestion_days": rows("""SELECT substr(indexed_at,1,10) AS day,COUNT(*) AS documents
                FROM matched GROUP BY day ORDER BY day"""),
        }
    return report


def related(directory: Path, doc_id: str, *, direction="both", limit=50, offset=0, unresolved_offset=0) -> dict:
    if direction not in ("both", "in", "out"):
        raise CatalogError("invalid_relationship_parameters")
    _check_page(limit, offset)
    _check_page(limit, unresolved_offset)
    _check_document_id(doc_id)
    where = {"in": "r.to_id=?", "out": "r.from_id=?", "both": "(r.from_id=? OR r.to_id=?)"}[direction]
    parameters = [doc_id, doc_id] if direction == "both" else [doc_id]
    with closing(connect_readonly(directory)) as connection:
        _require_document(connection, doc_id)
        total = connection.execute("SELECT COUNT(*) FROM relationships r WHERE " + where, parameters).fetchone()[0]
        rows = connection.execute("""SELECT r.*,a.type AS from_type,a.title AS from_title,
            b.type AS to_type,b.title AS to_title FROM relationships r
            JOIN documents a ON a.doc_id=r.from_id JOIN documents b ON b.doc_id=r.to_id
            WHERE """ + where + " ORDER BY r.relationship_id LIMIT ? OFFSET ?", [*parameters, limit, offset])
        relations = []
        for row in rows:
            entry = dict(row)
            entry["metadata"] = json.loads(entry.pop("metadata_json"))
            relations.append(entry)
        missing = [_decode_row(row) for row in connection.execute("""SELECT target_type,target_source_id,relation,reason,
            source_path,locator_json FROM unresolved_references WHERE from_id=? ORDER BY reference_id LIMIT ? OFFSET ?""",
            (doc_id, limit, unresolved_offset))]
        missing_total = connection.execute("SELECT COUNT(*) FROM unresolved_references WHERE from_id=?", (doc_id,)).fetchone()[0]
    return {"doc_id": doc_id, "total": total, "limit": limit, "offset": offset, "direction": direction,
            "relationships": relations, "unresolved_total": missing_total,
            "unresolved_limit": limit, "unresolved_offset": unresolved_offset, "unresolved_references": missing}


def validate(directory: Path = DEFAULT_CATALOG) -> dict:
    directory = directory.resolve()
    result = validate_database(directory)
    manifest = read_json(directory / "artifact_manifest.json")
    artifact_names = set(manifest["artifacts"])
    failures = []
    compressed_exports = artifact_names.intersection(CATALOG_JSONL_EXPORTS)
    plain_exports = artifact_names.intersection(LEGACY_CATALOG_JSONL_EXPORTS)
    if compressed_exports and plain_exports:
        failures.append({"artifact": "jsonl_exports", "reason": "mixed_export_format"})
    expected_exports = set(CATALOG_JSONL_EXPORTS if compressed_exports or not plain_exports
                           else LEGACY_CATALOG_JSONL_EXPORTS)
    for name in sorted(expected_exports - artifact_names):
        failures.append({"artifact": name, "reason": "missing_export"})
    for name, expected in manifest["artifacts"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or path.parent != directory or not path.is_file():
            failures.append({"artifact": name, "reason": "invalid_or_missing_file"})
        elif path.stat().st_size != expected["bytes"] or digest_file(path) != expected["sha256"]:
            failures.append({"artifact": name, "reason": "checksum_mismatch"})
    result["artifact_files_checked"] = len(manifest["artifacts"])
    result["artifact_failures"] = failures
    version_validation = validate_version_metadata(directory)
    result["version_metadata"] = version_validation
    if failures or version_validation["status"] == "failed":
        result["status"] = "failed"
    return result


def _missing_catalog_hint(directory):
    """Explain how to create a catalog before the first query."""
    return "\n".join((
        "未找到知识库目录：" + str(directory),
        "本仓库只提供源码，不附带邮件、附件或数据库。请先用自己的资料建立知识库，或指定已有目录：",
        "  python -X utf8 -B knowledge_catalog.py build --output data/my_catalog",
        "  python -X utf8 -B knowledge_catalog.py --catalog data/my_catalog status",
        "步骤见 docs/DATA_PIPELINE.md。",
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="Create a new catalog without changing source assets")
    build_parser.add_argument("--root", type=Path, default=ROOT)
    build_parser.add_argument("--output", type=Path)
    build_parser.add_argument("--mail-source", action="append", default=None,
                              help="Mail candidates JSONL; repeat for multiple PST runs")
    build_parser.add_argument("--attachment-source", action="append", default=None,
                              help="Attachment run directory (or documents.jsonl); repeat for multiple PST runs")
    build_parser.add_argument("--case-source", default=None, help="Case run directory")
    build_parser.add_argument("--historical-exclusions", choices=("full_record", "provenance_only"),
                              default="full_record", help="Payload retained for excluded historical rows")
    commands.add_parser("status")
    commands.add_parser("validate")
    commands.add_parser("versions")
    commands.add_parser("batches")
    commands.add_parser("growth", help="Report cumulative growth from real import batches")
    quality_parser = commands.add_parser(
        "quality", help="Evaluate similar-case recommendation signals and latency")
    quality_parser.add_argument("--sample-size", type=int, default=200)
    quality_parser.add_argument("--limit", type=int, default=10)
    quality_parser.add_argument("--seed", type=int, default=20261022)
    gate_parser = commands.add_parser(
        "quality-gate", help="Run automated recommendation quality gate without human labels")
    gate_parser.add_argument("--sample-size", type=int, default=200)
    gate_parser.add_argument("--limit", type=int, default=10)
    gate_parser.add_argument("--seed", type=int, default=20261022)
    review_create_parser = commands.add_parser(
        "review-create", help="Create a separate human review queue")
    review_create_parser.add_argument("--review-root", type=Path,
                                      default=ROOT / "data/phase3_review_v1")
    review_create_parser.add_argument("--sample-size", type=int, default=200)
    review_create_parser.add_argument("--limit", type=int, default=10)
    review_create_parser.add_argument("--seed", type=int, default=20261022)
    review_create_parser.add_argument("--reviewer", action="append", dest="reviewers")
    review_create_parser.add_argument("--include-same-source", action="store_true")
    review_next_parser = commands.add_parser(
        "review-next", help="List one reviewer's next unlabelled queue items")
    review_next_parser.add_argument("--review-root", type=Path,
                                    default=ROOT / "data/phase3_review_v1")
    review_next_parser.add_argument("--queue-id", required=True)
    review_next_parser.add_argument("--reviewer", required=True)
    review_next_parser.add_argument("--limit", type=int, default=10)
    review_evidence_parser = commands.add_parser(
        "review-evidence",
        help="Show local read-only source and candidate evidence for one review item")
    review_evidence_parser.add_argument("--review-root", type=Path,
                                        default=ROOT / "data/phase3_review_v1")
    review_evidence_parser.add_argument("--queue-id", required=True)
    review_evidence_parser.add_argument("--review-id", required=True)
    review_evidence_parser.add_argument("--reviewer", required=True)
    review_evidence_parser.add_argument("--max-chars", type=int, default=12000)
    review_submit_parser = commands.add_parser(
        "review-submit", help="Record one anonymous human review revision")
    review_submit_parser.add_argument("--review-root", type=Path,
                                      default=ROOT / "data/phase3_review_v1")
    review_submit_parser.add_argument("--queue-id", required=True)
    review_submit_parser.add_argument("--review-id", required=True)
    review_submit_parser.add_argument("--reviewer", required=True)
    review_submit_parser.add_argument("--relevance", required=True)
    review_submit_parser.add_argument("--engineering", required=True)
    review_submit_parser.add_argument("--reason-code", action="append", required=True)
    review_submit_parser.add_argument("--note", default="")
    review_submit_parser.add_argument("--expected-revision", type=int)
    review_report_parser = commands.add_parser(
        "review-report", help="Aggregate local human labels without document-level output")
    review_report_parser.add_argument("--review-root", type=Path,
                                      default=ROOT / "data/phase3_review_v1")
    review_report_parser.add_argument("--queue-id", required=True)
    diff_parser = commands.add_parser("diff", help="Compare two registered version snapshots")
    diff_parser.add_argument("from_version")
    diff_parser.add_argument("to_version")
    stats_parser = commands.add_parser("stats", help="Report retained catalog records, without search deduplication")
    stats_parser.add_argument("query", nargs="?", default="")
    stats_parser.add_argument("--type", choices=DOCUMENT_TYPES)
    stats_parser.add_argument("--source", choices=SOURCE_TYPES)
    stats_parser.add_argument("--tag", action="append", default=[])
    stats_parser.add_argument("--mode", choices=("fulltext", "metadata"), default="fulltext")
    stats_parser.add_argument("--version")
    stats_parser.add_argument("--content-status", choices=("ready", "metadata_only"))
    ingest_parser = commands.add_parser("ingest", help="Ingest one source into a new independent catalog")
    ingest_parser.add_argument("--manifest", type=Path, required=True)
    ingest_parser.add_argument("--output", type=Path, required=True)
    detail_parser = commands.add_parser("show", help="Read a document's original text and provenance metadata")
    detail_parser.add_argument("doc_id")
    evidence_parser = commands.add_parser(
        "evidence", help="Build a bounded, source-grounded evidence package")
    evidence_parser.add_argument("query", nargs="?", default="")
    evidence_parser.add_argument("--type", choices=DOCUMENT_TYPES)
    evidence_parser.add_argument("--source", choices=SOURCE_TYPES)
    evidence_parser.add_argument("--tag", action="append", default=[])
    evidence_parser.add_argument("--limit", type=int, default=10)
    evidence_parser.add_argument("--offset", type=int, default=0)
    evidence_parser.add_argument("--version")
    evidence_parser.add_argument("--content-status", choices=("ready", "metadata_only"))
    evidence_parser.add_argument("--export", type=Path,
                                 help="Write the evidence response to a new local JSON file")
    evidence_parser.add_argument("--overwrite", action="store_true",
                                 help="Replace an existing exported JSON file")
    agent_parser = commands.add_parser(
        "agent", help="Answer with exact excerpts from the local read-only evidence layer")
    agent_parser.add_argument("question", nargs="?", default="")
    agent_parser.add_argument("--type", choices=DOCUMENT_TYPES)
    agent_parser.add_argument("--source", choices=SOURCE_TYPES)
    agent_parser.add_argument("--tag", action="append", default=[])
    agent_parser.add_argument("--limit", type=int, default=10)
    agent_parser.add_argument("--offset", type=int, default=0)
    agent_parser.add_argument("--max-sources", type=int)
    agent_parser.add_argument("--version")
    agent_parser.add_argument("--content-status", choices=("ready", "metadata_only"))
    agent_parser.add_argument("--export", type=Path,
                              help="Write the agent response to a new local JSON file")
    agent_parser.add_argument("--overwrite", action="store_true",
                              help="Replace an existing exported JSON file")
    chat_parser = commands.add_parser(
        "chat", help="Converse with a source-grounded PCB lamination agent")
    chat_parser.add_argument("question", nargs="?", default="")
    chat_parser.add_argument("--json", action="store_true", dest="as_json",
                             help="Print one-shot JSON output")
    chat_parser.add_argument("--model", choices=("auto", "off"), default="auto",
                             help="auto uses the local model; off uses evidence fallback")
    chat_parser.add_argument("--type", choices=DOCUMENT_TYPES)
    chat_parser.add_argument("--source", choices=SOURCE_TYPES)
    chat_parser.add_argument("--tag", action="append", default=[])
    chat_parser.add_argument("--limit", type=int, default=6)
    chat_parser.add_argument("--max-history-turns", type=int, default=4)
    chat_parser.add_argument("--max-new-tokens", type=int, default=360)
    chat_parser.add_argument("--version")
    chat_parser.add_argument("--content-status", choices=("ready", "metadata_only"))
    chat_parser.add_argument("--export", type=Path,
                             help="Write the chat response to a new local JSON file")
    chat_parser.add_argument("--overwrite", action="store_true",
                             help="Replace an existing exported JSON file")
    workbench_parser = commands.add_parser(
        "workbench", help="Interactive multi-turn Agent workbench with persistent local cases")
    workbench_parser.add_argument("question", nargs="?", default="")
    workbench_parser.add_argument("--mode", choices=("agent", "local"), default="agent")
    workbench_parser.add_argument("--case", dest="case_id",
                                  help="Open or create a persistent case under .private/cases/")
    workbench_parser.add_argument("--cases-dir", type=Path)
    workbench_parser.add_argument("--agent-config", type=Path,
                                  help="Optional local profile JSON (default: .private/agent_profile.json)")
    workbench_parser.add_argument("--profile", default=None,
                                  help="Named profile inside .private/agent_profile.json")
    workbench_parser.add_argument("--json", action="store_true", dest="as_json",
                                  help="Print one-shot JSON output")
    workbench_parser.add_argument("--mcp-config", action="store_true",
                                  help="Print MCP client and Open WebUI (mcpo) configuration snippets")
    workbench_parser.add_argument("--toolset", choices=("single", "agent"), default="agent")
    web_parser = commands.add_parser(
        "web", help="Launch local Web workbench UI, REST API, and /mcp HTTP endpoint")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8765)
    web_parser.add_argument("--cases-dir", type=Path)
    web_parser.add_argument("--agent-config", type=Path)
    web_parser.add_argument("--profile", default=None,
                            help="Initial active profile inside .private/agent_profile.json")
    web_parser.add_argument("--toolset", choices=("single", "agent"), default="agent")
    web_parser.add_argument("--api-token", default=None)
    web_parser.add_argument("--open", action="store_true", dest="open_browser",
                            help="Open the workbench in the default browser")
    web_parser.add_argument("--desktop", action="store_true",
                            help="Open the workbench in a standalone desktop app window")
    web_parser.add_argument("--create-shortcut", action="store_true",
                            help="Create a windowless desktop shortcut on Windows")
    web_parser.add_argument("--reuse-running", action="store_true",
                            help="If workbench is already running on host:port, open it and exit 0")
    web_parser.add_argument("--auto-port", action="store_true",
                            help="If preferred port is busy, automatically select next free port")
    doctor_parser = commands.add_parser(
        "doctor", help="Inspect Python runtime, read-only Catalog, privacy assets, and LLM profile")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json",
                               help="Print structured JSON report")
    doctor_parser.add_argument("--probe-llm", action="store_true",
                               help="Send a minimal zero-data connectivity probe to the active LLM profile")
    doctor_parser.add_argument("--agent-config", type=Path, default=None)
    search_parser = commands.add_parser("search")
    search_parser.add_argument("query", nargs="?", default="")
    search_parser.add_argument("--type", choices=DOCUMENT_TYPES)
    search_parser.add_argument("--source", choices=SOURCE_TYPES)
    search_parser.add_argument("--tag", action="append", default=[])
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--offset", type=int, default=0)
    search_parser.add_argument("--include-duplicates", action="store_true")
    search_parser.add_argument("--mode", choices=("fulltext", "metadata"), default="fulltext")
    search_parser.add_argument("--version")
    search_parser.add_argument("--content-status", choices=("ready", "metadata_only"))
    relation_parser = commands.add_parser("related")
    relation_parser.add_argument("doc_id")
    relation_parser.add_argument("--direction", choices=("in", "out", "both"), default="both")
    relation_parser.add_argument("--limit", type=int, default=50)
    relation_parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    if args.command not in ("build", "doctor") and not Path(args.catalog).is_dir():
        print(_missing_catalog_hint(args.catalog), file=sys.stderr)
    if args.command == "chat" and not args.question and (args.as_json or args.export):
        parser.error("--json and --export require a question")
    try:
        if args.command == "doctor":
            import pcb_assistant.config as runtime_config
            report = runtime_config.run_doctor(
                catalog_directory=args.catalog,
                profile_path=args.agent_config or runtime_config.DEFAULT_PROFILE_PATH,
                probe_llm=args.probe_llm,
            )
            if args.as_json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(runtime_config.format_doctor_text(report))
            return 1 if report.get("status") == "failed" else 0
        if args.command == "build":
            from pcb_assistant.catalog.build import build
            build(args.root, args.output, mail_sources=args.mail_source,
                  attachment_sources=args.attachment_source, case_source=args.case_source,
                  historical_exclusions=args.historical_exclusions)
            return 0
        if args.command == "status":
            result = status(args.catalog)
        elif args.command == "versions":
            result = versions(args.catalog)
        elif args.command == "batches":
            result = batches(args.catalog)
        elif args.command == "growth":
            result = growth(args.catalog)
        elif args.command == "quality":
            from pcb_assistant.catalog.evaluation import evaluate_similar_cases
            result = evaluate_similar_cases(args.catalog, sample_size=args.sample_size,
                                            limit=args.limit, seed=args.seed)
        elif args.command == "quality-gate":
            from pcb_assistant.catalog.evaluation import evaluate_quality_gate
            result = evaluate_quality_gate(args.catalog, sample_size=args.sample_size,
                                           limit=args.limit, seed=args.seed)
        elif args.command == "review-create":
            from pcb_assistant.catalog.review import DEFAULT_REVIEWERS, create_review_queue
            reviewers = tuple(args.reviewers) if args.reviewers else DEFAULT_REVIEWERS
            result = create_review_queue(
                args.catalog, args.review_root, sample_size=args.sample_size,
                limit=args.limit, seed=args.seed, reviewers=reviewers,
                include_same_source=args.include_same_source)
        elif args.command == "review-next":
            from pcb_assistant.catalog.review import review_next
            result = review_next(args.review_root, args.queue_id, args.reviewer,
                                 limit=args.limit)
        elif args.command == "review-evidence":
            from pcb_assistant.catalog.review import review_evidence
            result = review_evidence(
                args.catalog, args.review_root, args.queue_id, args.review_id,
                args.reviewer, max_chars=args.max_chars)
        elif args.command == "review-submit":
            from pcb_assistant.catalog.review import submit_review
            result = submit_review(
                args.review_root, args.queue_id, args.review_id, args.reviewer,
                relevance_label=args.relevance, engineering_label=args.engineering,
                reason_codes=args.reason_code, note=args.note,
                expected_revision=args.expected_revision)
        elif args.command == "review-report":
            from pcb_assistant.catalog.review import build_review_report
            result = build_review_report(args.review_root, args.queue_id)
        elif args.command == "diff":
            result = version_diff(args.catalog, args.from_version, args.to_version)
        elif args.command == "ingest":
            from pcb_assistant.catalog.incremental import ingest
            result = ingest(args.catalog, args.manifest, args.output)
        elif args.command == "stats":
            result = filtered_statistics(args.catalog, args.query, doc_type=args.type,
                                         source_type=args.source, tags=args.tag, query_mode=args.mode,
                                         source_version=args.version, content_status=args.content_status)
        elif args.command == "show":
            result = document(args.catalog, args.doc_id)
        elif args.command == "validate":
            result = validate(args.catalog)
        elif args.command == "agent":
            from pcb_assistant.catalog.agent import ask_agent, export_agent
            result = ask_agent(
                args.catalog, args.question, doc_type=args.type,
                source_type=args.source, tags=args.tag, limit=args.limit,
                offset=args.offset, max_sources=args.max_sources,
                source_version=args.version, content_status=args.content_status)
            if args.export:
                result = export_agent(
                    result, args.export, overwrite=args.overwrite,
                    catalog_directory=args.catalog)
        elif args.command == "evidence":
            from pcb_assistant.catalog.evidence import evidence, export_evidence
            result = evidence(args.catalog, args.query, doc_type=args.type,
                              source_type=args.source, tags=args.tag, limit=args.limit,
                              offset=args.offset, source_version=args.version,
                              content_status=args.content_status)
            if args.export:
                result = export_evidence(
                    result, args.export, overwrite=args.overwrite,
                    catalog_directory=args.catalog)
        elif args.command == "chat":
            from pcb_assistant.catalog.chat import main as chat_main
            chat_argv = [
                "--catalog", str(args.catalog),
                "--model", args.model,
                "--limit", str(args.limit),
                "--max-history-turns", str(args.max_history_turns),
                "--max-new-tokens", str(args.max_new_tokens),
            ]
            if args.question:
                chat_argv.extend(["ask", args.question])
                if args.as_json:
                    chat_argv.append("--json")
                if args.type:
                    chat_argv.extend(["--type", args.type])
                if args.source:
                    chat_argv.extend(["--source", args.source])
                for tag in args.tag:
                    chat_argv.extend(["--tag", tag])
                if args.version:
                    chat_argv.extend(["--version", args.version])
                if args.content_status:
                    chat_argv.extend(["--content-status", args.content_status])
                if args.export:
                    chat_argv.extend(["--export", str(args.export)])
                    if args.overwrite:
                        chat_argv.append("--overwrite")
            else:
                chat_argv.append("chat")
            return chat_main(chat_argv)
        elif args.command == "workbench":
            from pcb_assistant.interfaces.workbench import main as workbench_main
            wb_argv = [
                "--catalog", str(args.catalog),
                "--mode", args.mode,
                "--toolset", args.toolset,
            ]
            if args.case_id:
                wb_argv.extend(["--case", args.case_id])
            if args.cases_dir:
                wb_argv.extend(["--cases-dir", str(args.cases_dir)])
            if args.agent_config:
                wb_argv.extend(["--agent-config", str(args.agent_config)])
            if args.profile:
                wb_argv.extend(["--profile", args.profile])
            if args.as_json:
                wb_argv.append("--json")
            if args.mcp_config:
                wb_argv.append("--mcp-config")
            if args.question:
                wb_argv.append(args.question)
            return workbench_main(wb_argv)
        elif args.command == "web":
            from pcb_assistant.interfaces.web_workbench import main as web_main
            web_argv = [
                "--catalog", str(args.catalog),
                "--host", args.host,
                "--port", str(args.port),
                "--toolset", args.toolset,
            ]
            if args.cases_dir:
                web_argv.extend(["--cases-dir", str(args.cases_dir)])
            if args.agent_config:
                web_argv.extend(["--agent-config", str(args.agent_config)])
            if args.profile:
                web_argv.extend(["--profile", args.profile])
            if args.api_token:
                web_argv.extend(["--api-token", args.api_token])
            if args.open_browser:
                web_argv.append("--open")
            if args.desktop:
                web_argv.append("--desktop")
            if args.create_shortcut:
                web_argv.append("--create-shortcut")
            if args.reuse_running:
                web_argv.append("--reuse-running")
            if args.auto_port:
                web_argv.append("--auto-port")
            return web_main(web_argv)
        elif args.command == "search":
            result = search(args.catalog, args.query, doc_type=args.type, source_type=args.source,
                            tags=args.tag, limit=args.limit, offset=args.offset, deduplicate=not args.include_duplicates,
                            query_mode=args.mode, source_version=args.version, content_status=args.content_status)
        else:
            result = related(args.catalog, args.doc_id, direction=args.direction, limit=args.limit, offset=args.offset)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(result.get("status") == "failed")
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(json.dumps({"event": "catalog_error", "error_type": type(exc).__name__,
                          "error_code": str(exc) if isinstance(exc, CatalogError) else "operation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
