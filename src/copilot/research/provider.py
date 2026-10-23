"""Configured web search providers (Brave Search API and Tavily).

Only the search API is contacted; source pages are never fetched, which avoids
SSRF and keeps the tool read-only. Results are bounded, deduplicated, stripped
of HTML/control characters, and accepted only with a public http(s) URL.
API keys stay in request headers and are never logged.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..config import Settings
from ..errors import ResearchProviderError
from ..util.text import strip_control_tokens, truncate

log = logging.getLogger(__name__)

USER_AGENT = "community-copilot/0.1 (+local community assistant)"
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 800
MAX_URL_CHARS = 2000
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class WebSource:
    """One search result; the URL is the citation and is stored verbatim."""

    title: str
    url: str
    snippet: str
    provider: str


@runtime_checkable
class WebSearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int) -> list[WebSource]:
        """Return bounded results for one query; raise ResearchProviderError on failure."""
        ...


def _clean_text(raw: object, limit: int) -> str:
    if raw is None:
        return ""
    text = strip_control_tokens(html.unescape(_TAG_RE.sub(" ", str(raw))))
    return truncate(" ".join(text.split()), limit)


def _is_public_host(host: str) -> bool:
    host = host.strip().strip(".").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def safe_source_url(raw: object) -> str | None:
    """Accept only a public, credential-free http(s) URL (fragment dropped)."""
    if raw is None:
        return None
    try:
        parts = urlsplit(str(raw).strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    if not _is_public_host(parts.hostname):
        return None
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    if len(cleaned) > MAX_URL_CHARS:
        return None
    return cleaned


class _BaseProvider:
    name = "base"

    def __init__(self, *, api_key: str, timeout_s: float, max_results: int) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_results = max_results

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Send one request with at most one retry for transient failures."""
        last_error = "unknown error"
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_s,
                    follow_redirects=False,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                ) as client:
                    response = await client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError):
                last_error = "timeout or connection error"
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise ResearchProviderError(
                    detail=f"{self.name} search: {last_error}"
                ) from None
            if response.status_code in _RETRY_STATUSES and attempt == 0:
                last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(0.5)
                continue
            if response.status_code >= 400:
                raise ResearchProviderError(
                    detail=f"{self.name} search returned HTTP {response.status_code}"
                )
            if response.is_redirect:
                raise ResearchProviderError(detail=f"{self.name} search returned a redirect")
            return response
        raise ResearchProviderError(detail=f"{self.name} search failed: {last_error}")

    def _results(self, payload: object) -> list[WebSource]:
        raise NotImplementedError

    async def search(self, query: str, *, limit: int) -> list[WebSource]:
        response = await self._request(query, limit)
        try:
            payload = response.json()
        except ValueError:
            raise ResearchProviderError(detail=f"{self.name} search returned invalid JSON") from None
        rows = self._results(payload)
        out: list[WebSource] = []
        seen: set[str] = set()
        for row in rows:
            url = safe_source_url(row.get("url"))
            if url is None or url in seen:
                continue
            seen.add(url)
            title = _clean_text(row.get("title"), MAX_TITLE_CHARS) or url
            snippet = _clean_text(row.get("snippet"), MAX_SNIPPET_CHARS)
            out.append(WebSource(title=title, url=url, snippet=snippet, provider=self.name))
            if len(out) >= min(limit, self._max_results):
                break
        return out

    async def _request(self, query: str, limit: int) -> httpx.Response:
        raise NotImplementedError


class BraveSearchProvider(_BaseProvider):
    name = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    async def _request(self, query: str, limit: int) -> httpx.Response:
        return await self._send(
            "GET",
            self.ENDPOINT,
            params={
                "q": query,
                "count": min(limit, self._max_results),
                "safesearch": "strict",
                "text_decorations": "false",
                "extra_snippets": "false",
            },
            headers={"X-Subscription-Token": self._api_key},
        )

    def _results(self, payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        web = payload.get("web")
        results = web.get("results") if isinstance(web, dict) else None
        if not isinstance(results, list):
            return []
        return [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("description") or item.get("snippet"),
            }
            for item in results
            if isinstance(item, dict)
        ]


class TavilySearchProvider(_BaseProvider):
    name = "tavily"
    ENDPOINT = "https://api.tavily.com/search"

    async def _request(self, query: str, limit: int) -> httpx.Response:
        return await self._send(
            "POST",
            self.ENDPOINT,
            json={
                "query": query,
                "max_results": min(limit, self._max_results),
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def _results(self, payload: object) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content"),
            }
            for item in results
            if isinstance(item, dict)
        ]


def build_web_search_provider(settings: Settings) -> WebSearchProvider | None:
    """Return a configured provider, or None when research is unavailable."""
    if settings.research_unavailable_reason is not None:
        return None
    if settings.web_search_provider == "brave":
        return BraveSearchProvider(
            api_key=settings.web_search_api_key,
            timeout_s=settings.web_search_timeout_s,
            max_results=settings.web_search_max_results,
        )
    if settings.web_search_provider == "tavily":
        return TavilySearchProvider(
            api_key=settings.web_search_api_key,
            timeout_s=settings.web_search_timeout_s,
            max_results=settings.web_search_max_results,
        )
    return None
