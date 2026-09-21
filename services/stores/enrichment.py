"""Phase 2: read each item's <head> for meta title / meta description.

This is the expensive half. Shopify's JSON has no meta fields, so every product
and collection costs one head-only request (~0.2 s at 8-way). The design is
shaped by three measured facts:

- 64 KiB was enough on 4/4 test pages but </head> arrived at 89 KiB on one
  theme, so the byte cap (MAX_HEAD_BYTES) is the real bound and the sentinel is
  the bonus. When meta is missing AND the read was truncated, retry once bigger.
- A worker queue, not chunked gather: with gather a batch waits for its slowest
  member, so one 10 s timeout idles seven workers. ~45% slower at scale.
- Enrichment is ~99.9% network-bound (selectolax on 90 KiB is ~1 ms against a
  ~1,500 ms wait). Only bytes on the wire, connection reuse and concurrency
  move the number.

Politeness is not optional here - we make hundreds of unattended requests to a
third party. robots.txt is honoured (wildcards and all), Crawl-delay is used as
the floor between requests but clamped, 429/503 back off, and five consecutive
429s abort the pass. Never retain the page HTML on an item: 5,000 x 90 KiB is
450 MB.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from urllib.parse import urlsplit

import httpx

from services.crawler.http_client import MAX_HEAD_BYTES, MAX_HEAD_RETRY_BYTES, get_head_html
from services.crawler.parser import first_text, link_by_rel, meta_by_name, meta_by_property, parse_html
from services.crawler.robots import RobotsRules
from services.stores.models import CatalogItem, EnrichmentStats, HeadMeta

logger = logging.getLogger("seo.stores.enrichment")

ENRICH_CONCURRENCY = 8
#: After a 401/403/429 on products.json the same WAF is about to look at our
#: page requests - halve the pressure.
BLOCKED_HOST_CONCURRENCY = 4
#: ~16 req/s ceiling. At the measured ~6 items/s this never binds normally; it is
#: a safety rail, not a throttle.
PER_HOST_MIN_INTERVAL_S = 0.06
#: A hostile Crawl-delay cannot stall a job for hours.
CRAWL_DELAY_CLAMP_S = 2.0
MAX_HOST_INTERVAL_S = 2.0
RETRY_DELAY_S = 0.5
CONSECUTIVE_429_ABORT = 5
CRAWL_DEADLINE_S = 20 * 60


class HostGate:
    """One per crawl (a crawl is one host). Serialises request STARTS so the rate
    ceiling holds regardless of how many workers are in flight."""

    def __init__(self, interval_s: float = PER_HOST_MIN_INTERVAL_S) -> None:
        self.interval_s = max(interval_s, PER_HOST_MIN_INTERVAL_S)
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    @classmethod
    def for_robots(cls, robots: RobotsRules) -> HostGate:
        delay = robots.crawl_delay or 0.0
        return cls(min(delay, CRAWL_DELAY_CLAMP_S))

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            due = self._last_start + self.interval_s
            if due > now:
                await asyncio.sleep(due - now)
                now = time.monotonic()
            self._last_start = now

    def back_off(self, retry_after: str | None = None) -> None:
        """Quadruple the interval for the rest of the crawl, honouring Retry-After."""
        proposed = self.interval_s * 4
        try:
            if retry_after:
                proposed = max(proposed, float(retry_after))
        except ValueError:
            pass
        self.interval_s = min(max(proposed, PER_HOST_MIN_INTERVAL_S), MAX_HOST_INTERVAL_S)


def extract_head_meta(head_html: str) -> dict[str, str]:
    """The six fields we want from a <head>. Takes a str, returns strs - the tree never escapes."""
    dom = parse_html(head_html)
    return {
        "meta_title": first_text(dom, "title"),
        "meta_description": " ".join((meta_by_name(dom, "description") or "").split()),
        "canonical_url": (link_by_rel(dom, "canonical") or "").strip(),
        "og_title": " ".join((meta_by_property(dom, "og:title") or "").split()),
        "og_description": " ".join((meta_by_property(dom, "og:description") or "").split()),
    }


async def _fetch_one(client: httpx.AsyncClient, item: CatalogItem, gate: HostGate, stats: EnrichmentStats) -> HeadMeta:
    meta = HeadMeta(kind=item.kind, handle=item.handle)
    await gate.wait()
    result = await get_head_html(client, item.url, max_bytes=MAX_HEAD_BYTES)

    # One retry, only for transport failures / 5xx. Never 403 (a WAF decision -
    # retrying escalates) and never 404 (deterministic).
    if result.error or (result.status_code is not None and result.status_code >= 500):
        stats.retries += 1
        await asyncio.sleep(RETRY_DELAY_S)
        await gate.wait()
        result = await get_head_html(client, item.url, max_bytes=MAX_HEAD_BYTES)

    meta.http_status = result.status_code
    if result.error:
        meta.error = result.error
        return meta
    if result.status_code is None or result.status_code >= 400:
        meta.error = f"HTTP {result.status_code}"
        return meta

    fields = extract_head_meta(result.text)
    if result.truncated and not fields["meta_title"] and not fields["meta_description"]:
        # The meta tags were not inside the first cap. Escape hatch: read bigger.
        stats.cap_hits += 1
        await gate.wait()
        bigger = await get_head_html(client, item.url, max_bytes=MAX_HEAD_RETRY_BYTES)
        if not bigger.error and bigger.status_code and bigger.status_code < 400:
            result = bigger
            fields = extract_head_meta(result.text)
    elif result.truncated:
        stats.cap_hits += 1

    meta.truncated = result.truncated
    for key, value in fields.items():
        setattr(meta, key, value)
    return meta


async def enrich_head_meta(
    client: httpx.AsyncClient,
    items: Sequence[CatalogItem],
    *,
    robots: RobotsRules,
    gate: HostGate | None = None,
    concurrency: int = ENRICH_CONCURRENCY,
    deadline_s: float = CRAWL_DEADLINE_S,
    stats: EnrichmentStats | None = None,
) -> AsyncIterator[HeadMeta]:
    """Yield one HeadMeta per item in COMPLETION order, at most `concurrency` in flight.

    An async iterator rather than a list so the caller owns batching, progress
    flushing and persistence while this layer knows nothing about them - and so
    memory stays flat. A single item's failure never raises; it yields a
    HeadMeta with `error` set. Items disallowed by robots yield immediately with
    `skipped_by_robots`.
    """
    stats = stats if stats is not None else EnrichmentStats()
    gate = gate or HostGate.for_robots(robots)
    queue: asyncio.Queue[CatalogItem | None] = asyncio.Queue()
    results: asyncio.Queue[HeadMeta | None] = asyncio.Queue()
    started = time.monotonic()
    consecutive_429 = 0
    aborted = False

    allowed: list[CatalogItem] = []
    for item in items:
        if robots.allows(urlsplit(item.url).path):
            allowed.append(item)
        else:
            stats.skipped_by_robots += 1
            stats.attempted += 1
            await results.put(HeadMeta(kind=item.kind, handle=item.handle, skipped_by_robots=True,
                                       error="skipped_by_robots"))
    for item in allowed:
        queue.put_nowait(item)
    workers_n = max(1, min(concurrency, len(allowed))) if allowed else 0
    for _ in range(workers_n):
        queue.put_nowait(None)

    async def worker() -> None:
        nonlocal consecutive_429, aborted
        while True:
            item = await queue.get()
            if item is None:
                return
            if aborted or time.monotonic() - started > deadline_s:
                if not aborted:
                    aborted = True
                    stats.aborted_reason = stats.aborted_reason or "deadline"
                # Drain: report the item as not attempted.
                await results.put(HeadMeta(kind=item.kind, handle=item.handle, error="not_attempted"))
                continue
            try:
                meta = await _fetch_one(client, item, gate, stats)
            except Exception as exc:  # noqa: BLE001 - one page must never take the crawl down
                logger.warning("enrich %s failed: %s: %s", item.url, type(exc).__name__, exc)
                meta = HeadMeta(kind=item.kind, handle=item.handle, error=f"{type(exc).__name__}")
            stats.attempted += 1
            if meta.http_status in (429, 503):
                consecutive_429 += 1
                gate.back_off()
                if consecutive_429 >= CONSECUTIVE_429_ABORT and not aborted:
                    aborted = True
                    stats.aborted_reason = f"{CONSECUTIVE_429_ABORT} consecutive {meta.http_status}s"
                    logger.warning("enrichment aborted: %s", stats.aborted_reason)
            elif meta.ok:
                consecutive_429 = 0
            if meta.ok:
                stats.succeeded += 1
            else:
                stats.failed += 1
            await results.put(meta)

    tasks = [asyncio.create_task(worker()) for _ in range(workers_n)]

    async def closer() -> None:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await results.put(None)

    closer_task = asyncio.create_task(closer())
    try:
        while True:
            meta = await results.get()
            if meta is None:
                break
            yield meta
    finally:
        for task in tasks:
            task.cancel()
        closer_task.cancel()
