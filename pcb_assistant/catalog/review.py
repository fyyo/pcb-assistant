"""Local, auditable human review for similar-case recommendations.

The review store is deliberately separate from the immutable catalog.  This
module can create a deterministic review queue, record anonymous reviewer
labels with revision history, and aggregate only counts and agreement metrics.
It never generates labels and never treats aggregate labels as an engineering
acceptance decision.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import time

from pcb_assistant.catalog.common import CatalogError, ROOT, connect_readonly, digest_file, digest_text, json_text, stable_id
from pcb_assistant.catalog.insights import similar_cases, warm_insights


DEFAULT_REVIEW_ROOT = ROOT / "data/phase3_review_v1"
DEFAULT_REVIEWERS = ("R01", "R02")

RELEVANCE_LABELS = (
    "relevant",
    "partially_relevant",
    "not_relevant",
    "uncertain",
)
ENGINEERING_LABELS = (
    "supported",
    "plausible_unverified",
    "not_supported",
    "insufficient_evidence",
)
REASON_CODES = (
    "same_problem",
    "same_process_stage",
    "same_material_stackup",
    "shared_process_conditions",
    "measured_values_present",
    "verification_record_present",
    "recommendation_only",
    "generic_topic_only",
    "unrelated_context",
    "conflicting_evidence",
    "missing_conditions",
    "insufficient_context",
)

_REVIEW_SCHEMA = "phase3-recommendation-review-v1"
_QUEUE_SCHEMA = "phase3-review-queue-v1"
_LABEL_SCHEMA = "phase3-review-label-v1"
_QUEUE_ID_PATTERN = re.compile(r"REVIEW_[0-9a-f]{32}")
_REVIEW_ID_PATTERN = re.compile(r"RITEM_[0-9a-f]{32}")
_REVIEWER_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_MAX_NOTE_LENGTH = 2000
_LOCK_TIMEOUT_SECONDS = 15.0


def _validate_integer(name, value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CatalogError("invalid_review_parameters")


def _validate_bool(name, value):
    if type(value) is not bool:
        raise CatalogError("invalid_review_parameters")


def _validate_reviewer_id(value):
    if not isinstance(value, str) or not _REVIEWER_PATTERN.fullmatch(value):
        raise CatalogError("invalid_reviewer_id")


def _validate_reviewers(values):
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 5:
        raise CatalogError("invalid_reviewers")
    result = []
    for value in values:
        _validate_reviewer_id(value)
        if value in result:
            raise CatalogError("duplicate_reviewer_id")
        result.append(value)
    return tuple(result)


def _validate_reason_codes(values):
    if not isinstance(values, (tuple, list)) or not values or len(values) > len(REASON_CODES):
        raise CatalogError("invalid_reason_codes")
    if any(not isinstance(value, str) or value not in REASON_CODES for value in values):
        raise CatalogError("invalid_reason_codes")
    if len(set(values)) != len(values):
        raise CatalogError("duplicate_reason_code")
    return tuple(sorted(values))


def _validate_note(value):
    if not isinstance(value, str) or len(value) > _MAX_NOTE_LENGTH:
        raise CatalogError("invalid_review_note")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise CatalogError("invalid_review_note")
    return value


def _validate_expected_revision(value):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise CatalogError("invalid_expected_revision")
    return value


def _review_directory(review_root: Path, queue_id: str) -> Path:
    if not isinstance(queue_id, str) or not _QUEUE_ID_PATTERN.fullmatch(queue_id):
        raise CatalogError("invalid_review_queue_id")
    root = Path(review_root).resolve()
    directory = (root / queue_id).resolve()
    if not directory.is_relative_to(root):
        raise CatalogError("invalid_review_queue_path")
    return directory


def _check_review_root(review_root: Path, catalog: Path) -> Path:
    root = Path(review_root).resolve()
    catalog = Path(catalog).resolve()
    if root == catalog or root.is_relative_to(catalog) or catalog.is_relative_to(root):
        raise CatalogError("review_directory_overlaps_catalog")
    return root


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, UnicodeError, ValueError) as exc:
        raise CatalogError("review_data_corrupt") from exc


def _read_jsonl(path: Path):
    rows = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("record must be an object")
                rows.append(value)
    except (OSError, UnicodeError, ValueError) as exc:
        raise CatalogError("review_data_corrupt") from exc
    return rows


def _atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
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


def _atomic_write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            for row in rows:
                stream.write(json_text(row))
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


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CatalogError("review_store_busy")
                    time.sleep(0.01)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _catalog_snapshot(directory: Path, case_count: int) -> dict:
    database = directory / "catalog.sqlite3"
    with closing(connect_readonly(directory)) as connection:
        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    version_ids = []
    registry = directory / "versions" / "version_registry.jsonl"
    if registry.is_file():
        for row in _read_jsonl(registry):
            version_id = row.get("version_id")
            if isinstance(version_id, str):
                version_ids.append(version_id)
    return {
        "database_sha256": digest_file(database),
        "document_count": document_count,
        "case_count": case_count,
        "version_ids": sorted(version_ids),
    }


def _case_document_ids(directory: Path):
    with closing(connect_readonly(directory)) as connection:
        return [row[0] for row in connection.execute(
            "SELECT doc_id FROM documents WHERE type='CASE' ORDER BY doc_id")]


def _queue_digest(items) -> str:
    return digest_text("".join(json_text(row) + "\n" for row in items))


def _review_item(queue_id, source_id, candidate, rank):
    return {
        "schema": _REVIEW_SCHEMA,
        "review_id": stable_id("RITEM", queue_id, source_id, candidate["doc_id"], str(rank)),
        "source_doc_id": source_id,
        "candidate_doc_id": candidate["doc_id"],
        "candidate_type": candidate["type"],
        "candidate_source_type": candidate["source_type"],
        "rank": rank,
        "score": candidate["score"],
        "score_components": dict(candidate["score_components"]),
        "shared_tags": list(candidate["shared_tags"]),
        "shared_terms": list(candidate["shared_terms"]),
        "same_source": bool(candidate["same_source"]),
    }


def create_review_queue(catalog: Path, review_root: Path = DEFAULT_REVIEW_ROOT, *,
                        sample_size=200, limit=10, seed=20261022,
                        reviewers=DEFAULT_REVIEWERS, include_same_source=False) -> dict:
    """Create one deterministic, source-text-free review queue.

    The queue is stored below ``review_root`` and contains document identifiers,
    rank/score snapshots, and explainable overlap fields only.  It never writes
    to the catalog or source assets.
    """
    _validate_integer("sample_size", sample_size, 1, 1000)
    _validate_integer("limit", limit, 1, 200)
    _validate_integer("seed", seed, 0, 2**32 - 1)
    _validate_bool("include_same_source", include_same_source)
    reviewers = _validate_reviewers(reviewers)
    catalog = Path(catalog).resolve()
    review_root = _check_review_root(review_root, catalog)

    warm_insights(catalog)
    case_ids = _case_document_ids(catalog)
    if not case_ids:
        raise CatalogError("no_cases_for_review")
    sampled_count = min(sample_size, len(case_ids))
    sampled_ids = sorted(random.Random(seed).sample(case_ids, sampled_count))

    candidate_rows = []
    sources_with_candidates = 0
    for source_id in sampled_ids:
        result = similar_cases(catalog, source_id, limit=200, offset=0)
        candidates = [item for item in result["results"]
                      if include_same_source or not item.get("same_source")]
        if not candidates:
            continue
        sources_with_candidates += 1
        for rank, candidate in enumerate(candidates[:limit], 1):
            candidate_rows.append((source_id, candidate, rank))

    if not candidate_rows:
        raise CatalogError("no_review_items")

    snapshot = _catalog_snapshot(catalog, len(case_ids))
    selection_digest = digest_text("".join(
        json_text({
            "source_doc_id": source_id,
            "candidate_doc_id": candidate["doc_id"],
            "rank": rank,
        }) + "\n"
        for source_id, candidate, rank in candidate_rows))
    queue_id = stable_id("REVIEW", _QUEUE_SCHEMA, snapshot["database_sha256"],
                         str(sample_size), str(limit), str(seed),
                         ",".join(reviewers), str(include_same_source), selection_digest)
    items = [_review_item(queue_id, source_id, candidate, rank)
             for source_id, candidate, rank in candidate_rows]
    queue_digest = _queue_digest(items)
    manifest = {
        "schema": _QUEUE_SCHEMA,
        "queue_id": queue_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selection": {
            "sample_size": sample_size,
            "sampled_sources": sampled_count,
            "sources_with_candidates": sources_with_candidates,
            "limit": limit,
            "seed": seed,
            "include_same_source": include_same_source,
            "selection_method": "deterministic_random_case_documents",
        },
        "reviewers": list(reviewers),
        "item_count": len(items),
        "review_slots": len(items) * len(reviewers),
        "queue_sha256": queue_digest,
        "catalog_snapshot": snapshot,
        "derived_recommendation": True,
        "not_source_evidence": True,
        "labels_generated_by_model": False,
        "training_allowed": False,
        "published": False,
        "external_mail_transmission": False,
    }

    review_root.mkdir(parents=True, exist_ok=True)
    queue_dir = review_root / queue_id
    try:
        queue_dir.mkdir()
    except FileExistsError as exc:
        raise CatalogError("review_queue_exists") from exc

    _atomic_write_jsonl(queue_dir / "queue.jsonl", items)
    _atomic_write_jsonl(queue_dir / "labels.jsonl", [])
    (queue_dir / "labels.lock").write_bytes(b"0")
    _atomic_write_json(queue_dir / "manifest.json", manifest)
    return {
        "status": "created",
        "queue_id": queue_id,
        "review_root": str(review_root),
        "item_count": len(items),
        "review_slots": manifest["review_slots"],
        "reviewers": list(reviewers),
        "sampled_sources": sampled_count,
        "sources_with_candidates": sources_with_candidates,
        "queue_sha256": queue_digest,
        "labels_generated_by_model": False,
        "catalog_writes": False,
        "contains_source_prose": False,
    }


def _load_queue(review_root: Path, queue_id: str):
    queue_dir = _review_directory(review_root, queue_id)
    manifest_path = queue_dir / "manifest.json"
    queue_path = queue_dir / "queue.jsonl"
    labels_path = queue_dir / "labels.jsonl"
    if not manifest_path.is_file() or not queue_path.is_file() or not labels_path.is_file():
        raise CatalogError("review_queue_not_found")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema") != _QUEUE_SCHEMA:
        raise CatalogError("review_data_corrupt")
    if manifest.get("queue_id") != queue_id:
        raise CatalogError("review_data_corrupt")
    try:
        reviewers = _validate_reviewers(manifest.get("reviewers"))
    except CatalogError as exc:
        raise CatalogError("review_data_corrupt") from exc
    items = _read_jsonl(queue_path)
    if len(items) != manifest.get("item_count") or not items:
        raise CatalogError("review_data_corrupt")
    if _queue_digest(items) != manifest.get("queue_sha256"):
        raise CatalogError("review_data_corrupt")
    seen = set()
    for item in items:
        if item.get("schema") != _REVIEW_SCHEMA:
            raise CatalogError("review_data_corrupt")
        review_id = item.get("review_id")
        if not isinstance(review_id, str) or not _REVIEW_ID_PATTERN.fullmatch(review_id):
            raise CatalogError("review_data_corrupt")
        if review_id in seen:
            raise CatalogError("review_data_corrupt")
        seen.add(review_id)
        if not isinstance(item.get("source_doc_id"), str) or not isinstance(item.get("candidate_doc_id"), str):
            raise CatalogError("review_data_corrupt")
        if type(item.get("rank")) is not int or item["rank"] < 1:
            raise CatalogError("review_data_corrupt")
    manifest["reviewers"] = list(reviewers)
    return queue_dir, manifest, items


def _latest_labels(labels, items, reviewers, queue_id):
    item_map = {item["review_id"]: item for item in items}
    reviewer_set = set(reviewers)
    latest = {}
    revisions = defaultdict(list)
    for row in labels:
        if row.get("schema") != _LABEL_SCHEMA or row.get("queue_id") != queue_id:
            raise CatalogError("review_data_corrupt")
        review_id = row.get("review_id")
        reviewer_id = row.get("reviewer_id")
        revision = row.get("revision")
        if review_id not in item_map or reviewer_id not in reviewer_set:
            raise CatalogError("review_data_corrupt")
        if type(revision) is not int or revision < 1:
            raise CatalogError("review_data_corrupt")
        if row.get("relevance_label") not in RELEVANCE_LABELS:
            raise CatalogError("review_data_corrupt")
        if row.get("engineering_label") not in ENGINEERING_LABELS:
            raise CatalogError("review_data_corrupt")
        try:
            _validate_reason_codes(row.get("reason_codes"))
            _validate_note(row.get("note", ""))
        except CatalogError as exc:
            raise CatalogError("review_data_corrupt") from exc
        if not isinstance(row.get("submitted_at"), str) or not row["submitted_at"].strip():
            raise CatalogError("review_data_corrupt")
        if (row.get("source_doc_id") != item_map[review_id]["source_doc_id"]
                or row.get("candidate_doc_id") != item_map[review_id]["candidate_doc_id"]):
            raise CatalogError("review_data_corrupt")
        revisions[(review_id, reviewer_id)].append(revision)
        latest[(review_id, reviewer_id)] = row
    for values in revisions.values():
        if values != list(range(1, len(values) + 1)):
            raise CatalogError("review_data_corrupt")
    return latest


def _read_labels(queue_dir, manifest, items):
    with _exclusive_file_lock(queue_dir / "labels.lock"):
        labels = _read_jsonl(queue_dir / "labels.jsonl")
        return _latest_labels(labels, items, manifest["reviewers"], manifest["queue_id"])


def submit_review(review_root: Path, queue_id: str, review_id: str, reviewer_id: str, *,
                  relevance_label: str, engineering_label: str, reason_codes,
                  note="", expected_revision=None, submitted_at=None) -> dict:
    """Record one local review revision without modifying the catalog."""
    _validate_reviewer_id(reviewer_id)
    if not isinstance(review_id, str) or not _REVIEW_ID_PATTERN.fullmatch(review_id):
        raise CatalogError("invalid_review_id")
    if relevance_label not in RELEVANCE_LABELS:
        raise CatalogError("invalid_relevance_label")
    if engineering_label not in ENGINEERING_LABELS:
        raise CatalogError("invalid_engineering_label")
    reason_codes = _validate_reason_codes(reason_codes)
    note = _validate_note(note)
    expected_revision = _validate_expected_revision(expected_revision)
    queue_dir, manifest, items = _load_queue(review_root, queue_id)
    if reviewer_id not in manifest["reviewers"]:
        raise CatalogError("reviewer_not_assigned")
    item = next((value for value in items if value["review_id"] == review_id), None)
    if item is None:
        raise CatalogError("review_item_not_found")

    with _exclusive_file_lock(queue_dir / "labels.lock"):
        labels = _read_jsonl(queue_dir / "labels.jsonl")
        latest = _latest_labels(labels, items, manifest["reviewers"], queue_id)
        current = latest.get((review_id, reviewer_id))
        if current is None:
            if expected_revision not in (None, 0):
                raise CatalogError("review_revision_conflict")
            revision = 1
        else:
            if expected_revision is None or expected_revision != current["revision"]:
                raise CatalogError("duplicate_review_submission")
            revision = current["revision"] + 1
        timestamp = submitted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise CatalogError("invalid_review_timestamp")
        record = {
            "schema": _LABEL_SCHEMA,
            "queue_id": queue_id,
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "source_doc_id": item["source_doc_id"],
            "candidate_doc_id": item["candidate_doc_id"],
            "revision": revision,
            "relevance_label": relevance_label,
            "engineering_label": engineering_label,
            "reason_codes": list(reason_codes),
            "note": note,
            "submitted_at": timestamp,
        }
        labels.append(record)
        _atomic_write_jsonl(queue_dir / "labels.jsonl", labels)
    return {
        "status": "recorded",
        "queue_id": queue_id,
        "review_id": review_id,
        "reviewer_id": reviewer_id,
        "revision": revision,
        "relevance_label": relevance_label,
        "engineering_label": engineering_label,
        "labels_generated_by_model": False,
        "catalog_writes": False,
        "contains_source_prose": False,
    }


def review_next(review_root: Path, queue_id: str, reviewer_id: str, *, limit=10) -> dict:
    """Return this reviewer's next unlabelled items, without other labels."""
    _validate_reviewer_id(reviewer_id)
    _validate_integer("limit", limit, 1, 200)
    _queue_dir, manifest, items = _load_queue(review_root, queue_id)
    if reviewer_id not in manifest["reviewers"]:
        raise CatalogError("reviewer_not_assigned")
    latest = _read_labels(_queue_dir, manifest, items)
    remaining = [item for item in items if (item["review_id"], reviewer_id) not in latest]
    return {
        "status": "ok",
        "queue_id": queue_id,
        "reviewer_id": reviewer_id,
        "total": len(items),
        "remaining": len(remaining),
        "items": remaining[:limit],
        "read_only": True,
        "catalog_writes": False,
        "contains_source_prose": False,
    }


def _evidence_document(connection, doc_id: str, max_chars: int) -> dict:
    row = connection.execute("""
        SELECT d.doc_id,d.type,d.source_type,d.source_version,d.title,
               d.content_status,d.evidence_status,d.independently_verified,
               d.format,d.extraction_method,
               COALESCE(c.normalized_text,c.original_text) AS evidence_text
        FROM documents d JOIN contents c USING(content_id)
        WHERE d.doc_id=?
    """, (doc_id,)).fetchone()
    if row is None:
        raise CatalogError("review_evidence_document_not_found")
    text = row["evidence_text"] if isinstance(row["evidence_text"], str) else ""
    tags = [dict(tag) for tag in connection.execute("""
        SELECT t.tag_id,t.label,dt.origin
        FROM document_tags dt JOIN tags t USING(tag_id)
        WHERE dt.doc_id=? ORDER BY t.tag_id,dt.origin
    """, (doc_id,))]
    returned = text[:max_chars]
    return {
        "doc_id": row["doc_id"],
        "type": row["type"],
        "source_type": row["source_type"],
        "source_version": row["source_version"],
        "title": row["title"],
        "content_status": row["content_status"],
        "evidence_status": row["evidence_status"],
        "independently_verified": bool(row["independently_verified"]),
        "format": row["format"],
        "extraction_method": row["extraction_method"],
        "tags": tags,
        "text": returned,
        "text_kind": "normalized_or_original",
        "text_characters": len(text),
        "returned_characters": len(returned),
        "text_truncated": len(text) > max_chars,
    }


def review_evidence(catalog: Path, review_root: Path, queue_id: str, review_id: str,
                    reviewer_id: str, *, max_chars=12000) -> dict:
    """Show one item's local source and candidate evidence without writing data.

    Source prose is returned only for a local reviewer inspecting one queued
    recommendation.  The output must not be persisted in aggregate reports or
    transmitted externally.
    """
    _validate_reviewer_id(reviewer_id)
    if not isinstance(review_id, str) or not _REVIEW_ID_PATTERN.fullmatch(review_id):
        raise CatalogError("invalid_review_id")
    _validate_integer("max_chars", max_chars, 1, 100000)
    _queue_dir, manifest, items = _load_queue(review_root, queue_id)
    if reviewer_id not in manifest["reviewers"]:
        raise CatalogError("reviewer_not_assigned")
    item = next((value for value in items if value["review_id"] == review_id), None)
    if item is None:
        raise CatalogError("review_item_not_found")

    expected_documents = manifest.get("catalog_snapshot", {}).get("document_count")
    with closing(connect_readonly(Path(catalog))) as connection:
        actual_documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if actual_documents != expected_documents:
            raise CatalogError("review_catalog_snapshot_mismatch")
        source = _evidence_document(connection, item["source_doc_id"], max_chars)
        candidate = _evidence_document(connection, item["candidate_doc_id"], max_chars)

    return {
        "status": "ok",
        "queue_id": queue_id,
        "reviewer_id": reviewer_id,
        "review_id": review_id,
        "rank": item["rank"],
        "score": item["score"],
        "score_components": dict(item["score_components"]),
        "shared_tags": list(item["shared_tags"]),
        "shared_terms": list(item["shared_terms"]),
        "same_source": bool(item["same_source"]),
        "source_document": source,
        "candidate_document": candidate,
        "max_chars_per_document": max_chars,
        "text_truncated": source["text_truncated"] or candidate["text_truncated"],
        "evidence_scope": "one_local_review_item",
        "local_review_only": True,
        "read_only": True,
        "review_data_writes": False,
        "catalog_writes": False,
        "contains_source_prose": True,
        "labels_generated_by_model": False,
        "training_allowed": False,
        "published": False,
        "external_mail_transmission": False,
    }


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _wilson_interval(successes, total, z=1.96):
    if not total:
        return {"lower": None, "upper": None}
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
              / denominator)
    return {"lower": round(max(0.0, center - margin), 4),
            "upper": round(min(1.0, center + margin), 4)}


def _cohen_kappa(pairs):
    if not pairs:
        return None
    total = len(pairs)
    observed = sum(left == right for left, right in pairs) / total
    left_counts = Counter(left for left, _right in pairs)
    right_counts = Counter(right for _left, right in pairs)
    categories = set(left_counts) | set(right_counts)
    expected = sum((left_counts[value] / total) * (right_counts[value] / total)
                   for value in categories)
    if expected >= 1 - 1e-12:
        return None
    return round((observed - expected) / (1 - expected), 4)


def _distribution(rows, key, categories):
    counts = Counter(row[key] for row in rows)
    return {category: counts.get(category, 0) for category in categories}


def build_review_report(review_root: Path, queue_id: str) -> dict:
    """Aggregate local review labels without emitting document-level results."""
    _queue_dir, manifest, items = _load_queue(review_root, queue_id)
    latest = _read_labels(_queue_dir, manifest, items)
    rows = list(latest.values())
    reviewers = manifest["reviewers"]
    item_count = len(items)
    total_slots = item_count * len(reviewers)
    submitted_slots = len(rows)

    item_reviewers = defaultdict(set)
    for review_id, reviewer_id in latest:
        item_reviewers[review_id].add(reviewer_id)
    fully_reviewed = sum(len(item_reviewers[item["review_id"]]) == len(reviewers)
                         for item in items)
    partially_reviewed = sum(0 < len(item_reviewers[item["review_id"]]) < len(reviewers)
                             for item in items)
    unreviewed = item_count - fully_reviewed - partially_reviewed

    relevance_pairs = []
    engineering_pairs = []
    unanimous_relevance = split_relevance = 0
    unanimous_engineering = split_engineering = 0
    for item in items:
        item_rows = [latest[(item["review_id"], reviewer_id)]
                     for reviewer_id in reviewers
                     if (item["review_id"], reviewer_id) in latest]
        if len(item_rows) == len(reviewers):
            relevance_values = {row["relevance_label"] for row in item_rows}
            engineering_values = {row["engineering_label"] for row in item_rows}
            if len(relevance_values) == 1:
                unanimous_relevance += 1
            else:
                split_relevance += 1
            if len(engineering_values) == 1:
                unanimous_engineering += 1
            else:
                split_engineering += 1
        for left, right in itertools.combinations(item_rows, 2):
            relevance_pairs.append((left["relevance_label"], right["relevance_label"]))
            engineering_pairs.append((left["engineering_label"], right["engineering_label"]))

    relevance_agreement = sum(left == right for left, right in relevance_pairs)
    engineering_agreement = sum(left == right for left, right in engineering_pairs)
    if submitted_slots == 0:
        status = "awaiting_human_labels"
    elif submitted_slots < total_slots:
        status = "in_progress"
    else:
        status = "review_complete_aggregate_only"

    reason_counts = Counter()
    for row in rows:
        reason_counts.update(row["reason_codes"])
    return {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evaluation_kind": "human_label_aggregation_not_model_validation",
        "queue": {
            "queue_id": queue_id,
            "sample": dict(manifest["selection"]),
            "reviewers": list(reviewers),
            "item_count": item_count,
            "review_slots": total_slots,
            "catalog_snapshot": dict(manifest["catalog_snapshot"]),
        },
        "coverage": {
            "submitted_slots": submitted_slots,
            "total_slots": total_slots,
            "slot_coverage_rate": _rate(submitted_slots, total_slots),
            "slot_coverage_ci95": _wilson_interval(submitted_slots, total_slots),
            "fully_reviewed_items": fully_reviewed,
            "partially_reviewed_items": partially_reviewed,
            "unreviewed_items": unreviewed,
            "item_full_coverage_rate": _rate(fully_reviewed, item_count),
            "item_full_coverage_ci95": _wilson_interval(fully_reviewed, item_count),
        },
        "label_distribution": {
            "relevance": _distribution(rows, "relevance_label", RELEVANCE_LABELS),
            "engineering": _distribution(rows, "engineering_label", ENGINEERING_LABELS),
            "reason_codes": {code: reason_counts.get(code, 0) for code in REASON_CODES},
        },
        "agreement": {
            "items_with_all_reviews": fully_reviewed,
            "items_with_pairwise_reviews": len({item["review_id"] for item in items
                                                if len(item_reviewers[item["review_id"]]) >= 2}),
            "relevance_pairs_compared": len(relevance_pairs),
            "engineering_pairs_compared": len(engineering_pairs),
            "relevance_exact_agreement_rate": _rate(relevance_agreement, len(relevance_pairs)),
            "engineering_exact_agreement_rate": _rate(engineering_agreement, len(engineering_pairs)),
            "relevance_exact_agreement_ci95": _wilson_interval(
                relevance_agreement, len(relevance_pairs)),
            "engineering_exact_agreement_ci95": _wilson_interval(
                engineering_agreement, len(engineering_pairs)),
            "relevance_cohen_kappa": _cohen_kappa(relevance_pairs),
            "engineering_cohen_kappa": _cohen_kappa(engineering_pairs),
        },
        "consensus": {
            "fully_reviewed_items": fully_reviewed,
            "unanimous_relevance_items": unanimous_relevance,
            "split_relevance_items": split_relevance,
            "unanimous_engineering_items": unanimous_engineering,
            "split_engineering_items": split_engineering,
        },
        "interpretation": {
            "human_labels_present": submitted_slots > 0,
            "label_origin": "local_operator_entry",
            "reviewer_identity_verified": False,
            "model_generated_labels": False,
            "human_relevance_verdict": "not_established",
            "engineering_validity_verdict": "not_established",
            "note": "Aggregate labels describe this review queue only; they are not an expert acceptance, production qualification, or proof of engineering effectiveness.",
        },
        "limitations": [
            "Labels are self-declared local review records; reviewer identity is not independently verified.",
            "The queue is a deterministic sample and does not by itself estimate population-wide precision.",
            "Agreement measures consistency between reviewers, not correctness.",
            "No engineering conclusion is inferred from recommendation overlap or label counts.",
        ],
        "read_only": True,
        "review_data_writes": False,
        "catalog_writes": False,
        "contains_source_prose": False,
        "contains_document_level_results": False,
        "labels_generated_by_model": False,
        "training_allowed": False,
        "published": False,
        "external_mail_transmission": False,
    }


__all__ = [
    "DEFAULT_REVIEWERS", "DEFAULT_REVIEW_ROOT", "ENGINEERING_LABELS",
    "REASON_CODES", "RELEVANCE_LABELS", "build_review_report",
    "create_review_queue", "review_evidence", "review_next", "submit_review",
]
