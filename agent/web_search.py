"""Opt-in SearXNG JSON search transport, with no third-party dependencies."""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from catalog_common import CatalogError

from .providers import ExternalSearchAdapter


DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _search_endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip() or len(base_url) > 2048:
        raise CatalogError("invalid_searxng_url")
    value = base_url.strip()
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise CatalogError("invalid_searxng_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CatalogError("invalid_searxng_url") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port == 0):
        raise CatalogError("invalid_searxng_url")
    path = parsed.path.rstrip("/")
    if not path.endswith("/search"):
        path += "/search"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class SearXNGSearchAdapter(ExternalSearchAdapter):
    """Search a caller-selected SearXNG instance when the workflow opts in.

    This adapter performs one bounded GET per search, without redirects or
    automatic retries. It does not fetch result pages or write catalog data.
    """

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.endpoint = _search_endpoint(base_url)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise CatalogError("invalid_search_timeout")
        self.timeout = float(timeout)
        super().__init__(self._request, provider_name="searxng")

    def _request(self, query: str, limit: int) -> list[Mapping]:
        params = urlencode({"q": query, "format": "json"})
        request = Request(
            self.endpoint + "?" + params,
            headers={"Accept": "application/json", "User-Agent": "pcb-lamination-agent/1.0"},
            method="GET",
        )
        with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CatalogError("external_search_response_too_large")
        payload = json.loads(raw)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            raise CatalogError("invalid_external_search_response")
        return [
            {
                "title": hit.get("title"),
                "snippet": hit.get("content"),
                "url": hit.get("url"),
                "score": hit.get("score"),
            }
            for hit in payload["results"][:limit] if isinstance(hit, Mapping)
        ]
