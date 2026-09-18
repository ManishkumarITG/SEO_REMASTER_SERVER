"""Single-URL fetcher. Kept reusable for the future site-wide crawler."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from exceptions.crawler import FetchError
from services.crawler.parser import PageData, parse_html

logger = logging.getLogger("seo.crawler")

USER_AGENT = "SEOAnalyzerBot/0.1"
TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 5
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}

#: Hard ceiling on how much of a response we read into memory. The URL is
#: user-supplied, so without this a single audit of a huge (or deliberately
#: endless) response reads until the process runs out of memory - selectolax
#: then holds a second copy of it. 5 MiB is far more HTML than any real page;
#: anything larger is truncated and analyzed as far as it got.
MAX_BYTES = 5 * 1024 * 1024
#: robots.txt is a small text file; a multi-megabyte one is a trap, not a config.
MAX_ROBOTS_BYTES = 512 * 1024



async def read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most max_bytes of a streaming response. Returns (body, truncated)."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            truncated = True
            break
    body = b"".join(chunks)[:max_bytes]
    return body, truncated


def _decode(response: httpx.Response, body: bytes) -> str:
    """Decode using the response's declared charset, never raising on bad bytes."""
    encoding = response.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:  # a charset name the codec registry does not know
        return body.decode("utf-8", errors="replace")


async def fetch_page(url: str) -> PageData:
    """Fetch url (following up to MAX_REDIRECTS), plus its robots.txt, into a PageData.

    The body is read in chunks and capped at MAX_BYTES: the audited URL comes
    from the client, so the response size is not ours to trust.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=httpx.Timeout(TIMEOUT_SECONDS),
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and content_type not in _HTML_CONTENT_TYPES:
                    # Rejected before reading the body: no point pulling megabytes
                    # of a PDF or a video into memory to then throw it away.
                    raise FetchError(
                        f"Unsupported content type {content_type!r} - only HTML pages can be audited"
                    )
                body, truncated = await read_capped(response, MAX_BYTES)
                load_time_ms = round((time.perf_counter() - started) * 1000, 2)
                status_code = response.status_code
                final_url = str(response.url)
                redirect_chain = [str(r.url) for r in response.history]
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                html = _decode(response, body)
            if truncated:
                logger.warning(
                    "Response for %s exceeded %d bytes - analyzing the first %d bytes only",
                    url, MAX_BYTES, len(body),
                )
            robots_txt = await _fetch_robots(client, final_url)
    except httpx.TooManyRedirects as exc:
        raise FetchError(f"Too many redirects (limit {MAX_REDIRECTS}) fetching {url}") from exc
    except httpx.TimeoutException as exc:
        raise FetchError(f"Timed out after {TIMEOUT_SECONDS:.0f}s fetching {url}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc

    return PageData(
        requested_url=url,
        final_url=final_url,
        status_code=status_code,
        redirect_chain=redirect_chain,
        headers=response_headers,
        html=html,
        dom=parse_html(html),
        load_time_ms=load_time_ms,
        fetched_at=datetime.now(UTC),
        robots_txt=robots_txt,
        truncated=truncated,
    )


async def _fetch_robots(client: httpx.AsyncClient, final_url: str) -> dict[str, Any]:
    """Best-effort robots.txt fetch: status plus the rules that apply to all crawlers."""
    parts = urlsplit(final_url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    info: dict[str, Any] = {
        "url": robots_url,
        "fetched": False,
        "status_code": None,
        "disallow": [],
        "allow": [],
        "sitemaps": [],
    }
    try:
        async with client.stream("GET", robots_url, timeout=5.0) as response:
            info["fetched"] = True
            info["status_code"] = response.status_code
            if response.status_code != 200:
                return info
            body, _ = await read_capped(response, MAX_ROBOTS_BYTES)
            text = _decode(response, body)
    except httpx.HTTPError:
        return info
    _parse_robots(text, info)
    return info


def _parse_robots(text: str, info: dict[str, Any]) -> None:
    applies_to_all = False
    for raw_line in text.splitlines()[:500]:
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            applies_to_all = value == "*"
        elif directive == "sitemap":
            info["sitemaps"].append(value)
        elif applies_to_all and directive in ("disallow", "allow") and value:
            info[directive].append(value)
