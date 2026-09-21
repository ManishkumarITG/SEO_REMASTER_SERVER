"""Find every product and collection URL a store has. Tier 1 JSON, tier 2 sitemap.

Tier 1 - Shopify's public `/products.json` and `/collections.json`, paginated at
250 per page. Gives name, description, price, image in ~1 s for a whole catalog.
Measured: 294 products in 2 requests, but page 1 alone was 1.6 MB.

Tier 2 - `/sitemap.xml`. Yields URLs only, so name and description must come
from the page in phase 2. Mandatory, not optional: gymshark.com answers 403 for
/products.json and serves 2,201 product URLs from its sitemap.

The two loops run and fall back INDEPENDENTLY - some stores 404 /collections.json
while still serving every /collections/<handle> page - so `source="mixed"` is a
legitimate outcome.

Sitemap XML is parsed with a bounded regex over <loc>, on purpose. The XML comes
from a user-supplied host. XXE is not reachable on CPython 3.12, but
entity-expansion DoS is - expat expands inline DOCTYPE entities with no limit and
the byte cap bounds input, not the blowup. We need one thing: the text of <loc>.
This is token extraction from an untrusted document, not parsing.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from exceptions.crawler import FetchError
from exceptions.stores import CatalogUnavailable, RobotsDisallowed
from services.crawler.fetcher import MAX_ROBOTS_BYTES
from services.crawler.http_client import (
    get_json,
    get_text,
    is_public_host,
    resolve_origin,
)
from services.crawler.parser import text_from_html_fragment
from services.crawler.robots import RobotsRules, parse_robots
from services.stores.models import CatalogItem, DiscoveryResult, OriginInfo, ProbeResult

logger = logging.getLogger("seo.stores.discovery")

SHOPIFY_PAGE_LIMIT = 250
#: Restart limit after a page exceeded the byte cap.
FALLBACK_PAGE_LIMIT = 50
MAX_PRODUCT_PAGES = 40  # 40 x 250 = 10,000
MAX_COLLECTION_PAGES = 8
MAX_PRODUCTS = 10_000
MAX_COLLECTIONS = 2_000
MAX_SUB_SITEMAPS = 20
MAX_SITEMAP_LOCS = 50_000
DESCRIPTION_CHARS = 2_000
#: Only status matters for the probe; read a sliver.
PROBE_BYTES = 16 * 1024

_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
#: Linear, no nested quantifiers - cannot backtrack catastrophically.
_LOC_RE = re.compile(r"<loc\b[^>]*>(.*?)</loc>", re.IGNORECASE | re.DOTALL)
#: A path segment like "es-US" or "fr" before "sitemap_" marks a locale duplicate.
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")


# --- robots -------------------------------------------------------------------------


async def fetch_robots(client: httpx.AsyncClient, origin: str) -> RobotsRules:
    """Fetch and parse robots.txt ONCE per crawl. Unreadable -> permissive."""
    result = await get_text(client, f"{origin}/robots.txt", max_bytes=MAX_ROBOTS_BYTES)
    if result.error or result.status_code != 200:
        rules = RobotsRules.permissive()
        rules.fetched = not result.error
        rules.status_code = result.status_code
        return rules
    info: dict[str, Any] = {"disallow": [], "allow": [], "sitemaps": [], "crawl_delay": None,
                            "fetched": True, "status_code": 200}
    parse_robots(result.text, info)
    return RobotsRules.from_info(info)


# --- probe (synchronous, in the POST) ----------------------------------------------


async def probe_store(client: httpx.AsyncClient, normalized_url: str) -> ProbeResult:
    """2-4 small requests: is this a store we may and can crawl?

    Raises FetchError (unreachable / not public), RobotsDisallowed (blanket
    Disallow for us) or CatalogUnavailable (reachable, but none of
    /products.json, /collections.json, /sitemap.xml exist). The POST maps those
    to 422 / 403 / 422 synchronously, so `example.com` never gets a job.
    """
    host = urlsplit(normalized_url).hostname or ""
    if not await asyncio.to_thread(is_public_host, host):
        raise FetchError(f"{host} is not a public host")

    origin_info = await resolve_origin(client, normalized_url)
    if origin_info is None:
        raise FetchError(f"Could not reach {normalized_url}")
    final_host = urlsplit(origin_info.origin).hostname or ""
    if final_host != host and not await asyncio.to_thread(is_public_host, final_host):
        raise FetchError(f"{normalized_url} redirects to a non-public host")

    robots = await fetch_robots(client, origin_info.origin)
    if robots.blanket_disallow:
        raise RobotsDisallowed(normalized_url)

    products, collections, sitemap = await asyncio.gather(
        get_json(client, f"{origin_info.origin}/products.json", params={"limit": 1}, max_bytes=PROBE_BYTES * 8),
        get_json(client, f"{origin_info.origin}/collections.json", params={"limit": 1}, max_bytes=PROBE_BYTES * 8),
        get_text(client, f"{origin_info.origin}/sitemap.xml", max_bytes=PROBE_BYTES),
    )
    probe = ProbeResult(
        origin=origin_info,
        robots=robots,
        products_json_status=products.status_code if products.ok else _non_ok_status(products.status_code),
        collections_json_status=collections.status_code if collections.ok else _non_ok_status(collections.status_code),
        sitemap_status=sitemap.status_code if (sitemap.ok and "<loc" in sitemap.text.lower()) else None,
    )
    if not probe.crawlable and not robots.sitemaps:
        raise CatalogUnavailable(normalized_url, probe.tried)
    return probe


def _non_ok_status(status: int | None) -> int | None:
    """200 with an unusable body counts as 'no JSON here', not as success."""
    return status if status not in (None, 200) else None


# --- tier 1: Shopify JSON -------------------------------------------------------------


async def _paginate(
    client: httpx.AsyncClient,
    origin: str,
    path: str,
    key: str,
    *,
    limit: int,
    max_items: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int, list[str], bool, bool]:
    """Walk ?limit=&page= until a short/empty page. -> (rows, pages, notes, hit_cap, truncated)."""
    rows: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    notes: list[str] = []
    previous_first: Any = object()
    pages = 0
    for page in range(1, max_pages + 1):
        result = await get_json(client, f"{origin}{path}", params={"limit": limit, "page": page})
        if result.truncated:
            return rows, pages, notes, False, True
        if not result.ok or not isinstance(result.payload, dict):
            if pages == 0 and result.status_code not in (None, 200):
                notes.append(f"{path} answered {result.status_code}")
            break
        batch = result.payload.get(key)
        if not isinstance(batch, list) or not batch:
            break
        pages += 1
        first_id = batch[0].get("id") if isinstance(batch[0], dict) else None
        if first_id is not None and first_id == previous_first:
            # A proxy is stripping ?page= - without this guard we would ingest
            # page 1 forty times.
            notes.append(f"{path} ignores the page parameter; stopped after page {page - 1}")
            break
        previous_first = first_id
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            item_id = raw.get("id")
            if item_id is not None:
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
            rows.append(raw)
            if len(rows) >= max_items:
                return rows, pages, notes, True, False
        if len(batch) < limit:
            break
    return rows, pages, notes, False, False


def _handle_ok(handle: Any) -> bool:
    return isinstance(handle, str) and bool(_HANDLE_RE.match(handle))


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _featured_image(images: Any) -> tuple[str, str]:
    """(src, alt) of the lowest-position image. Alt is often null -> ""."""
    if isinstance(images, dict):  # collections.json: a single object or null
        return str(images.get("src") or ""), str(images.get("alt") or "")
    if not isinstance(images, list) or not images:
        return "", ""
    dicts = [img for img in images if isinstance(img, dict)]
    if not dicts:
        return "", ""
    first = min(dicts, key=lambda img: img.get("position") if isinstance(img.get("position"), int) else 1 << 30)
    return str(first.get("src") or ""), str(first.get("alt") or "")


def _parse_dt(value: Any):
    from datetime import datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def product_from_json(origin: str, raw: dict[str, Any], *, currency: str = "") -> CatalogItem | None:
    handle = raw.get("handle")
    if not _handle_ok(handle):
        return None
    variants = [v for v in (raw.get("variants") or []) if isinstance(v, dict)]
    prices = [d for d in (_decimal(v.get("price")) for v in variants) if d is not None]
    tags = raw.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    image_url, image_alt = _featured_image(raw.get("images"))
    return CatalogItem(
        kind="product",
        handle=handle,
        url=f"{origin}/products/{handle}",
        source_id=raw.get("id") if isinstance(raw.get("id"), int) else None,
        name=str(raw.get("title") or "").strip(),
        description=text_from_html_fragment(str(raw.get("body_html") or ""), DESCRIPTION_CHARS),
        price=str(variants[0].get("price")) if variants and variants[0].get("price") is not None else None,
        price_min=str(min(prices)) if prices else None,
        price_max=str(max(prices)) if prices else None,
        currency=currency,
        available=any(bool(v.get("available")) for v in variants) if variants else None,
        variants_count=len(variants),
        product_type=str(raw.get("product_type") or "").strip(),
        vendor=str(raw.get("vendor") or "").strip(),
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        featured_image_url=image_url,
        featured_image_alt=image_alt,
        published_at=_parse_dt(raw.get("published_at")),
        updated_at=_parse_dt(raw.get("updated_at")),
    )


def collection_from_json(origin: str, raw: dict[str, Any]) -> CatalogItem | None:
    handle = raw.get("handle")
    if not _handle_ok(handle):
        return None
    image_url, image_alt = _featured_image(raw.get("image"))
    count = raw.get("products_count")
    return CatalogItem(
        kind="collection",
        handle=handle,
        url=f"{origin}/collections/{handle}",
        source_id=raw.get("id") if isinstance(raw.get("id"), int) else None,
        name=str(raw.get("title") or "").strip(),
        # NB: collections.json calls it `description` (already HTML), not body_html.
        description=text_from_html_fragment(str(raw.get("description") or ""), DESCRIPTION_CHARS),
        products_count=count if isinstance(count, int) else None,
        featured_image_url=image_url,
        featured_image_alt=image_alt,
        published_at=_parse_dt(raw.get("published_at")),
        updated_at=_parse_dt(raw.get("updated_at")),
    )


async def _discover_json(
    client: httpx.AsyncClient,
    origin: str,
    path: str,
    key: str,
    *,
    max_items: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]] | None, int, list[str], bool]:
    """Paginate; on a truncated page restart once at a smaller limit. None = unusable."""
    rows, pages, notes, hit_cap, truncated = await _paginate(
        client, origin, path, key, limit=SHOPIFY_PAGE_LIMIT, max_items=max_items, max_pages=max_pages
    )
    if truncated:
        notes.append(f"{path} page exceeded the byte cap at limit={SHOPIFY_PAGE_LIMIT}; retrying at {FALLBACK_PAGE_LIMIT}")
        rows, pages2, notes2, hit_cap, truncated = await _paginate(
            client, origin, path, key, limit=FALLBACK_PAGE_LIMIT, max_items=max_items,
            max_pages=max_pages * (SHOPIFY_PAGE_LIMIT // FALLBACK_PAGE_LIMIT),
        )
        pages += pages2
        notes += notes2
        if truncated:
            notes.append(f"{path} still exceeded the byte cap at limit={FALLBACK_PAGE_LIMIT}; using the sitemap")
            return None, pages, notes, False
    if pages == 0:
        return None, 0, notes, False
    return rows, pages, notes, hit_cap


# --- tier 2: sitemap ---------------------------------------------------------------------


def parse_sitemap_locs(xml_text: str, *, limit: int = MAX_SITEMAP_LOCS) -> list[str]:
    """Every <loc> value, unescaped, in document order, capped. Never parses XML."""
    locs: list[str] = []
    for match in _LOC_RE.finditer(xml_text):
        value = html.unescape(match.group(1)).strip()
        if value:
            locs.append(value)
            if len(locs) >= limit:
                break
    return locs


def _same_host(origin: str, url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == (urlsplit(origin).hostname or "").lower()


def _is_locale_sitemap(url: str) -> bool:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return len(segments) >= 2 and bool(_LOCALE_SEGMENT_RE.match(segments[0])) and "sitemap" in segments[-1].lower()


def classify_catalog_url(url: str) -> str | None:
    """"product" | "collection" | None from the path shape alone (works on non-Shopify too)."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) == 2 and segments[0] == "products" and _handle_ok(segments[1]):
        return "product"
    if len(segments) == 2 and segments[0] == "collections" and _handle_ok(segments[1]):
        if segments[1].lower() in ("all", "frontpage"):
            return None  # Shopify's virtual collections, not real ones
        return "collection"
    return None


def item_from_url(kind: str, url: str) -> CatalogItem:
    parts = urlsplit(url)
    handle = [s for s in parts.path.split("/") if s][-1]
    return CatalogItem(kind=kind, handle=handle, url=f"{parts.scheme}://{parts.netloc}{parts.path}")  # type: ignore[arg-type]


async def _discover_sitemap(
    client: httpx.AsyncClient,
    origin: str,
    robots: RobotsRules,
    *,
    want_products: bool,
    want_collections: bool,
    max_products: int,
    max_collections: int,
) -> tuple[list[CatalogItem], list[CatalogItem], list[str], int]:
    notes: list[str] = []
    # Shopify's direct sub-sitemap paths, tried last, and only for what is wanted:
    # probing sitemap_products_1.xml on a store we only need collections from is a
    # wasted request the store's WAF still counts.
    candidates = [f"{origin}/sitemap.xml", *robots.sitemaps]
    if want_products:
        candidates.append(f"{origin}/sitemap_products_1.xml")
    if want_collections:
        candidates.append(f"{origin}/sitemap_collections_1.xml")
    seen_sitemaps: set[str] = set()
    products: dict[str, CatalogItem] = {}
    collections: dict[str, CatalogItem] = {}
    fetched = 0

    async def ingest(url: str, depth: int) -> None:
        nonlocal fetched
        if url in seen_sitemaps or fetched >= MAX_SUB_SITEMAPS or not _same_host(origin, url):
            return
        if url.lower().endswith(".gz"):
            notes.append(f"skipped gzipped sitemap {url}")
            return
        seen_sitemaps.add(url)
        result = await get_text(client, url)
        fetched += 1
        if result.error or result.status_code != 200:
            return
        if result.truncated:
            notes.append(f"sitemap {url} exceeded the byte cap; entries beyond it were not read")
        locs = parse_sitemap_locs(result.text)
        subs = [loc for loc in locs if loc.lower().endswith((".xml", ".xml.gz")) or "sitemap" in loc.lower()]
        leaves = [loc for loc in locs if loc not in subs]
        for loc in leaves:
            if not _same_host(origin, loc):
                continue
            kind = classify_catalog_url(loc)
            if kind == "product" and want_products and len(products) < max_products:
                item = item_from_url("product", loc)
                products.setdefault(item.handle, item)
            elif kind == "collection" and want_collections and len(collections) < max_collections:
                item = item_from_url("collection", loc)
                collections.setdefault(item.handle, item)
        if depth == 0:
            # Products first, then collections; skip pages/blogs and locale copies.
            ordered = sorted(
                (s for s in subs if not _is_locale_sitemap(s)),
                key=lambda s: (0 if "product" in s.lower() else 1 if "collection" in s.lower() else 2),
            )
            for sub in ordered:
                low = sub.lower()
                if "page" in low or "blog" in low or "hreflang" in low:
                    continue
                if ("product" in low and not want_products) or ("collection" in low and not want_collections):
                    continue
                await ingest(sub, depth + 1)

    for candidate in candidates:
        if products or collections:
            break
        await ingest(candidate, 0)
    return list(products.values()), list(collections.values()), notes, fetched


# --- entry point --------------------------------------------------------------------------


async def discover_catalog(
    client: httpx.AsyncClient,
    probe: ProbeResult,
    *,
    max_products: int = MAX_PRODUCTS,
    max_collections: int = MAX_COLLECTIONS,
) -> DiscoveryResult:
    """Full discovery. Raises CatalogUnavailable if, after both tiers, there is nothing."""
    origin: OriginInfo = probe.origin
    notes: list[str] = []
    pages = 0
    hit_cap = False
    products: list[CatalogItem] = []
    collections: list[CatalogItem] = []
    products_from_json = collections_from_json = False

    if probe.has_products_json:
        rows, p, n, cap = await _discover_json(
            client, origin.origin, "/products.json", "products",
            max_items=max_products, max_pages=MAX_PRODUCT_PAGES,
        )
        pages += p
        notes += n
        if rows is not None:
            products = [i for i in (product_from_json(origin.origin, r, currency=origin.currency) for r in rows) if i]
            products_from_json = True
            hit_cap = hit_cap or cap

    if probe.has_collections_json:
        rows, p, n, cap = await _discover_json(
            client, origin.origin, "/collections.json", "collections",
            max_items=max_collections, max_pages=MAX_COLLECTION_PAGES,
        )
        pages += p
        notes += n
        if rows is not None:
            collections = [i for i in (collection_from_json(origin.origin, r) for r in rows) if i]
            collections_from_json = True
            hit_cap = hit_cap or cap

    if not products_from_json or not collections_from_json:
        sp, sc, n, fetched = await _discover_sitemap(
            client, origin.origin, probe.robots,
            want_products=not products_from_json,
            want_collections=not collections_from_json,
            max_products=max_products, max_collections=max_collections,
        )
        pages += fetched
        notes += n
        if not products_from_json:
            products = sp
        if not collections_from_json:
            collections = sc

    if not products and not collections:
        raise CatalogUnavailable(origin.requested_url, probe.tried)

    if products_from_json and (collections_from_json or not collections):
        source = "products_json"
    elif not products_from_json and not collections_from_json:
        source = "sitemap"
    else:
        source = "mixed"

    if probe.blocked:
        notes.append(f"/products.json answered {probe.products_json_status} - treating the host as bot-sensitive")

    return DiscoveryResult(
        source=source,  # type: ignore[arg-type]
        items=[*collections, *products],
        products_total=len(products),
        collections_total=len(collections),
        pages_fetched=pages,
        hit_item_cap=hit_cap,
        blocked=probe.blocked,
        notes=notes,
    )
