"""Pooled HTTP primitives for crawling many URLs on one host.

`fetcher.fetch_page` is the right tool for a single audited page and the wrong
one for a catalog: it builds a new `httpx.AsyncClient` per call (each one
constructs an SSLContext and loads the CA bundle - minutes of pure CPU at a
thousand pages), always fetches robots.txt, and hard-rejects any non-HTML
content type. It is left untouched; this module imports its constants and
capped-read helpers and adds what bulk work needs:

- ONE pooled client per crawl, created by the caller and passed in. Never a
  module-level singleton: `AsyncClient` binds its pool to the event loop that
  opened the connections - the same class of bug `db/mongo.py::get_client`
  already documents for `AsyncMongoClient`.
- Capped JSON and text GETs that record the content type instead of gating on
  it (some proxies serve products.json as text/json), and that never raise for
  a non-2xx (tier selection has to tell a 403 from a 404).
- A head-only HTML read that stops at `</head>` or `<body` - or at a byte cap,
  because on some themes the <head> alone exceeds 89 KiB and the sentinel never
  arrives inside a small budget. The cap is the real bound; the sentinel is the
  bonus that usually fires first.
- An SSRF guard. `normalize_store_url` accepts `http://169.254.169.254/`, and
  this feature turns "fetch one page" into "fetch hundreds of URLs listed in a
  document the target host controls". The same-host rule the discovery layer
  applies to sitemap URLs is the control that stops amplification.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from services.crawler.fetcher import MAX_REDIRECTS, USER_AGENT, _decode, read_capped
from services.crawler.parser import first_attr, first_text, meta_by_property, parse_html
from services.stores.models import OriginInfo

logger = logging.getLogger("seo.crawler.http")

DEFAULT_LIMITS = httpx.Limits(max_connections=24, max_keepalive_connections=12, keepalive_expiry=30.0)
#: Split what fetcher.TIMEOUT_SECONDS lumps together. A dead host should cost
#: 5 s (connect), not 15, and a head-only read never legitimately needs 15 s.
CRAWL_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=10.0)

#: products.json page 1 measured 1.6 MB at limit=250; the sitemap that listed
#: 2,201 products was 1.5 MB. 8 MiB leaves headroom without inviting a bomb.
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
#: 64 KiB recovered title+description on 4/4 test pages, but </head> arrived at
#: 89 KiB on one theme. 256 KiB makes the cap a safety net, not the normal exit,
#: while still saving 58-75% of the bytes of a 500 KiB-1.2 MiB product page.
MAX_HEAD_BYTES = 256 * 1024
#: Escape hatch when the meta tags were not inside the first cap.
MAX_HEAD_RETRY_BYTES = 768 * 1024
HEAD_SENTINELS: tuple[bytes, ...] = (b"</head", b"<body")
#: Homepage read for Shopify markers / site name; the head is all we need.
MAX_HOMEPAGE_BYTES = 256 * 1024

_SHOPIFY_MARKERS = ("cdn.shopify.com", "Shopify.theme", "/cdn/shop/")


# --- Client ----------------------------------------------------------------------


def build_crawl_client(
    *,
    user_agent: str | None = None,
    limits: httpx.Limits = DEFAULT_LIMITS,
    timeout: httpx.Timeout = CRAWL_TIMEOUT,
    http2: bool = False,
) -> httpx.AsyncClient:
    """One pooled client for one crawl. The caller owns its lifetime."""
    kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "max_redirects": MAX_REDIRECTS,
        "limits": limits,
        "timeout": timeout,
        "headers": {"User-Agent": user_agent or USER_AGENT},
    }
    if http2:
        try:
            import h2  # noqa: F401 - probe only; httpx needs it installed to negotiate HTTP/2

            kwargs["http2"] = True
        except ImportError:
            # Under HTTP/1.1 an aborted body read closes the connection, so every
            # head-only fetch pays a fresh TLS handshake. HTTP/2 aborts a stream and
            # keeps the connection. Worth having, not worth failing over.
            logger.info("h2 is not installed - crawling over HTTP/1.1 (pip install 'httpx[http2]')")
    return httpx.AsyncClient(**kwargs)


@asynccontextmanager
async def crawl_client(**kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
    client = build_crawl_client(**kwargs)
    try:
        yield client
    finally:
        await client.aclose()


# --- SSRF guard -------------------------------------------------------------------


def is_public_host(host: str) -> bool:
    """False for loopback, private, link-local, reserved or multicast targets.

    Resolution happens here and again inside httpx, which leaves a DNS-rebinding
    window; closing it properly needs a custom transport and is not worth it for
    this feature. The amplification control is the same-host rule on every
    sitemap-derived URL, not this check.
    """
    if not host:
        return False
    if host.lower() in ("localhost",) or host.lower().endswith(".localhost"):
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        # Unresolvable: let the fetch fail with a real network error instead of
        # a misleading "not public" refusal.
        return True
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
        if ip.is_unspecified:
            return False
    return True


# --- Results ------------------------------------------------------------------


@dataclass(slots=True)
class HttpResult:
    url: str
    final_url: str = ""
    status_code: int | None = None
    content_type: str = ""
    body: bytes = b""
    text: str = ""
    truncated: bool = False
    stopped_early: bool = False
    elapsed_ms: float = 0.0
    error: str = ""
    redirect_chain: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and self.status_code is not None and 200 <= self.status_code < 300


@dataclass(slots=True)
class JsonResult:
    url: str
    status_code: int | None = None
    payload: Any = None
    content_type: str = ""
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and self.payload is not None and not self.error


# --- Reads --------------------------------------------------------------------


async def read_until_sentinel(
    response: httpx.Response,
    max_bytes: int,
    sentinels: tuple[bytes, ...] = HEAD_SENTINELS,
) -> tuple[bytes, bool, bool]:
    """Read until a sentinel appears or max_bytes is reached.

    Returns (body, hit_sentinel, truncated). A sibling of `fetcher.read_capped`,
    not an option on it: this needs a third fact - WHY it stopped. Sentinel-hit
    means "we have everything we wanted" and must not be reported as truncated,
    or every successful head read would light up `PageData.truncated`. It also
    needs a tail buffer so a sentinel split across two chunks is still found.
    """
    lowered = tuple(s.lower() for s in sentinels)
    tail_len = max(len(s) for s in lowered) - 1
    chunks: list[bytes] = []
    total = 0
    tail = b""
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        window = (tail + chunk).lower()
        hit = min((i for i in (window.find(s) for s in lowered) if i != -1), default=-1)
        if hit != -1:
            # Keep everything up to and including the sentinel's line so the
            # closing tag itself is in the parsed text.
            cut = total - len(window) + hit + tail_len + 1
            body = b"".join(chunks)[: max(cut, 0) + 64]
            return body, True, False
        if total >= max_bytes:
            return b"".join(chunks)[:max_bytes], False, True
        tail = window[-tail_len:] if tail_len > 0 else b""
    return b"".join(chunks), False, False


async def get_capped(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    params: dict[str, Any] | None = None,
    sentinels: tuple[bytes, ...] | None = None,
    accept: str | None = None,
) -> HttpResult:
    """GET with a byte cap. Never raises; transport errors land in `.error`."""
    result = HttpResult(url=url)
    headers = {"Accept": accept} if accept else None
    started = time.perf_counter()
    try:
        async with client.stream("GET", url, params=params, headers=headers) as response:
            result.status_code = response.status_code
            result.final_url = str(response.url)
            result.redirect_chain = [str(r.url) for r in response.history]
            result.content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if sentinels:
                body, hit, truncated = await read_until_sentinel(response, max_bytes, sentinels)
                result.stopped_early = hit
            else:
                body, truncated = await read_capped(response, max_bytes)
            result.body = body
            result.truncated = truncated
            result.text = _decode(response, body)
    except httpx.TooManyRedirects:
        result.error = f"too many redirects (limit {MAX_REDIRECTS})"
    except httpx.TimeoutException as exc:
        result.error = f"timeout: {type(exc).__name__}"
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:200]
    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return result


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_bytes: int = MAX_JSON_BYTES,
) -> JsonResult:
    """Capped JSON GET. A truncated document is never partially parsed - that would invent data."""
    raw = await get_capped(client, url, max_bytes=max_bytes, params=params, accept="application/json")
    result = JsonResult(url=url, status_code=raw.status_code, content_type=raw.content_type,
                        truncated=raw.truncated, error=raw.error)
    if raw.error or raw.status_code != 200:
        return result
    if raw.truncated:
        result.error = f"response exceeded {max_bytes} bytes"
        logger.info("JSON at %s exceeded %d bytes - ignored", url, max_bytes)
        return result
    head = raw.body.lstrip()[:1]
    if head not in (b"{", b"["):
        # HTML served at a .json path is common; do not hand 8 MiB of it to json.loads.
        result.error = f"not JSON (content-type {raw.content_type or 'unknown'})"
        return result
    try:
        result.payload = json.loads(raw.body)
    except (ValueError, UnicodeDecodeError) as exc:
        result.error = f"invalid JSON: {type(exc).__name__}"
    return result


async def get_text(client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_XML_BYTES) -> HttpResult:
    """Capped text GET - sitemaps and robots.txt. Content type is recorded, never enforced."""
    return await get_capped(client, url, max_bytes=max_bytes, accept="application/xml,text/xml,text/plain;q=0.9,*/*;q=0.5")


async def get_head_html(client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_HEAD_BYTES) -> HttpResult:
    """Read a page only as far as its <head>."""
    return await get_capped(client, url, max_bytes=max_bytes, sentinels=HEAD_SENTINELS,
                            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.1")


# --- Origin resolution ----------------------------------------------------------


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def resolve_origin(client: httpx.AsyncClient, normalized_url: str) -> OriginInfo | None:
    """Follow redirects ONCE and pin the effective origin for every URL we build.

    Every catalog URL is `f"{origin}/products/{handle}"`. Built from the requested
    origin when the store redirects apex->www, each one is one redirect off its
    canonical and `canonical_mismatch` fires on every item. A GET rather than a
    HEAD because some CDNs 405 HEAD, and the homepage <head> gives us the
    Shopify markers, og:site_name and currency for free.
    """
    raw = await get_capped(client, normalized_url, max_bytes=MAX_HOMEPAGE_BYTES, sentinels=HEAD_SENTINELS,
                           accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.1")
    if raw.error or raw.status_code is None:
        return None
    dom = parse_html(raw.text)
    site_name = first_attr(dom, 'meta[property="og:site_name"]', "content") or first_text(dom, "title")
    currency = (meta_by_property(dom, "og:price:currency") or "").strip()
    return OriginInfo(
        requested_url=normalized_url,
        origin=origin_of(raw.final_url or normalized_url),
        final_url=raw.final_url or normalized_url,
        status_code=raw.status_code,
        redirect_chain=raw.redirect_chain,
        is_shopify=any(marker in raw.text for marker in _SHOPIFY_MARKERS),
        site_name=" ".join(site_name.split())[:255],
        currency=currency[:8],
    )
