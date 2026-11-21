"""Read-only relationship paths and explainable similar-case ranking.

Both features are derived views over the retained catalog.  They never add
relationships, documents, or unresolved placeholders to the database.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import heapq
import json
import os
from pathlib import Path
import tempfile
import threading

from pcb_assistant.catalog.common import CatalogError, connect_readonly
from pcb_assistant.catalog.cli import _check_document_id, _check_page, _require_document
from pcb_assistant.knowledge import GENERIC, SYNONYMS, tokens


_MAX_PATH_DEPTH = 5
_TERM_LIMIT = 128
_TAG_POINTS = 4
_TERM_POINTS = 1
_SAME_SOURCE_POINTS = 3
_CACHE_SCHEMA = "phase3-insights-cache-v1"
_CACHE_DIRECTORY = ".phase3_insights_cache"
_WARM_LOCK = threading.Lock()
_WARM_THREADS = {}

_TERM_LABELS: dict[str, str] = {}
for _name, _terms in SYNONYMS.items():
    _label = _terms[0]
    for _term in _terms:
        _TERM_LABELS[_term.casefold()] = _label
    _TERM_LABELS["concept" + _name] = _label
_GENERIC_TERMS = {value.casefold() for value in GENERIC}


@dataclass(frozen=True)
class _CaseProfile:
    doc_id: str
    doc_type: str
    source_type: str
    source_id: str
    source_version: str
    title: str
    content_status: str
    evidence_status: str
    independently_verified: bool
    tags: tuple[tuple[str, str], ...]
    terms: frozenset[str]


@dataclass(frozen=True)
class _CaseSimilarityIndex:
    """Immutable bitmask index used to rank cases without per-request sets."""

    profiles: tuple[_CaseProfile, ...]
    positions: dict[str, int]
    term_bits: dict[str, int]
    tag_bits: dict[str, int]
    term_masks: tuple[int, ...]
    tag_masks: tuple[int, ...]
    source_keys: tuple[tuple[str, str], ...]


def _check_depth(max_depth):
    if type(max_depth) is not int or not 1 <= max_depth <= _MAX_PATH_DEPTH:
        raise CatalogError("invalid_path_parameters")


def _check_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise CatalogError("invalid_path_parameters")


def insight_cache_path(directory: Path) -> Path:
    """Return the derived cache path outside the immutable catalog directory."""
    directory = Path(directory).resolve()
    digest = hashlib.sha256(str(directory).encode("utf-8")).hexdigest()[:20]
    return directory.parent / _CACHE_DIRECTORY / (digest + ".json")


def _cache_header(directory, size, mtime_ns):
    return {
        "schema": _CACHE_SCHEMA,
        "catalog_path": str(directory),
        "catalog_size": size,
        "catalog_mtime_ns": mtime_ns,
    }


def _cache_matches(payload, directory, size, mtime_ns):
    return (isinstance(payload, dict)
            and payload.get("schema") == _CACHE_SCHEMA
            and payload.get("catalog_path") == str(directory)
            and payload.get("catalog_size") == size
            and payload.get("catalog_mtime_ns") == mtime_ns)


def _read_cache_file(path: Path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@lru_cache(maxsize=4)
def _persistent_payload(directory_value, size, mtime_ns):
    """Load one validated cache payload, or return None when it is stale."""
    payload = _read_cache_file(insight_cache_path(Path(directory_value)))
    if not _cache_matches(payload, Path(directory_value).resolve(), size, mtime_ns):
        return None
    return payload


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _write_cache_part(directory, size, mtime_ns, key, value) -> bool:
    """Atomically update one derived cache section without touching the catalog."""
    path = insight_cache_path(directory)
    payload = _read_cache_file(path)
    if not _cache_matches(payload, directory, size, mtime_ns):
        payload = _cache_header(directory, size, mtime_ns)
    payload[key] = value
    try:
        _atomic_write_json(path, payload)
    except OSError:
        return False
    _persistent_payload.cache_clear()
    return True


def _node(connection, doc_id):
    row = connection.execute("""SELECT doc_id,type,source_type,title,content_status,
        evidence_status,independently_verified FROM documents WHERE doc_id=?""", (doc_id,)).fetchone()
    if row is None:
        return None
    entry = dict(row)
    entry["independently_verified"] = bool(entry["independently_verified"])
    return entry


def _build_adjacency(rows):
    """Build the undirected view from relationship-id ordered unique rows."""
    adjacency = defaultdict(list)
    previous_id = None
    for edge in rows:
        relationship_id = edge["relationship_id"]
        if previous_id is not None and relationship_id <= previous_id:
            raise ValueError("relationship rows must be sorted and unique")
        previous_id = relationship_id
        adjacency[edge["from_id"]].append(edge)
        adjacency[edge["to_id"]].append(edge)
    return {node: tuple(edges) for node, edges in adjacency.items()}, len(rows)


def _relationship_rows(adjacency):
    """Serialize each undirected relationship once, ordered by identity."""
    by_id = {}
    for edges in adjacency.values():
        for edge in edges:
            by_id[edge["relationship_id"]] = edge
    return [by_id[relationship_id] for relationship_id in sorted(by_id)]


def _load_adjacency(connection):
    rows = [dict(row) for row in connection.execute("""SELECT relationship_id,from_id,to_id,relation
        FROM relationships ORDER BY relationship_id""")]
    return _build_adjacency(rows)


@lru_cache(maxsize=4)
def _relationship_graph(directory_value, _size, _mtime_ns):
    """Load or build an immutable read-only adjacency map for one snapshot."""
    payload = _persistent_payload(directory_value, _size, _mtime_ns)
    if payload is not None:
        cached = payload.get("relationship_graph")
        try:
            rows = cached["edges"]
            relationship_count = cached["relationship_count"]
            if type(relationship_count) is not int or relationship_count < 0:
                raise ValueError("invalid relationship count")
            if not isinstance(rows, list):
                raise ValueError("invalid relationship rows")
            adjacency, actual_count = _build_adjacency(rows)
            if actual_count != relationship_count:
                raise ValueError("relationship count mismatch")
            return adjacency, relationship_count
        except (KeyError, TypeError, ValueError):
            pass
    directory = Path(directory_value)
    with closing(connect_readonly(directory)) as connection:
        adjacency, relationship_count = _load_adjacency(connection)
    _write_cache_part(
        directory, _size, _mtime_ns, "relationship_graph",
        {"relationship_count": relationship_count,
         "edges": _relationship_rows(adjacency)})
    return adjacency, relationship_count


def warm_relationship_graph(directory: Path) -> int:
    """Build the read-only relationship cache used by path search."""
    directory = Path(directory).resolve()
    stat = (directory / "catalog.sqlite3").stat()
    _, relationship_count = _relationship_graph(
        str(directory), stat.st_size, stat.st_mtime_ns)
    return relationship_count


def _shortest_paths(adjacency, from_id, to_id, max_depth):
    """Return lexicographically stable shortest paths over undirected edges."""
    # Heap order makes the first path found for a node the shortest and then
    # lexicographically smallest by node and relationship IDs.
    heap = [(0, (from_id,), (), from_id)]
    best = {}
    found = {}
    while heap:
        depth, node_path, edge_path, current = heapq.heappop(heap)
        if current in best:
            continue
        best[current] = (depth, node_path, edge_path)
        if depth and (to_id is None or current == to_id):
            found[current] = (depth, node_path, edge_path)
        if current == to_id:
            break
        if depth >= max_depth:
            continue
        for edge in adjacency.get(current, ()):
            neighbor = edge["to_id"] if edge["from_id"] == current else edge["from_id"]
            if neighbor in best:
                continue
            heapq.heappush(heap, (depth + 1, node_path + (neighbor,),
                                  edge_path + (edge["relationship_id"],), neighbor))
    return found


def _edge_view(connection, edge_ids, node_path):
    if not edge_ids:
        return []
    rows = {row["relationship_id"]: dict(row) for row in connection.execute(
        "SELECT relationship_id,from_id,to_id,relation FROM relationships WHERE relationship_id IN (" +
        ",".join("?" for _ in edge_ids) + ")", edge_ids)}
    result = []
    for index, edge_id in enumerate(edge_ids):
        edge = rows[edge_id]
        current = node_path[index]
        result.append({
            "relationship_id": edge["relationship_id"],
            "relation": edge["relation"],
            "from_id": edge["from_id"],
            "to_id": edge["to_id"],
            "direction": "forward" if edge["from_id"] == current else "reverse",
        })
    return result


def relationship_paths(directory: Path, from_id: str, to_id: str | None = None, *,
                       max_depth=3, limit=20, wait_timeout=None) -> dict:
    """Find bounded shortest paths over registered relationships.

    Traversal is undirected so a knowledge record can be connected back to the
    mail or attachment that supplied it.  Unresolved references are deliberately
    excluded: this view cannot invent a node that is absent from the catalog.
    """
    _check_document_id(from_id)
    if to_id is not None:
        _check_document_id(to_id)
    _check_depth(max_depth)
    _check_limit(limit)
    directory = Path(directory).resolve()
    database = directory / "catalog.sqlite3"
    with closing(connect_readonly(directory)) as connection:
        _require_document(connection, from_id)
        if to_id is not None:
            _require_document(connection, to_id)
        if from_id == to_id:
            start = _node(connection, from_id)
            paths = [{"length": 0, "node_ids": [from_id], "nodes": [start], "edges": []}]
            total = 1
        else:
            if not wait_for_insights(directory, timeout=wait_timeout):
                raise CatalogError("insights_not_ready")
            stat = database.stat()
            adjacency, _ = _relationship_graph(
                str(directory), stat.st_size, stat.st_mtime_ns)
            found = _shortest_paths(adjacency, from_id, to_id, max_depth)
            ordered = sorted(found.values(), key=lambda item: (item[0], item[1], item[2]))
            total = len(ordered)
            paths = []
            node_cache = {}
            for depth, node_path, edge_path in ordered[:limit]:
                nodes = []
                for doc_id in node_path:
                    if doc_id not in node_cache:
                        node_cache[doc_id] = _node(connection, doc_id)
                    nodes.append(node_cache[doc_id])
                paths.append({
                    "length": depth,
                    "node_ids": list(node_path),
                    "nodes": nodes,
                    "edges": _edge_view(connection, edge_path, node_path),
                })
        from_document = _node(connection, from_id)
        to_document = _node(connection, to_id) if to_id is not None else None
    return {
        "from_id": from_id,
        "to_id": to_id,
        "from_document": from_document,
        "to_document": to_document,
        "max_depth": max_depth,
        "limit": limit,
        "total": total,
        "paths": paths,
        "traversal": "undirected_registered_relationships",
        "search_mode": "shortest_paths",
        "unresolved_references_included": False,
        "generated_relationships": False,
        "read_only": True,
    }


def _selected_metadata_texts(metadata, title):
    fields = metadata.get("fields") if isinstance(metadata, dict) else None
    texts = [title if isinstance(title, str) else ""]
    if isinstance(fields, dict):
        for value in fields.values():
            if not isinstance(value, list):
                continue
            for quote in value:
                if isinstance(quote, dict) and isinstance(quote.get("text"), str):
                    texts.append(quote["text"])
    selected = []
    size = 0
    for text in texts:
        piece = text.strip()[:1200]
        if not piece:
            continue
        if size + len(piece) > 12000:
            break
        selected.append(piece)
        size += len(piece)
    return tuple(selected)


@lru_cache(maxsize=16384)
def _term_set_for_text(text):
    terms = set()
    for term in tokens(text):
        label = _TERM_LABELS.get(term.casefold(), term.casefold())
        if label in _GENERIC_TERMS or not 2 <= len(label) <= 80:
            continue
        terms.add(label)
    return frozenset(terms)


def _metadata_terms(metadata, title):
    terms = set()
    for text in _selected_metadata_texts(metadata, title):
        terms.update(_term_set_for_text(text))
    return frozenset(sorted(terms, key=lambda value: (-len(value), value))[:_TERM_LIMIT])


def _profile_to_cache(profile):
    return {
        "doc_id": profile.doc_id,
        "doc_type": profile.doc_type,
        "source_type": profile.source_type,
        "source_id": profile.source_id,
        "source_version": profile.source_version,
        "title": profile.title,
        "content_status": profile.content_status,
        "evidence_status": profile.evidence_status,
        "independently_verified": profile.independently_verified,
        "tags": [[tag_id, label] for tag_id, label in profile.tags],
        "terms": sorted(profile.terms),
    }


def _profile_from_cache(value):
    if not isinstance(value, dict):
        raise ValueError("invalid case profile")
    required = {"doc_id", "doc_type", "source_type", "source_id", "source_version",
                "title", "content_status", "evidence_status",
                "independently_verified", "tags", "terms"}
    if not required.issubset(value):
        raise ValueError("incomplete case profile")
    if type(value["independently_verified"]) is not bool:
        raise ValueError("invalid verification flag")
    if not isinstance(value["tags"], list) or not isinstance(value["terms"], list):
        raise ValueError("invalid profile collections")
    tags = []
    for item in value["tags"]:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("invalid profile tag")
        tags.append((item[0], item[1]))
    if any(not isinstance(term, str) for term in value["terms"]):
        raise ValueError("invalid profile term")
    return _CaseProfile(
        doc_id=value["doc_id"], doc_type=value["doc_type"],
        source_type=value["source_type"], source_id=value["source_id"],
        source_version=value["source_version"], title=value["title"],
        content_status=value["content_status"], evidence_status=value["evidence_status"],
        independently_verified=value["independently_verified"],
        tags=tuple(tags), terms=frozenset(value["terms"]))


@lru_cache(maxsize=4)
def _case_profiles(directory_value: str, size: int, mtime_ns: int):
    payload = _persistent_payload(directory_value, size, mtime_ns)
    if payload is not None:
        cached = payload.get("case_profiles")
        if isinstance(cached, list):
            try:
                return tuple(_profile_from_cache(value) for value in cached)
            except (KeyError, TypeError, ValueError):
                pass
    directory = Path(directory_value)
    with closing(connect_readonly(directory)) as connection:
        rows = list(connection.execute("""SELECT doc_id,type,source_type,source_id,source_version,title,
            content_status,evidence_status,independently_verified,metadata_json FROM documents
            WHERE type='CASE' OR (type='KNOWLEDGE' AND source_type='case') ORDER BY doc_id"""))
        tags = defaultdict(list)
        for row in connection.execute("""SELECT dt.doc_id,t.tag_id,t.label FROM document_tags dt
            JOIN tags t USING(tag_id) JOIN documents d ON d.doc_id=dt.doc_id
            WHERE d.type='CASE' OR (d.type='KNOWLEDGE' AND d.source_type='case')
            ORDER BY dt.doc_id,t.tag_id"""):
            tags[row["doc_id"]].append((row["tag_id"], row["label"]))
    profiles = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, ValueError):
            metadata = {}
        profiles.append(_CaseProfile(
            doc_id=row["doc_id"], doc_type=row["type"], source_type=row["source_type"],
            source_id=row["source_id"], source_version=row["source_version"], title=row["title"],
            content_status=row["content_status"], evidence_status=row["evidence_status"],
            independently_verified=bool(row["independently_verified"]),
            tags=tuple(tags[row["doc_id"]]), terms=_metadata_terms(metadata, row["title"])))
    profiles = tuple(profiles)
    _write_cache_part(
        directory, size, mtime_ns, "case_profiles",
        [_profile_to_cache(profile) for profile in profiles])
    return profiles


def _build_similarity_index(profiles):
    """Build compact term/tag bitmasks once per immutable catalog snapshot."""
    term_bits = {}
    tag_bits = {}
    term_masks = []
    tag_masks = []
    for profile in profiles:
        term_mask = 0
        for term in profile.terms:
            bit = term_bits.get(term)
            if bit is None:
                bit = 1 << len(term_bits)
                term_bits[term] = bit
            term_mask |= bit
        tag_mask = 0
        for tag_id, _label in profile.tags:
            bit = tag_bits.get(tag_id)
            if bit is None:
                bit = 1 << len(tag_bits)
                tag_bits[tag_id] = bit
            tag_mask |= bit
        term_masks.append(term_mask)
        tag_masks.append(tag_mask)
    return _CaseSimilarityIndex(
        profiles=profiles,
        positions={profile.doc_id: position for position, profile in enumerate(profiles)},
        term_bits=term_bits,
        tag_bits=tag_bits,
        term_masks=tuple(term_masks),
        tag_masks=tuple(tag_masks),
        source_keys=tuple((profile.source_type, profile.source_id) for profile in profiles))


@lru_cache(maxsize=4)
def _case_similarity_index(directory_value: str, size: int, mtime_ns: int):
    """Return the warmed bitmask index for one validated catalog snapshot."""
    return _build_similarity_index(
        _case_profiles(directory_value, size, mtime_ns))


def warm_similar_cases(directory: Path) -> int:
    """Build the read-only case profile and similarity caches."""
    directory = Path(directory).resolve()
    database = directory / "catalog.sqlite3"
    stat = database.stat()
    index = _case_similarity_index(
        str(directory), stat.st_size, stat.st_mtime_ns)
    return len(index.profiles)


def warm_insights(directory: Path) -> dict:
    """Warm both derived insight views from one validated catalog snapshot."""
    directory = Path(directory).resolve()
    relationship_count = warm_relationship_graph(directory)
    case_count = warm_similar_cases(directory)
    # The immutable in-memory views now own the data; release the raw payload.
    _persistent_payload.cache_clear()
    return {"relationship_count": relationship_count, "case_count": case_count}
def _warm_key(directory: Path):
    stat = (directory / "catalog.sqlite3").stat()
    return str(directory), stat.st_size, stat.st_mtime_ns


def _run_insights_warmup(directory: Path) -> None:
    try:
        warm_insights(directory)
    except Exception:
        # The first related request retries synchronously and reports the
        # normal API error if the catalog is unavailable.
        pass


def warm_insights_async(directory: Path):
    """Warm both derived views in the background, coalescing duplicate starts."""
    directory = Path(directory).resolve()
    key = _warm_key(directory)
    with _WARM_LOCK:
        existing = _WARM_THREADS.get(key)
        if existing is not None:
            return existing
        thread = threading.Thread(
            target=_run_insights_warmup, args=(directory,),
            name="catalog-insights-warmup", daemon=True)
        _WARM_THREADS[key] = thread
        thread.start()
        return thread


def wait_for_insights(directory: Path, *, timeout: float | None = None) -> bool:
    """Wait for an in-flight warmup and report whether it finished in time.

    A timed-out warmup stays registered and keeps running as a daemon thread.
    Callers may therefore retry the wait without starting duplicate work.
    """
    directory = Path(directory).resolve()
    key = _warm_key(directory)
    with _WARM_LOCK:
        thread = _WARM_THREADS.get(key)
    if thread is None:
        return True
    if thread is threading.current_thread():
        return False
    thread.join(timeout)
    completed = not thread.is_alive()
    with _WARM_LOCK:
        if completed and _WARM_THREADS.get(key) is thread:
            _WARM_THREADS.pop(key, None)
    return completed


def _profile_for(connection, doc_id):
    row = connection.execute("""SELECT doc_id,type,source_type,source_id,source_version,title,
        content_status,evidence_status,independently_verified,metadata_json FROM documents
        WHERE doc_id=?""", (doc_id,)).fetchone()
    if row is None:
        raise CatalogError("document_not_found")
    if row["type"] != "CASE" and not (row["type"] == "KNOWLEDGE" and row["source_type"] == "case"):
        raise CatalogError("similar_cases_require_case")
    tag_rows = connection.execute("""SELECT t.tag_id,t.label FROM document_tags dt
        JOIN tags t USING(tag_id) WHERE dt.doc_id=? ORDER BY t.tag_id""", (doc_id,))
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, ValueError):
        metadata = {}
    return _CaseProfile(
        doc_id=row["doc_id"], doc_type=row["type"], source_type=row["source_type"],
        source_id=row["source_id"], source_version=row["source_version"], title=row["title"],
        content_status=row["content_status"], evidence_status=row["evidence_status"],
        independently_verified=bool(row["independently_verified"]),
        tags=tuple((item["tag_id"], item["label"]) for item in tag_rows),
        terms=_metadata_terms(metadata, row["title"]))


def similar_cases(directory: Path, doc_id: str, *, limit=20, offset=0,
                  wait_timeout=None) -> dict:
    """Rank other retained cases by explainable overlap, never by inference."""
    _check_document_id(doc_id)
    _check_page(limit, offset)
    directory = Path(directory).resolve()
    if not wait_for_insights(directory, timeout=wait_timeout):
        raise CatalogError("insights_not_ready")
    database = directory / "catalog.sqlite3"
    stat = database.stat()
    index = _case_similarity_index(
        str(directory), stat.st_size, stat.st_mtime_ns)
    profiles = index.profiles
    source_position = index.positions.get(doc_id)
    if source_position is None:
        with closing(connect_readonly(directory)) as connection:
            source = _profile_for(connection, doc_id)
    else:
        source = profiles[source_position]

    source_term_mask = 0
    for term in source.terms:
        bit = index.term_bits.get(term)
        if bit is not None:
            source_term_mask |= bit
    source_tag_mask = 0
    for tag_id, _label in source.tags:
        bit = index.tag_bits.get(tag_id)
        if bit is not None:
            source_tag_mask |= bit
    source_key = (source.source_type, source.source_id)
    term_masks = index.term_masks
    tag_masks = index.tag_masks
    source_keys = index.source_keys

    # Rank compact integer tuples.  Shared labels and terms are reconstructed
    # only for the requested page, keeping pagination and scores unchanged.
    ranked = []
    append = ranked.append
    for position, candidate in enumerate(profiles):
        if candidate.doc_id == source.doc_id:
            continue
        shared_tag_count = (tag_masks[position] & source_tag_mask).bit_count()
        shared_term_count = (term_masks[position] & source_term_mask).bit_count()
        same_source = source_keys[position] == source_key
        tag_points = shared_tag_count * _TAG_POINTS
        term_points = shared_term_count * _TERM_POINTS
        source_points = _SAME_SOURCE_POINTS if same_source else 0
        score = tag_points + term_points + source_points
        if score <= 0:
            continue
        append((
            -score, -shared_tag_count, -shared_term_count,
            candidate.doc_id, position))
    total = len(ranked)
    page_end = offset + limit
    rank_key = lambda item: item[:4]
    if page_end < total:
        selected = heapq.nsmallest(page_end, ranked, key=rank_key)[offset:]
    else:
        ranked.sort(key=rank_key)
        selected = ranked[offset:]

    source_tags = dict(source.tags)
    results = []
    for _score_key, _tag_key, _term_key, _doc_key, position in selected:
        candidate = profiles[position]
        shared_tags = tuple(
            (tag_id, label) for tag_id, label in candidate.tags
            if tag_id in source_tags)
        shared_terms = candidate.terms & source.terms
        same_source = source_keys[position] == source_key
        tag_points = len(shared_tags) * _TAG_POINTS
        term_points = len(shared_terms) * _TERM_POINTS
        source_points = _SAME_SOURCE_POINTS if same_source else 0
        score = tag_points + term_points + source_points
        results.append({
            "doc_id": candidate.doc_id,
            "type": candidate.doc_type,
            "source_type": candidate.source_type,
            "source_id": candidate.source_id,
            "source_version": candidate.source_version,
            "title": candidate.title,
            "content_status": candidate.content_status,
            "evidence_status": candidate.evidence_status,
            "independently_verified": candidate.independently_verified,
            "tags": [{"tag_id": tag_id, "label": label} for tag_id, label in candidate.tags],
            "shared_tags": [{"tag_id": tag_id, "label": label} for tag_id, label in shared_tags],
            "shared_terms": sorted(shared_terms, key=lambda value: (-len(value), value)),
            "same_source": same_source,
            "score": score,
            "score_components": {
                "shared_tags": tag_points,
                "shared_terms": term_points,
                "same_source": source_points,
            },
        })
    return {
        "doc_id": doc_id,
        "eligible": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": results,
        "score_model": {
            "shared_tag_points": _TAG_POINTS,
            "shared_term_points": _TERM_POINTS,
            "same_source_points": _SAME_SOURCE_POINTS,
            "term_limit_per_case": _TERM_LIMIT,
        },
        "method": "shared_topics_verbatim_field_terms_and_source_identity",
        "derived_recommendation": True,
        "not_source_evidence": True,
        "read_only": True,
        "writes_catalog": False,
    }