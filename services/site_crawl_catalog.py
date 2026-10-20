"""Catalog-first phase of a site crawl: read a Shopify store's whole catalog as JSON.

A store with 10,000 products is 10,000 page fetches for the HTML crawl, and on a
store that tolerates ~1 request a second that is three hours. The same store's
public `/products.json` returns 250 products a request, so the entire catalog -
every product and collection, its description, its images, its last-modified
time - is ~40 requests, under a minute. That is the fastest honest answer this
tool can give about a large store, so it comes first.

What JSON can and cannot say
----------------------------
JSON carries the catalog's CONTENT: descriptions, images and their alt text,
whether a collection is empty, whether two products share a name. It does not
carry what Shopify renders into `<head>` - the meta title, meta description and
canonical - so those still come from the page crawl, which runs straight after
and fills them in page by page. The two are complementary, not alternatives.

The gap definitions are the store crawl's own (`services.stores.seo_gaps.item_gaps`),
so the two features can never disagree about what "image alt missing" means. The
one check added here, `duplicate_name`, is the JSON-level counterpart of the store
crawl's duplicate meta title check, which needs the rendered `<head>`.

Every request goes through the page crawl's own rate limiter (see `limiter_for`),
so the store sees one paced client rather than two.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.crawler.site_crawl import SiteProbe
from services.crawler.throttle import LocalRateLimiter
from services.stores.discovery import fetch_catalog_json
from services.stores.models import CatalogItem
from services.stores.seo_gaps import item_gaps

logger = logging.getLogger("seo.site_crawl.catalog")

#: Gaps that only mean "we did not fetch the page", which is the page crawl's
#: job here, not a finding about the catalog.
_ENRICHMENT_ONLY = frozenset({"not_enriched", "page_unreachable", "skipped_by_robots"})
#: Stored per item so a list read stays small; the full text is not needed.
EXCERPT_CHARS = 200


@dataclass(slots=True)
class SiteCatalog:
    """The catalog phase's result: findings, records to store, URLs to crawl."""

    products: int = 0
    collections: int = 0
    #: Both endpoints were read to their natural end.
    complete: bool = False
    requests: int = 0
    elapsed_s: float = 0.0
    blocked: bool = False
    gap_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Product and collection URLs, for the page crawl's frontier.
    urls: list[str] = field(default_factory=list)
    #: One document per item, for `db.site_catalog`.
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.products == 0 and self.collections == 0:
            return "unavailable"
        return "complete" if self.complete else "partial"

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "products": self.products,
            "collections": self.collections,
            "complete": self.complete,
            "requests": self.requests,
            "elapsed_s": round(self.elapsed_s, 1),
            "gap_counts": dict(self.gap_counts),
        }


def _collapse(text: str) -> str:
    return " ".join(text.lower().split())


def catalog_findings(items: list[CatalogItem]) -> dict[str, list[str]]:
    """url -> gaps, from what JSON can tell. O(n): one Counter for duplicates."""
    name_counts = Counter(_collapse(i.name) for i in items if i.kind == "product" and i.name.strip())
    findings: dict[str, list[str]] = {}
    for item in items:
        gaps = [g for g in item_gaps(item) if g not in _ENRICHMENT_ONLY]
        if item.kind == "product" and item.name.strip() and name_counts[_collapse(item.name)] > 1:
            # Two products a shopper cannot tell apart in search results either.
            gaps.append("duplicate_name")
        findings[item.url] = gaps
    return findings


def catalog_record(item: CatalogItem, gaps: list[str], *, crawl_id: str, host: str) -> dict[str, Any]:
    description = item.description or ""
    return {
        "crawl_id": crawl_id,
        "host": host,
        "kind": item.kind,
        "handle": item.handle,
        "url": item.url,
        "name": item.name,
        "product_type": item.product_type,
        "vendor": item.vendor,
        "description_excerpt": description[:EXCERPT_CHARS],
        "description_words": len(description.split()),
        "featured_image_url": item.featured_image_url,
        "featured_image_alt": item.featured_image_alt,
        "products_count": item.products_count,
        "variants_count": item.variants_count,
        "price": item.price,
        "currency": item.currency,
        "available": item.available,
        # The store's own last-modified time: what an incremental re-crawl will
        # compare against to decide whether a page needs fetching again.
        "updated_at": item.updated_at,
        "published_at": item.published_at,
        "gaps": gaps,
        "gap_count": len(gaps),
    }


async def collect_site_catalog(
    client: httpx.AsyncClient,
    probe: SiteProbe,
    *,
    crawl_id: str,
    limiter: LocalRateLimiter,
    should_stop: Callable[[], bool] | None = None,
    max_products: int = 50_000,
    max_collections: int = 5_000,
) -> SiteCatalog:
    """Read the whole catalog and turn it into findings, records and seed URLs."""
    started = time.monotonic()
    raw = await fetch_catalog_json(
        client, probe.origin, probe.robots,
        pacer=limiter, should_stop=should_stop,
        max_products=max_products, max_collections=max_collections, currency=probe.currency,
    )
    items = [*raw.collections, *raw.products]
    findings = catalog_findings(items)

    out = SiteCatalog(
        products=len(raw.products),
        collections=len(raw.collections),
        complete=raw.products_complete and raw.collections_complete,
        requests=raw.requests,
        elapsed_s=time.monotonic() - started,
        blocked=raw.blocked,
        notes=list(raw.notes),
    )
    counts: Counter[str] = Counter()
    for item in items:
        gaps = findings.get(item.url, [])
        counts.update(gaps)
        out.records.append(catalog_record(item, gaps, crawl_id=crawl_id, host=probe.host))
        out.urls.append(item.url)
    out.gap_counts = dict(counts.most_common())
    logger.info(
        "Site crawl %s: catalog %s - %d products, %d collections in %.1fs (%d requests)",
        crawl_id, out.status, out.products, out.collections, out.elapsed_s, out.requests,
    )
    return out
