"""Small provider adapters for local and explicitly opted-in search."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import math
from typing import Protocol
from urllib.parse import urlparse

from catalog_common import CatalogError


class EvidenceProvider(Protocol):
    """Read-only search provider returning normalized evidence records."""

    def search(self, query: str, *, limit: int = 6) -> list[dict]: ...


class ExternalSearchAdapter:
    """Normalize an injected search function; this class performs no networking.

    The diagnosis workflow invokes this adapter only after an explicit opt-in
    and supplies a defect/cause-only query, never the raw case text.
    """

    def __init__(
        self,
        search: Callable[[str, int], list[Mapping]],
        *,
        provider_name: str = "external_search",
    ):
        if not callable(search):
            raise CatalogError("invalid_external_search_provider")
        self._search = search
        self.provider_name = str(provider_name or "external_search")[:80]

    def search(self, query: str, *, limit: int = 6) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise CatalogError("query_required")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise CatalogError("invalid_search_limit")
        try:
            hits = self._search(query.strip(), limit)
        except Exception as exc:
            raise CatalogError("external_search_failed") from exc
        if not isinstance(hits, list):
            raise CatalogError("invalid_external_search_response")

        normalized = []
        seen = set()
        for hit in hits[:limit]:
            if not isinstance(hit, Mapping):
                continue
            title = _bounded_text(hit.get("title"), 240)
            snippet = _bounded_text(hit.get("snippet") or hit.get("text"), 1200)
            url = _safe_url(hit.get("url"))
            if not title or not snippet or not url:
                continue
            try:
                score = float(hit.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if not math.isfinite(score):
                score = 0.0
            key = hashlib.sha256((url + "\n" + snippet).encode("utf-8")).hexdigest()
            citation_id = "EXC_WEB_" + key[:24]
            if citation_id in seen:
                continue
            seen.add(citation_id)
            normalized.append({
                "citation_id": citation_id,
                "doc_id": "WEB_" + key[:24],
                "source_id": url,
                "title": title,
                "url": url,
                "source_type": "external_web_search",
                "source_version": "unverified_search_result",
                "doc_type": "WEB",
                "evidence_status": "unverified_external",
                "independently_verified": False,
                "source_tier": "C",
                "tier_weight": 1,
                "text": snippet,
                "matched_terms": [],
                "retrieval_score": score,
                "provider": self.provider_name,
            })
        return normalized


def _bounded_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _safe_url(value) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 for char in value):
        return ""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or port == 0):
        return ""
    return value
