"""Domain models shared by discovery, enrichment and the worker.

These live in the service layer (the precedent is `store_profile.py` defining
`StoreProduct`/`StoreProfile` itself). The API-facing shapes are separate, in
`schemas/stores.py`, so the wire contract can evolve without touching the crawl.

`CatalogItem` and `HeadMeta` are pydantic because they are persisted
(`model_dump(mode="json")` is what MongoDB and the cache receive). Everything else
is a plain dataclass - internal, never serialised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from services.crawler.robots import RobotsRules

CatalogKind = Literal["product", "collection"]
CatalogSource = Literal["products_json", "sitemap", "mixed"]


class CatalogItem(BaseModel):
    """One product or collection. Never carries HTML - see the enrichment docstring."""

    kind: CatalogKind
    handle: str
    url: str
    #: Shopify's numeric id when known (products.json / collections.json only).
    source_id: int | None = None
    #: The user's "product name" - Shopify calls it title.
    name: str = ""
    #: Plain text, tags stripped, capped. Raw body_html is deliberately not kept.
    description: str = ""

    # --- phase 2: read from the page <head> ------------------------------------
    meta_title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    og_title: str = ""
    og_description: str = ""
    http_status: int | None = None
    enriched: bool = False
    enrich_error: str = ""

    # --- products only ----------------------------------------------------------
    #: Decimal STRINGS exactly as Shopify sends them. A float loses trailing
    #: zeros and introduces binary error on money; Decimal is used only to compare.
    price: str | None = None
    price_min: str | None = None
    price_max: str | None = None
    currency: str = ""
    available: bool | None = None
    variants_count: int = 0
    product_type: str = ""
    vendor: str = ""
    tags: list[str] = Field(default_factory=list)

    # --- collections only -------------------------------------------------------
    products_count: int | None = None

    # --- shared -----------------------------------------------------------------
    featured_image_url: str = ""
    #: Shopify sends null more often than not. Normalised to "" so the field is
    #: always a string; the `image_alt_missing` flag carries the meaning.
    featured_image_alt: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    #: Stable flag ids from seo_gaps.SEO_GAP_FLAGS.
    gaps: list[str] = Field(default_factory=list)


class HeadMeta(BaseModel):
    """What one head-only fetch yielded. `error` is set iff the head was unreadable."""

    kind: CatalogKind
    handle: str
    meta_title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    og_title: str = ""
    og_description: str = ""
    http_status: int | None = None
    truncated: bool = False
    skipped_by_robots: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped_by_robots


@dataclass(slots=True)
class OriginInfo:
    """The store's effective origin after following redirects, plus homepage facts."""

    requested_url: str
    origin: str
    final_url: str
    status_code: int
    redirect_chain: list[str] = field(default_factory=list)
    is_shopify: bool = False
    site_name: str = ""
    currency: str = ""


@dataclass(slots=True)
class ProbeResult:
    """Everything the synchronous POST learns before scheduling the job."""

    origin: OriginInfo
    robots: RobotsRules
    products_json_status: int | None
    collections_json_status: int | None
    sitemap_status: int | None

    @property
    def has_products_json(self) -> bool:
        return self.products_json_status == 200

    @property
    def has_collections_json(self) -> bool:
        return self.collections_json_status == 200

    @property
    def has_sitemap(self) -> bool:
        return self.sitemap_status == 200

    @property
    def blocked(self) -> bool:
        """products.json answered but refused us - a WAF/bot-detection signal."""
        return self.products_json_status in (401, 403, 429)

    @property
    def crawlable(self) -> bool:
        return self.has_products_json or self.has_collections_json or self.has_sitemap

    @property
    def tried(self) -> list[str]:
        return ["/products.json", "/collections.json", "/sitemap.xml"]


@dataclass(slots=True)
class DiscoveryResult:
    source: CatalogSource
    items: list[CatalogItem]
    products_total: int
    collections_total: int
    pages_fetched: int
    hit_item_cap: bool = False
    blocked: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EnrichmentStats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_by_robots: int = 0
    cap_hits: int = 0
    retries: int = 0
    aborted_reason: str = ""
