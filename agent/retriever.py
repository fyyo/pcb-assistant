"""Read-only evidence retrieval adapter for the lamination agent."""
from __future__ import annotations

from pathlib import Path
from contextlib import closing
import json

from pcb_assistant.catalog.common import CatalogError, DEFAULT_CATALOG, connect_readonly
from pcb_assistant.catalog.evidence import evidence
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


class CatalogRetriever:
    """Thin read-only adapter over the existing catalog evidence API."""

    def __init__(self, directory: Path = DEFAULT_CATALOG, *, limit: int = 6,
                 filters: dict | None = None):
        self.directory = Path(directory)
        self.limit = limit
        self.filters = dict(filters or {})

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
        return normalize_evidence(result)
