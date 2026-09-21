"""Store catalog crawl request/response schemas.

Field names are snake_case (`total`, `limit`, `offset`). The camelCase in
schemas/search_console.py mirrors Google's own field names and is not a house
convention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from services.backlinks.store_profile import normalize_store_url

StoreCrawlStatus = Literal["pending", "discovering", "enriching", "complete", "partial", "failed"]
StoreCatalogItemKind = Literal["product", "collection"]
StoreCatalogSource = Literal["products_json", "sitemap", "mixed", "none"]
StoreCatalogSort = Literal["gaps", "name", "handle"]


class StoreCrawlCreateRequest(BaseModel):
    store_url: str = Field(min_length=1, max_length=2048, description="Any page on the store")

    @field_validator("store_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_store_url(value)


class StoreCrawlCreatedResponse(BaseModel):
    crawl_id: str
    store_domain: str
    status: StoreCrawlStatus = "pending"
    source: StoreCatalogSource = "none"
    #: True when an identical crawl was already in flight and was handed back.
    reused: bool = False


class StoreCrawlStatusResponse(BaseModel):
    crawl_id: str
    store_url: str
    store_domain: str
    store_name: str | None = None
    status: StoreCrawlStatus
    source: StoreCatalogSource = "none"
    products_total: int = 0
    products_enriched: int = 0
    collections_total: int = 0
    collections_enriched: int = 0
    enrich_failures: int = 0
    enrich_skipped: int = 0
    #: 0-100 across both phases; 100 only in a terminal status.
    progress_pct: int = 0
    items_persisted: int = 0
    revisions_written: int = 0
    #: False when the crawl ran but MongoDB could not be written.
    persistence_ok: bool = True
    shop_suffix: str | None = None
    flag_counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    discovered_at: datetime | None = None
    completed_at: datetime | None = None


class StoreCrawlSummary(BaseModel):
    crawl_id: str
    store_url: str
    store_domain: str
    store_name: str | None = None
    status: StoreCrawlStatus
    source: StoreCatalogSource = "none"
    products_total: int = 0
    collections_total: int = 0
    products_enriched: int = 0
    collections_enriched: int = 0
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class StoreCrawlListResponse(BaseModel):
    crawls: list[StoreCrawlSummary] = Field(default_factory=list)


class StoreCatalogItem(BaseModel):
    kind: StoreCatalogItemKind
    handle: str
    url: str
    name: str = ""
    description_excerpt: str = ""
    meta_title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    price: str | None = None
    price_min: str | None = None
    price_max: str | None = None
    currency: str = ""
    available: bool | None = None
    variants_count: int = 0
    product_type: str = ""
    vendor: str = ""
    tags: list[str] = Field(default_factory=list)
    products_count: int | None = None
    featured_image_url: str = ""
    featured_image_alt: str = ""
    seo_flags: list[str] = Field(default_factory=list)
    seo_gap_count: int = 0
    enriched: bool = False
    enrich_error: str = ""
    http_status: int | None = None
    source: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    enriched_at: datetime | None = None


class StoreCatalogResponse(BaseModel):
    store_domain: str
    kind: StoreCatalogItemKind
    items: list[StoreCatalogItem] = Field(default_factory=list)
    #: Total matching the filter, for paging. NOT len(items).
    total: int = 0
    limit: int = 50
    offset: int = 0
    last_crawl_id: str | None = None
    last_crawl_status: StoreCrawlStatus | None = None
    last_crawled_at: datetime | None = None


class StoreCatalogItemRevision(BaseModel):
    content_hash: str
    crawl_id: str | None = None
    captured_at: datetime | None = None
    name: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    description_excerpt: str | None = None
    price: str | None = None
    available: bool | None = None
    seo_flags: list[str] = Field(default_factory=list)


class StoreCatalogItemDetailResponse(BaseModel):
    item: StoreCatalogItem
    #: Full plain-text description; list reads project it out.
    description: str = ""
    revisions: list[StoreCatalogItemRevision] = Field(default_factory=list)


class StoreFlagCountsResponse(BaseModel):
    store_domain: str
    kind: StoreCatalogItemKind | None = None
    counts: dict[str, int] = Field(default_factory=dict)
