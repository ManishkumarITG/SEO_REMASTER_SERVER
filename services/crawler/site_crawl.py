"""Site-wide crawl: walk one site from a seed URL, breadth-first and bounded.

This is the "future site-wide crawler" that `fetcher.py` was kept reusable for.
It is the third crawl shape in the service layer and deliberately not a variant
of either existing one:

- `fetcher.fetch_page` audits ONE url the user typed. It may build its own
  client and refetch robots.txt, because it does that once.
- `services/stores/` walks a Shopify CATALOG. It knows the URLs up front (from
  products.json or a sitemap) and only reads each <head>, so its frontier never
  grows while it works.
- this module discovers URLs AS IT GOES. The frontier grows from the pages it
  parses, which changes the termination problem entirely: workers must block for
  new work while their siblings are still parsing, and the crawl is finished only
  when the frontier is empty AND no worker is mid-page. That is what
  `asyncio.Queue.join()` expresses, and why this is a queue with `task_done()`
  rather than the fixed worker-pool-plus-sentinels in `enrichment.py`.

Everything else is shared on purpose: the pooled client and capped reads
(`http_client`), robots parsing and wildcard path matching (`robots`), per-host
pacing (`throttle.HostGate`) and <loc> extraction (`sitemap`). One bug fix in
politeness has to land for every crawl at once.

Bounds are not optional. This makes hundreds of unattended requests to a third
party, so every exit is explicit: `max_pages`, `max_depth`, a wall-clock
deadline, and a per-page byte cap. Rate limiting is NOT one of them: a 429 is
answered by waiting and asking again, never by abandoning the crawl.
Reaching one is a normal outcome recorded in `CrawlStats`, never an exception -
a partial crawl with an honest reason is worth far more than a raised error.

On SSRF: the seed host is checked against `is_public_host`, and every URL that
enters the frontier must be same-site, so a document the target controls cannot
aim us at a new host. httpx still follows redirects wherever they point, so the
same-site rule is re-applied to the FINAL url before any of its links are
followed. That is the control `services/stores/discovery.py` settled on and the
reasoning there applies unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from exceptions.crawler import FetchError
from exceptions.stores import RobotsDisallowed
from services.crawler.http_client import (
    fetch_robots,
    get_capped,
    get_text,
    is_public_host,
    origin_of,
    resolve_origin,
)
from services.crawler.models import CrawledPage, CrawlLimits, CrawlStats, SiteCrawlResult
from services.crawler.parser import (
    PageData,
    first_attr,
    first_text,
    link_by_rel,
    meta_by_name,
    parse_html,
    visible_text,
)
from services.crawler.robots import RobotsRules
from services.crawler.sitemap import is_sitemap_url, looks_like_sitemap, parse_sitemap_locs
from services.crawler.throttle import HostGate
from services.crawler.urls import (
    classify_catalog_url,
    extract_links,
    host_of,
    is_probably_asset,
    normalize_url,
    same_site,
)

logger = logging.getLogger("seo.crawler.site")

HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})

#: Per-page read cap. Lower than `fetcher.MAX_BYTES` (5 MiB) because that budget
#: is spent once per audit and this one is spent `max_pages` times: 500 pages x
#: 5 MiB is 2.5 GB of decoded text through selectolax. 2 MiB still covers the
#: 99th percentile of real HTML documents.
MAX_PAGE_BYTES = 2 * 1024 * 1024
RETRY_DELAY_S = 0.5
#: A 429 means "too fast", not "go away". Each one is retried after waiting -
#: honouring Retry-After when the host sends it - because abandoning the URL
#: loses a page we were allowed to have. Only after this many attempts on the
#: SAME page do we accept that we cannot get it, and even then the crawl
#: continues with the rest.
MAX_THROTTLE_RETRIES = 4
#: Ceiling on the pause between requests once a host is throttling us. Far above
#: the catalog crawl 2 s, because waiting 30 s and finishing beats giving up:
#: the deadline is what bounds the crawl, not our patience.
THROTTLED_MAX_INTERVAL_S = 30.0
#: Longest single sleep honoured from a Retry-After header. A host asking for ten
#: minutes gets the deadline instead.
MAX_RETRY_AFTER_S = 60.0
#: Sitemap documents read while seeding, index files included.
MAX_SITEMAP_FETCHES = 10
#: URLs taken from the sitemaps when the crawl has NO page limit. Without this a
#: limit derived from `max_pages` collapses to a token number the moment the page
#: cap is switched off, which is the opposite of what "all pages" asks for. It is
#: still a cap, because a sitemap is a document a third party controls.
MAX_SITEMAP_SEEDS = 25_000
#: Skipped URLs are reported individually up to this many, then only counted. A
#: `Disallow: /search` on a faceted store can match tens of thousands of URLs and
#: the finding is "robots blocks your search pages", not a list of them.
MAX_SKIP_RECORDS = 200
MAX_H1_KEPT = 5
#: Ceiling on the set used to count depth-limited URLs once. Past it the counter
#: may double-count a URL linked from several pages, which is a far better
#: outcome than tracking a set the size of the site.
MAX_DEEP_TRACKED = 50_000

#: Directive separators in a robots meta tag or X-Robots-Tag header. The colon is
#: included so `googlebot: noindex` and `max-snippet:-1` tokenise sanely.
_DIRECTIVE_SPLIT = re.compile(r"[,;:\s]+")

#: Called once per successfully parsed same-site page, while its DOM is still
#: alive. This is the ONLY window in which a page can be analyzed: the markup is
#: dropped the moment the page is reduced to facts, and re-fetching 500 pages to
#: score them afterwards would double the crawl. A hook is passed in rather than
#: imported so this module stays ignorant of `services.analyzers`, which itself
#: imports from `services.crawler.parser`.
PageHook = Callable[[CrawledPage, PageData], Awaitable[None]]


# --- page facts -------------------------------------------------------------------


def robots_directives(dom: HTMLParser, headers: dict[str, str]) -> tuple[str, bool, bool]:
    """-> (raw directives, noindex, nofollow) from meta tags AND X-Robots-Tag.

    The header matters as much as the tag and is invisible in the markup:
    `analyzers/technical.py` already reads it for single-page audits, and a crawl
    that ignored it would report a de-indexed page as indexable. `none` is
    Google's shorthand for both directives at once.
    """
    parts = [value.strip() for name in ("robots", "googlebot")
             if (value := meta_by_name(dom, name)) and value.strip()]
    header = (headers.get("x-robots-tag") or "").strip()
    if header:
        parts.append(header)
    raw = ", ".join(parts)
    tokens = {token for token in _DIRECTIVE_SPLIT.split(raw.lower()) if token}
    return raw, ("noindex" in tokens or "none" in tokens), ("nofollow" in tokens or "none" in tokens)


def extract_page_facts(dom: HTMLParser, headers: dict[str, str] | None = None) -> dict[str, object]:
    """Reduce a parsed page to the facts a report needs, then let the markup go.

    Returns a plain dict rather than a model so `CrawledPage` stays the only
    place that decides which facts are persisted.
    """
    raw_robots, noindex, nofollow = robots_directives(dom, headers or {})
    h1_nodes = dom.css("h1")
    images = dom.css("img")
    #: An absent alt and a whitespace-only alt are the same finding. A genuinely
    #: decorative image uses alt="" - which this counts too, on purpose: at crawl
    #: time we cannot tell decorative from forgotten, so we report and let the
    #: analyzer layer decide.
    missing_alt = sum(1 for node in images if not (node.attributes.get("alt") or "").strip())
    return {
        "title": first_text(dom, "title"),
        "meta_description": " ".join((meta_by_name(dom, "description") or "").split()),
        "canonical_url": (link_by_rel(dom, "canonical") or "").strip(),
        "lang": first_attr(dom, "html[lang]", "lang"),
        "robots_meta": raw_robots,
        "noindex": noindex,
        "nofollow": nofollow,
        "h1": [" ".join((node.text() or "").split()) for node in h1_nodes[:MAX_H1_KEPT]],
        "h1_count": len(h1_nodes),
        "h2_count": len(dom.css("h2")),
        "word_count": len(visible_text(dom).split()),
        "images": len(images),
        "images_missing_alt": missing_alt,
    }


def robots_target(url: str) -> str:
    """The path+query robots.txt rules are matched against.

    The query string is included because real rules use it - Shopify ships
    `Disallow: /*?*oseid=*` - and matching the bare path would silently allow
    every URL those rules exist to exclude.
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


# --- pre-flight -------------------------------------------------------------------


@dataclass(slots=True)
class SiteProbe:
    """What the synchronous pre-flight learns before a crawl is scheduled.

    Two small requests (the homepage <head> and robots.txt) so an unreachable or
    forbidden site answers the POST immediately, instead of becoming a background
    job that fails two seconds later. `store_crawl_service` takes the same
    approach with `probe_store`, for the same reason.
    """

    start_url: str
    origin: str
    host: str
    robots: RobotsRules
    site_name: str = ""
    status_code: int | None = None
    redirect_chain: list[str] = field(default_factory=list)


async def probe_site(client: httpx.AsyncClient, start_url: str) -> SiteProbe:
    """Resolve the seed and read robots.txt once.

    Raises FetchError (not an http(s) URL, not a public host, unreachable) or
    RobotsDisallowed (robots.txt forbids our agent the whole site).

    The redirect is followed HERE rather than inside the crawl because the
    effective origin decides what "same site" means. Seeded at `example.com` on a
    host that redirects to `www.example.com`, every link on every page would be a
    different host and the crawl would stop after one page. Pinning the origin
    first is the same fix `resolve_origin` exists for in the catalog crawl.
    """
    normalized = normalize_url(start_url)
    if normalized is None:
        raise FetchError(f"{start_url!r} is not a crawlable http(s) URL")
    host = host_of(normalized)
    if not await asyncio.to_thread(is_public_host, host):
        raise FetchError(f"{host} is not a public host")

    info = await resolve_origin(client, normalized)
    if info is None:
        raise FetchError(f"Could not reach {normalized}")
    final_host = host_of(info.origin) or host
    if final_host != host and not await asyncio.to_thread(is_public_host, final_host):
        raise FetchError(f"{normalized} redirects to a non-public host")

    robots = await fetch_robots(client, info.origin)
    if robots.blanket_disallow:
        raise RobotsDisallowed(normalized)

    return SiteProbe(
        start_url=normalize_url(info.final_url or normalized) or normalized,
        origin=info.origin,
        host=final_host,
        robots=robots,
        site_name=info.site_name,
        status_code=info.status_code,
        redirect_chain=list(info.redirect_chain),
    )


# --- seeding ----------------------------------------------------------------------


async def sitemap_seed_urls(
    client: httpx.AsyncClient,
    origin: str,
    robots: RobotsRules,
    *,
    host: str,
    include_subdomains: bool = False,
    limit: int = 5_000,
    gate: HostGate | None = None,
) -> tuple[list[str], list[str]]:
    """Same-site page URLs listed in the sitemaps. -> (urls, notes).

    Worth the handful of requests for one reason: a page in the sitemap that no
    crawled page links to is an orphan, and orphans are invisible without a
    declared list of what should exist. Nested indexes are followed one level,
    capped by MAX_SITEMAP_FETCHES.
    """
    candidates: list[str] = [f"{origin}/sitemap.xml", *robots.sitemaps]
    seen_docs: set[str] = set()
    found: list[str] = []
    notes: list[str] = []
    seen_urls: set[str] = set()
    pending = candidates
    fetches = 0

    for depth in (0, 1):
        next_pending: list[str] = []
        for doc_url in pending:
            normalized_doc = normalize_url(doc_url)
            if normalized_doc is None or normalized_doc in seen_docs:
                continue
            if not same_site(normalized_doc, host, include_subdomains=include_subdomains):
                # A sitemap on another host is exactly the amplification vector
                # the discovery layer refuses, for the same reason.
                notes.append(f"ignored off-site sitemap {normalized_doc}")
                continue
            if fetches >= MAX_SITEMAP_FETCHES:
                notes.append(f"stopped after {MAX_SITEMAP_FETCHES} sitemap documents")
                break
            if normalized_doc.lower().endswith(".gz"):
                notes.append(f"skipped gzipped sitemap {normalized_doc}")
                continue
            seen_docs.add(normalized_doc)
            if gate is not None:
                await gate.wait()
            result = await get_text(client, normalized_doc)
            fetches += 1
            if result.error or result.status_code != 200 or not looks_like_sitemap(result.text):
                continue
            if result.truncated:
                notes.append(f"sitemap {normalized_doc} exceeded the byte cap; later entries were not read")
            for loc in parse_sitemap_locs(result.text):
                normalized = normalize_url(loc)
                if normalized is None or normalized in seen_urls:
                    continue
                if not same_site(normalized, host, include_subdomains=include_subdomains):
                    continue
                if is_sitemap_url(normalized):
                    if depth == 0:
                        next_pending.append(normalized)
                    continue
                if is_probably_asset(normalized):
                    continue
                seen_urls.add(normalized)
                found.append(normalized)
                if len(found) >= limit:
                    notes.append(f"sitemap seeding stopped at {limit} URLs")
                    return found, notes
        pending = next_pending
        if not pending:
            break
    return found, notes


# --- the crawl --------------------------------------------------------------------


async def crawl_site(
    client: httpx.AsyncClient,
    start_url: str,
    *,
    robots: RobotsRules | None = None,
    limits: CrawlLimits | None = None,
    gate: HostGate | None = None,
    stats: CrawlStats | None = None,
    extra_seeds: Sequence[str] = (),
    page_hook: PageHook | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AsyncIterator[CrawledPage]:
    """Yield one CrawledPage per URL resolved, in COMPLETION order.

    An async iterator for the reason `enrichment.enrich_head_meta` is one: the
    caller owns batching, progress and persistence, this layer knows nothing
    about them, and memory stays flat on a large site.

    A single page never aborts the crawl - its failure arrives as a CrawledPage
    with `error` or `skipped` set. Only two things end a crawl early, both
    recorded in `stats` and never raised: a bound being reached, and a host
    telling us to stop with repeated 429/503.

    Raises FetchError only for a seed that cannot be crawled at all (not an
    http(s) URL, or not a public host) - a caller error, knowable before any
    request is made.
    """
    limits = limits or CrawlLimits()
    stats = stats if stats is not None else CrawlStats()

    start = normalize_url(start_url)
    if start is None:
        raise FetchError(f"{start_url!r} is not a crawlable http(s) URL")
    host = host_of(start)
    if not await asyncio.to_thread(is_public_host, host):
        raise FetchError(f"{host} is not a public host")
    origin = origin_of(start)
    if robots is None:
        robots = await fetch_robots(client, origin)
    gate = gate or HostGate.for_robots(
        robots, floor_s=limits.min_interval_s, max_interval_s=THROTTLED_MAX_INTERVAL_S
    )

    #: (url, depth, discovered_from, is_target). `is_target` is False for a page
    #: fetched only to reach the ones the caller asked for.
    frontier: asyncio.Queue[tuple[str, int, str, bool]] = asyncio.Queue()
    results: asyncio.Queue[CrawledPage | None] = asyncio.Queue()
    seen: set[str] = set()
    deep_seen: set[str] = set()
    started = time.monotonic()
    reserved = 0
    navigation_reserved = 0
    stopping = False
    skip_records = 0

    def emit(page: CrawledPage) -> None:
        results.put_nowait(page)

    def emit_skip(url: str, depth: int, source: str, reason: str) -> None:
        nonlocal skip_records
        if skip_records >= MAX_SKIP_RECORDS:
            # `note` dedupes, so this is safe to reach thousands of times.
            stats.note(f"more than {MAX_SKIP_RECORDS} skipped URLs were counted but not listed individually")
            return
        skip_records += 1
        # Classified like any other URL: a robots-blocked PRODUCT page is still
        # part of a product-only report (we wanted it and could not have it),
        # while a robots-blocked account page is not. Defaulting these to
        # is_target=True put /account into a products-only answer.
        page_type = classify_catalog_url(url) or ""
        emit(CrawledPage(
            url=url, depth=depth, discovered_from=source, skipped=reason,  # type: ignore[arg-type]
            page_type=page_type, is_target=limits.wants(page_type),
        ))

    def enqueue(url: str, depth: int, source: str) -> bool:
        """Normalise, filter and admit one URL. True when it entered the frontier."""
        normalized = normalize_url(url)
        if normalized is None or normalized in seen:
            return False
        if not same_site(normalized, host, include_subdomains=limits.include_subdomains):
            return False
        if depth > limits.max_depth:
            # NOT added to `seen`: with concurrent workers a URL can surface deep
            # first and shallow second, and banning it here would lose it.
            if len(deep_seen) < MAX_DEEP_TRACKED and normalized not in deep_seen:
                deep_seen.add(normalized)
                stats.beyond_depth += 1
            elif len(deep_seen) >= MAX_DEEP_TRACKED:
                stats.beyond_depth += 1
            return False
        seen.add(normalized)
        if is_probably_asset(normalized):
            stats.skipped_asset += 1
            emit_skip(normalized, depth, source, "asset")
            return False
        if not robots.allows(robots_target(normalized)):
            stats.skipped_by_robots += 1
            emit_skip(normalized, depth, source, "robots")
            return False

        # The page type is read off the URL, so a page outside the filter costs
        # nothing to reject. It is still worth FETCHING when it is the only route
        # to the pages that are inside the filter - a store home page is the map
        # to its product pages - which is what the navigation budget pays for.
        is_target = limits.wants(classify_catalog_url(normalized))
        if not is_target and limits.max_navigation_pages <= 0:
            stats.skipped_page_type += 1
            emit_skip(normalized, depth, source, "page_type")
            return False

        stats.discovered += 1
        stats.max_depth_seen = max(stats.max_depth_seen, depth)
        frontier.put_nowait((normalized, depth, source, is_target))
        stats.frontier_peak = max(stats.frontier_peak, frontier.qsize())
        return True

    # The seed goes through `enqueue` like any other URL: a crawl of a start URL
    # that robots.txt disallows must report that rather than do it.
    seed_admitted = enqueue(start, 0, "")
    if not seed_admitted and not robots.allows(robots_target(start)):
        stats.note("the start URL is disallowed by robots.txt")

    for seed in extra_seeds:
        enqueue(seed, 0, "")

    if limits.seed_from_sitemap:
        seeds, notes = await sitemap_seed_urls(
            client, origin, robots, host=host,
            include_subdomains=limits.include_subdomains,
            # Twice the page budget, so the frontier has slack for the ones that
            # redirect or 404 - or everything the sitemap offers when there is no
            # budget to derive it from.
            limit=(max(limits.max_pages * 2, 100) if limits.max_pages else MAX_SITEMAP_SEEDS),
            gate=gate,
        )
        for note in notes:
            stats.note(note)
        admitted = sum(1 for seed in seeds if enqueue(seed, 0, "sitemap"))
        stats.seeded_from_sitemap = admitted

    async def handle(url: str, depth: int, source: str, is_target: bool) -> None:
        nonlocal reserved, navigation_reserved, stopping

        if stopping:
            stats.not_attempted += 1
            return
        if should_stop is not None and should_stop():
            # The only interruption a person asked for. Everything fetched so far
            # is kept; the crawl simply stops asking for more.
            stopping = True
            stats.stopped_by_user = True
            stats.aborted_reason = stats.aborted_reason or "stopped by user"
            stats.not_attempted += 1
            return
        # `>=`, not `>`: a deadline of 0 must mean "no time at all", and
        # time.monotonic() has ~16 ms granularity on Windows, so a strict
        # comparison lets the first page through before the clock has ticked.
        if time.monotonic() - started >= limits.deadline_s:
            stopping = True
            stats.deadline_hit = True
            stats.aborted_reason = stats.aborted_reason or "deadline"
            stats.not_attempted += 1
            return
        if is_target:
            budget = limits.page_budget
            if budget is not None and reserved >= budget:
                # Reserved BEFORE the await, so `max_pages` is a real ceiling rather
                # than a number `concurrency` workers can each overshoot.
                stopping = True
                stats.hit_page_budget = True
                stats.aborted_reason = stats.aborted_reason or "page budget"
                stats.not_attempted += 1
                return
            reserved += 1
        else:
            # Running out of navigation budget ends the DETOUR, never the crawl:
            # targets already in the frontier, and every URL the sitemap supplied,
            # are still worth fetching.
            if navigation_reserved >= limits.max_navigation_pages:
                stats.hit_navigation_budget = True
                stats.skipped_page_type += 1
                return
            navigation_reserved += 1

        page = await _fetch_page(client, url, depth, source, gate=gate, stats=stats,
                                 limits=limits, host=host, robots=robots,
                                 # A page nobody asked for is not worth scoring.
                                 page_hook=page_hook if is_target else None,
                                 is_target=is_target)
        emit(page)
        if page.fetched:
            if is_target:
                stats.targets += 1
            else:
                stats.navigation_pages += 1

        if page.status_code in (429, 503):
            # Every retry is already spent by now. Record the page as unreachable
            # and carry on: one page we could not have is not a reason to abandon
            # the hundreds we still can.
            stats.gave_up_throttled += 1
            return
        # A page carrying `nofollow` asked us not to walk its links. Its outlinks
        # are still recorded: they are part of the site graph even when we do not
        # follow them, and a report needs to see them.
        if page.nofollow or page.skipped or not page.ok:
            return
        for target in page.outlinks:
            enqueue(target, depth + 1, page.url)

    async def worker() -> None:
        while True:
            url, depth, source, is_target = await frontier.get()
            try:
                await handle(url, depth, source, is_target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one page must never take the crawl down
                logger.warning("crawl %s failed: %s: %s", url, type(exc).__name__, exc)
                stats.transport_errors += 1
                emit(CrawledPage(url=url, depth=depth, discovered_from=source, error=type(exc).__name__))
            finally:
                frontier.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(limits.concurrency)]

    async def closer() -> None:
        await frontier.join()
        results.put_nowait(None)

    closer_task = asyncio.create_task(closer())
    try:
        while True:
            page = await results.get()
            if page is None:
                break
            yield page
    finally:
        for task in workers:
            task.cancel()
        closer_task.cancel()
        stats.elapsed_s = round(time.monotonic() - started, 2)


async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    depth: int,
    source: str,
    *,
    gate: HostGate,
    stats: CrawlStats,
    limits: CrawlLimits,
    host: str,
    robots: RobotsRules | None = None,
    page_hook: PageHook | None = None,
    is_target: bool = True,
) -> CrawledPage:
    """Fetch and reduce one URL. Never raises for anything the network did."""
    page = CrawledPage(
        url=url, depth=depth, discovered_from=source,
        page_type=classify_catalog_url(url) or "", is_target=is_target,
    )
    await gate.wait()
    result = await get_capped(client, url, max_bytes=MAX_PAGE_BYTES, accept=HTML_ACCEPT)

    # One retry, only for transport failures and 5xx. Never 403 (a WAF decision -
    # retrying escalates) and never 404 (deterministic).
    if result.error or (result.status_code is not None and 500 <= result.status_code < 600
                        and result.status_code != 503):
        stats.retries += 1
        await asyncio.sleep(RETRY_DELAY_S)
        await gate.wait()
        result = await get_capped(client, url, max_bytes=MAX_PAGE_BYTES, accept=HTML_ACCEPT)

    # Throttling is a separate case with a separate rule: wait and ask again for
    # the SAME url. The old code recorded a 429 as the page result and moved on,
    # so a rate-limited host cost us the page AND the crawl.
    for attempt in range(MAX_THROTTLE_RETRIES):
        if result.status_code not in (429, 503):
            break
        stats.throttled += 1
        gate.back_off(result.headers.get("retry-after"))
        wait_s = min(_retry_after_seconds(result.headers.get("retry-after")) or gate.interval_s,
                     MAX_RETRY_AFTER_S)
        logger.info("throttled by %s (HTTP %s) - waiting %.1fs before retry %d/%d of %s",
                    host, result.status_code, wait_s, attempt + 1, MAX_THROTTLE_RETRIES, url)
        await asyncio.sleep(wait_s)
        await gate.wait()
        result = await get_capped(client, url, max_bytes=MAX_PAGE_BYTES, accept=HTML_ACCEPT)
        stats.retries += 1
    if result.status_code is not None and 200 <= result.status_code < 400:
        # The host is answering normally again, so stop paying the throttle tax.
        gate.recover()

    page.status_code = result.status_code
    page.final_url = result.final_url or url
    page.redirect_chain = result.redirect_chain
    page.content_type = result.content_type
    page.elapsed_ms = result.elapsed_ms
    page.truncated = result.truncated
    if result.truncated:
        stats.truncated_pages += 1
    if page.redirect_chain:
        stats.redirected += 1

    if result.error:
        page.error = result.error
        stats.transport_errors += 1
        return page

    stats.fetched += 1
    status = result.status_code or 0
    if status >= 500:
        stats.server_errors += 1
        return page
    if status >= 400:
        stats.client_errors += 1
        return page
    if 200 <= status < 300:
        stats.ok += 1
    else:
        return page

    # An empty content-type is common on misconfigured hosts, so absence is
    # treated as "probably HTML" and the parse decides; a declared non-HTML type
    # is believed.
    if result.content_type and result.content_type not in _HTML_CONTENT_TYPES:
        page.skipped = "non_html"
        stats.skipped_non_html += 1
        return page

    dom = parse_html(result.text)
    for key, value in extract_page_facts(dom, result.headers).items():
        setattr(page, key, value)

    if not same_site(page.final_url, host, include_subdomains=limits.include_subdomains):
        # Fetched, but the response landed on another host. Recorded with its
        # status; its links are not ours to follow.
        page.skipped = "off_site"
        stats.skipped_off_site += 1
        return page

    scan = extract_links(
        dom, page.final_url or url, host,
        include_subdomains=limits.include_subdomains,
        follow_nofollow=limits.follow_nofollow,
        max_links=limits.max_links_per_page,
    )
    page.internal_links = len(scan.internal)
    page.internal_nofollow_links = scan.internal_nofollow
    page.external_links = scan.external + scan.external_nofollow
    page.outlinks = scan.internal
    page.outlinks_dropped = scan.dropped

    if page_hook is not None:
        # Last possible moment: after this returns, `dom` and `result.text` go out
        # of scope and the page is only its facts. A hook that raises is a bug in
        # the hook, never a reason to lose the page we just paid to fetch.
        try:
            await page_hook(page, _page_data(result, dom, url, robots))
        except Exception as exc:  # noqa: BLE001
            logger.warning("page hook failed for %s: %s: %s", url, type(exc).__name__, exc)
    return page


def _retry_after_seconds(value: str | None) -> float | None:
    """Seconds from a Retry-After header. Only the numeric form; a date is ignored."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _page_data(result: Any, dom: HTMLParser, requested_url: str, robots: RobotsRules | None) -> PageData:
    """The crawl result as the `PageData` every analyzer already understands.

    Built here rather than in the analyzer layer so the contract stays one-way:
    analyzers know PageData, the crawl knows how to produce one, and neither
    imports the other. `robots_txt` is reconstructed from the parsed rules
    because `technical.py` reads its status and sitemaps off that dict.
    """
    origin = origin_of(result.final_url or requested_url)
    robots_txt: dict[str, Any] = {
        "url": f"{origin}/robots.txt",
        "fetched": bool(robots and robots.fetched),
        "status_code": robots.status_code if robots else None,
        "disallow": list(robots.disallow) if robots else [],
        "allow": list(robots.allow) if robots else [],
        "sitemaps": list(robots.sitemaps) if robots else [],
        "crawl_delay": robots.crawl_delay if robots else None,
    }
    return PageData(
        requested_url=requested_url,
        final_url=result.final_url or requested_url,
        status_code=result.status_code or 0,
        redirect_chain=list(result.redirect_chain),
        headers=dict(result.headers),
        html=result.text,
        dom=dom,
        load_time_ms=result.elapsed_ms,
        fetched_at=datetime.now(UTC),
        robots_txt=robots_txt,
        truncated=result.truncated,
    )


async def collect_site_crawl(
    client: httpx.AsyncClient,
    start_url: str,
    *,
    robots: RobotsRules | None = None,
    limits: CrawlLimits | None = None,
    gate: HostGate | None = None,
    page_hook: PageHook | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SiteCrawlResult:
    """Run a crawl to completion and return it in memory.

    For callers that want the finished graph - orphan detection needs every page
    before it can answer anything. Streaming stays the default for a worker
    persisting as it goes.
    """
    limits = limits or CrawlLimits()
    stats = CrawlStats()
    normalized = normalize_url(start_url)
    result = SiteCrawlResult(
        start_url=normalized or start_url,
        origin=origin_of(normalized or start_url),
        host=host_of(normalized or start_url),
        stats=stats,
    )
    async for page in crawl_site(client, start_url, robots=robots, limits=limits, gate=gate,
                                 stats=stats, page_hook=page_hook, should_stop=should_stop):
        result.pages.append(page)
    return result
