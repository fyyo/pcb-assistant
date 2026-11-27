"""Read-only evidence retrieval adapter for the lamination agent."""
from __future__ import annotations

from pathlib import Path
from contextlib import closing
import hashlib
import json

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG, connect_readonly
from pcb_assistant.catalog.evidence import MAX_EXCERPT_CHARS, evidence
from pcb_assistant.knowledge import term_groups
from .source_policy import is_ocr_evidence, public_source_tier


SOURCE_TIER_WEIGHTS = {"A": 3, "B": 2, "C": 1}


def _upgrade(tier: str) -> str:
    return {"C": "B", "B": "A", "A": "A"}.get(tier, tier)


def source_tier(item: dict) -> str:
    """Classify a catalog record without treating any source as verified truth."""
    if is_ocr_evidence(item):
        return "C"
    doc_type = str(item.get("type") or "").upper()
    source_type = str(item.get("source_type") or "").casefold()
    title = str(item.get("title") or "").casefold()
    if doc_type == "PUBLIC" or source_type == "public_reference":
        tier = public_source_tier(item.get("public_source") or {})
    elif doc_type == "CASE" or source_type == "case":
        tier = "B"
    elif doc_type == "KNOWLEDGE" and any(
            marker in title for marker in ("pdf 附件", "xlsx 附件", "docx 附件", "pptx 附件")):
        # Native document text is still unverified, but it is stronger than an
        # OCR fragment or an email sentence.
        tier = "B"
    elif doc_type == "MAIL":
        tier = "C"
    else:
        tier = "C"
    if item.get("independently_verified"):
        tier = _upgrade(tier)
    return tier


def normalize_evidence(result: dict) -> list[dict]:
    """Flatten a catalog evidence package into citable, ranked excerpts."""
    items = []
    seen = set()
    for group in result.get("results") or []:
        tier = source_tier(group)
        score = float(group.get("retrieval_score") or 0.0)
        for excerpt in group.get("excerpts") or []:
            excerpt_id = excerpt.get("excerpt_id")
            if not excerpt_id or excerpt_id in seen:
                continue
            seen.add(excerpt_id)
            items.append({
                "citation_id": excerpt_id,
                "doc_id": group.get("doc_id"),
                "source_id": group.get("source_id"),
                "title": group.get("title") or "",
                "source_type": group.get("source_type") or "",
                "source_version": group.get("source_version") or "",
                "doc_type": group.get("type") or "",
                "evidence_status": group.get("evidence_status") or "",
                "independently_verified": bool(group.get("independently_verified")),
                "source_tier": tier,
                "tier_weight": SOURCE_TIER_WEIGHTS[tier],
                "text": excerpt.get("text") or "",
                "start": excerpt.get("start"),
                "end": excerpt.get("end"),
                "sha256": excerpt.get("sha256"),
                "matched_terms": list(excerpt.get("matched_terms") or []),
                "retrieval_score": score,
                "locator": (group.get("citation") or {}).get("locator") or {},
                **({"ocr_values_independently_verified": False} if is_ocr_evidence(group) else {}),
                **({"url": group["public_source"].get("url"),
                    "locator": (group.get("citation") or {}).get("locator"),
                    "provenance": {key: group["public_source"].get(key) for key in (
                        "publisher", "kind", "document_revision", "revision_status", "rights_status", "split",
                        "sha256",
                    )}}
                   if group.get("public_source") else {}),
            })
    # SQLite FTS5 bm25 scores are negative; a smaller value is a better match.
    items.sort(key=lambda item: (
        -item["tier_weight"], item["retrieval_score"], item["citation_id"]
    ))
    return items


def _literal_slice(text: str, query: str, max_chars: int = MAX_EXCERPT_CHARS) -> tuple[int, int, str, list[str]]:
    """Return one bounded literal slice from document text without rewriting it."""
    if not text:
        return 0, 0, "", []
    groups = term_groups(query) if query else []
    flat_terms = [term for group in groups for term in group if term]
    lowered = text.casefold()
    hit_start = None
    matched = []
    for term in flat_terms:
        idx = lowered.find(term.casefold())
        if idx >= 0:
            if hit_start is None or idx < hit_start:
                hit_start = idx
            if term not in matched:
                matched.append(term)
    if hit_start is None:
        start = 0
        end = min(len(text), max_chars)
    else:
        start = max(0, hit_start - 80)
        end = min(len(text), start + max_chars)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    snippet = text[start:end]
    if not matched and flat_terms:
        snippet_lower = snippet.casefold()
        matched = [term for term in flat_terms if term.casefold() in snippet_lower]
    return start, end, snippet, matched


def _excerpt_id(content_id: str, start: int, end: int, excerpt_text: str) -> str:
    identity = f"{content_id}\0{start}\0{end}\0{excerpt_text}"
    return "EXC_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _linked_ids(connection, doc_id: str, relations: tuple[str, ...], limit: int = 30) -> list[str]:
    placeholders = ",".join("?" for _ in relations)
    rows = connection.execute(
        f"""
        SELECT from_id, to_id FROM relationships
        WHERE (from_id = ? OR to_id = ?) AND relation IN ({placeholders})
        ORDER BY relationship_id
        LIMIT ?
        """,
        (doc_id, doc_id, *relations, limit),
    ).fetchall()
    linked = []
    for row in rows:
        other = row["to_id"] if row["from_id"] == doc_id else row["from_id"]
        if other and other != doc_id and other not in linked:
            linked.append(other)
    return linked


def related_attachment_excerpts(
    directory: Path,
    doc_ids: list[str] | tuple[str, ...],
    *,
    query: str = "",
    limit_per_doc: int = 2,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> list[dict]:
    """Trace multi-hop relationships (CASE/KNOWLEDGE -> MAIL -> ATTACHMENT -> KNOWLEDGE).

    Resolves both direct mail attachments and 2-hop attachments linked via a
    CASE's DERIVED_FROM parent MAIL record, returning citable literal slices.
    """
    clean_ids = [doc_id for doc_id in dict.fromkeys(doc_ids or ()) if isinstance(doc_id, str) and doc_id]
    if not clean_ids:
        return []
    items = []
    seen_excerpts = set()
    with closing(connect_readonly(directory)) as connection:
        for seed_id in clean_ids:
            mail_ids: list[str] = []
            case_ids: list[str] = []
            attachment_ids: list[str] = []
            direct_knowledge_ids: list[str] = []

            if seed_id.startswith("ATTACHMENT_"):
                attachment_ids.append(seed_id)
            elif seed_id.startswith("MAIL_"):
                mail_ids.append(seed_id)
            elif seed_id.startswith("CASE_"):
                case_ids.append(seed_id)
            else:
                # KNOWLEDGE_ or PUBLIC_: check parent CASE_ / ATTACHMENT_ / MAIL_
                for other in _linked_ids(connection, seed_id, ("EXTRACTED_FROM", "DERIVED_FROM", "ATTACHED_TO"), limit=20):
                    if other.startswith("ATTACHMENT_") and other not in attachment_ids:
                        attachment_ids.append(other)
                    elif other.startswith("MAIL_") and other not in mail_ids:
                        mail_ids.append(other)
                    elif other.startswith("CASE_") and other not in case_ids:
                        case_ids.append(other)

            # Hop 1: CASE_ -> DERIVED_FROM -> MAIL_ (and any direct attachment links)
            for case_id in case_ids[:5]:
                for other in _linked_ids(connection, case_id, ("DERIVED_FROM", "ATTACHED_TO"), limit=20):
                    if other.startswith("MAIL_") and other not in mail_ids:
                        mail_ids.append(other)
                    elif other.startswith("ATTACHMENT_") and other not in attachment_ids:
                        attachment_ids.append(other)

            # Hop 2: MAIL_ -> ATTACHED_TO -> ATTACHMENT_
            for mail_id in mail_ids[:5]:
                for other in _linked_ids(connection, mail_id, ("ATTACHED_TO", "EXTRACTED_FROM"), limit=30):
                    if other.startswith("ATTACHMENT_") and other not in attachment_ids:
                        attachment_ids.append(other)
                    elif other.startswith("KNOWLEDGE_") and other != seed_id and other not in direct_knowledge_ids:
                        direct_knowledge_ids.append(other)

            # Hop 3: ATTACHMENT_ -> EXTRACTED_FROM -> readable KNOWLEDGE_
            candidate_doc_ids: list[str] = []
            for att_id in attachment_ids[:20]:
                if att_id != seed_id and att_id not in candidate_doc_ids:
                    candidate_doc_ids.append(att_id)
                for other in _linked_ids(connection, att_id, ("EXTRACTED_FROM",), limit=20):
                    if other != seed_id and other not in candidate_doc_ids:
                        candidate_doc_ids.append(other)
            for kid in direct_knowledge_ids[:10]:
                if kid != seed_id and kid not in candidate_doc_ids:
                    candidate_doc_ids.append(kid)

            added = 0
            for target_id in candidate_doc_ids:
                if added >= limit_per_doc:
                    break
                row = connection.execute(
                    """
                    SELECT d.doc_id, d.type, d.source_type, d.source_id, d.source_version,
                           d.title, d.content_id, d.evidence_status, d.independently_verified,
                           d.locator_json, c.original_text, c.normalized_text
                    FROM documents d
                    JOIN contents c ON c.content_id = d.content_id
                    WHERE d.doc_id = ?
                    """,
                    (target_id,),
                ).fetchone()
                if not row:
                    continue
                body = row["original_text"] or row["normalized_text"] or ""
                if not body.strip():
                    continue
                start, end, snippet, matched = _literal_slice(body, query, max_chars=max_chars)
                if not snippet:
                    continue
                exc_id = _excerpt_id(row["content_id"], start, end, snippet)
                if exc_id in seen_excerpts:
                    continue
                seen_excerpts.add(exc_id)
                group_like = {
                    "type": row["type"],
                    "source_type": row["source_type"],
                    "title": row["title"] or "",
                    "evidence_status": row["evidence_status"] or "",
                    "independently_verified": bool(row["independently_verified"]),
                }
                tier = source_tier(group_like)
                try:
                    locator = json.loads(row["locator_json"] or "{}")
                except ValueError:
                    locator = {}
                items.append({
                    "citation_id": exc_id,
                    "excerpt_id": exc_id,
                    "doc_id": row["doc_id"],
                    "related_from_doc_id": seed_id,
                    "source_id": row["source_id"],
                    "title": row["title"] or "",
                    "source_type": row["source_type"] or "",
                    "source_version": row["source_version"] or "",
                    "doc_type": row["type"] or "",
                    "type": row["type"] or "",
                    "evidence_status": row["evidence_status"] or "",
                    "independently_verified": bool(row["independently_verified"]),
                    "source_tier": tier,
                    "tier_weight": SOURCE_TIER_WEIGHTS[tier],
                    "text": snippet,
                    "start": start,
                    "end": end,
                    "sha256": hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
                    "matched_terms": matched,
                    "retrieval_score": 0.0,
                    "locator": locator if isinstance(locator, dict) else {},
                    **({"ocr_values_independently_verified": False}
                       if is_ocr_evidence(group_like) else {}),
                })
                added += 1
    items.sort(key=lambda item: (-item["tier_weight"], item["citation_id"]))
    return items


class CatalogRetriever:
    """Thin read-only adapter over the existing catalog evidence API."""

    def __init__(self, directory: Path = DEFAULT_CATALOG, *, limit: int = 6,
                 filters: dict | None = None,
                 include_related_attachments: bool = False):
        self.directory = Path(directory)
        self.limit = limit
        self.filters = dict(filters or {})
        self.include_related_attachments = include_related_attachments

    def search(self, query: str, *, limit: int | None = None) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise CatalogError("query_required")
        effective_limit = self.limit if limit is None else limit
        result = evidence(
            self.directory,
            query.strip(),
            limit=effective_limit,
            ranking_terms=term_groups(query),
            **{target: self.filters[key] for key, target in (
                ("doc_type", "doc_type"), ("source", "source_type"),
                ("tags", "tags"), ("version", "source_version"),
                ("content_status", "content_status")) if key in self.filters},
        )
        public_groups = [group for group in result.get("results") or []
                         if group.get("source_type") == "public_reference"]
        if public_groups:
            ids = [group["doc_id"] for group in public_groups]
            with closing(connect_readonly(self.directory)) as connection:
                metadata = {
                    row["doc_id"]: json.loads(row["metadata_json"]).get("public_source", {})
                    for row in connection.execute(
                        "SELECT doc_id,metadata_json FROM documents WHERE doc_id IN ("
                        + ",".join("?" for _ in ids) + ")", ids)
                }
            for group in public_groups:
                group["public_source"] = metadata.get(group["doc_id"], {})
        items = normalize_evidence(result)
        if self.include_related_attachments and items:
            seed_docs = [
                item["doc_id"] for item in items[:3]
                if item.get("doc_id") and item.get("doc_type") in ("MAIL", "ATTACHMENT", "CASE")
            ]
            if seed_docs:
                seen_ids = {item["citation_id"] for item in items}
                related_items = related_attachment_excerpts(
                    self.directory, seed_docs, query=query.strip(), limit_per_doc=2,
                )
                for rel in related_items:
                    if rel["citation_id"] not in seen_ids:
                        if self.filters.get("source") and rel.get("source_type") != self.filters["source"]:
                            continue
                        if self.filters.get("doc_type") and rel.get("doc_type") != self.filters["doc_type"]:
                            continue
                        seen_ids.add(rel["citation_id"])
                        items.append(rel)
                items.sort(key=lambda item: (
                    -item["tier_weight"], item["retrieval_score"], item["citation_id"]
                ))
        return items
