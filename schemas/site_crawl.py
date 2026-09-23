"""Site-wide crawl request/response schemas.

snake_case throughout, matching schemas/stores.py.

The request carries optional bound overrides. They are capped HERE rather than
only in the service, because these are the numbers that decide how many requests
we make to somebody else's server: a validator that rejects
`max_pages=1000000` at the edge is worth more than one that clamps it silently
three layers in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from services.crawler.urls import normalize_seed_url

#: "stopped" is its own outcome: a person pressed Stop, which is not a failure
#: and not a limitation to work around.
SiteCrawlStatus = Literal["pending", "crawling", "complete", "partial", "failed", "stopped"]
#: Catalog page types a crawl can be restricted to. An empty list means every
#: page, which is the default.
SiteCrawlPageType = Literal["product", "collection"]
SiteCrawlPageSort = Literal["depth", "url", "status", "words", "links", "score"]
SiteCrawlIssue = Literal[
    "broken", "redirected", "noindex", "missing_title", "missing_description",
    "missing_canonical", "multiple_h1", "no_h1", "thin", "images_missing_alt",
    "blocked_by_robots", "failed", "orphan", "critical", "warning",
    "product", "collection",
]

#: Hard ceilings on what one API caller may ask for, independent of the
#: configured defaults. Above these a crawl stops being a diagnostic and starts
#: being a load test against a third party.
MAX_REQUESTABLE_PAGES = 5_000
MAX_REQUESTABLE_DEPTH = 10


class SiteCrawlCreateRequest(BaseModel):
    start_url: str = Field(min_length=1, max_length=2048, description="Any page on the site")
    #: None means "use the configured default". ZERO means no page limit at all,
    #: in which case the time limit and the depth limit are the only bounds left -
    #: which is why they are not optional and cannot be switched off from here.
    max_pages: int | None = Field(default=None, ge=0, le=MAX_REQUESTABLE_PAGES)
    max_depth: int | None = Field(default=None, ge=0, le=MAX_REQUESTABLE_DEPTH)
    include_subdomains: bool | None = None
    #: Following rel="nofollow" ignores an explicit instruction from the site, so
    #: it is opt-in per request and never a default.
    follow_nofollow: bool | None = None
    seed_from_sitemap: bool | None = None
    #: Restrict the crawl to product pages, collection pages, or both. Omitted or
    #: empty crawls every page. Duplicates are harmless; order is not significant.
    page_types: list[SiteCrawlPageType] = Field(default_factory=list)
    #: How many non-matching pages may be fetched purely to reach the matching
    #: ones. Only meaningful alongside `page_types`.
    max_navigation_pages: int | None = Field(default=None, ge=0, le=MAX_REQUESTABLE_PAGES)

    @field_validator("start_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        normalized = normalize_seed_url(value)
        if normalized is None:
            raise ValueError("start_url must be an http(s) URL with a host")
        return normalized


class SiteCrawlCreatedResponse(BaseModel):
    crawl_id: str
    start_url: str
    host: str
    status: SiteCrawlStatus = "pending"
    #: True when a crawl of this host was already in flight and was handed back
    #: instead of starting a second one.
    reused: bool = False


class SiteCrawlStatusResponse(BaseModel):
    crawl_id: str
    start_url: str
    host: str
    site_name: str | None = None
    status: SiteCrawlStatus
    max_pages: int = 0
    max_depth: int = 0

    pages_discovered: int = 0
    pages_fetched: int = 0
    pages_ok: int = 0
    client_errors: int = 0
    server_errors: int = 0
    transport_errors: int = 0
    skipped_by_robots: int = 0
    skipped_asset: int = 0
    skipped_non_html: int = 0
    skipped_off_site: int = 0
    beyond_depth: int = 0
    not_attempted: int = 0
    seeded_from_sitemap: int = 0
    max_depth_seen: int = 0

    #: 0-100. Only ever 100 in a terminal status. A crawl discovers its own work,
    #: so this is a fraction of the page BUDGET, not of a known total.
    progress_pct: int = 0
    hit_page_budget: bool = False
    deadline_hit: bool = False
    aborted_reason: str | None = None

    orphan_count: int = 0
    noindex_count: int = 0
    missing_title_count: int = 0
    missing_description_count: int = 0
    broken_page_count: int = 0

    #: Mean analyzer score across the pages that were scored. None means page
    #: scoring was off for this crawl.
    pages_analyzed: int = 0
    avg_score: float | None = None
    grade: str | None = None
    critical_findings: int = 0
    warning_findings: int = 0

    #: The filter this crawl ran under, and what it cost. `target_pages` is the
    #: answer; `navigation_pages` is what had to be fetched to find it.
    page_types: list[str] = Field(default_factory=list)
    target_pages: int = 0
    navigation_pages: int = 0
    skipped_page_type: int = 0

    #: Responses asking us to slow down, and pages finally abandoned to them.
    #: A crawl is never aborted over throttling; these say what it cost.
    throttled: int = 0
    gave_up_throttled: int = 0

    pages_persisted: int = 0
    persistence_ok: bool = True
    notes: list[str] = Field(default_factory=list)
    elapsed_s: float = 0.0
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SiteCrawlSummary(BaseModel):
    crawl_id: str
    start_url: str
    host: str
    site_name: str | None = None
    status: SiteCrawlStatus
    pages_fetched: int = 0
    pages_ok: int = 0
    orphan_count: int = 0
    broken_page_count: int = 0
    avg_score: float | None = None
    grade: str | None = None
    #: Carried on the summary too, so a history row can show the size of the
    #: ANSWER rather than the raw request count: a products-only crawl that
    #: returned 40 product pages fetched 148 URLs to find them, and "148 pages"
    #: next to it is a number nobody asked for.
    page_types: list[str] = Field(default_factory=list)
    target_pages: int = 0
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class SiteCrawlListResponse(BaseModel):
    crawls: list[SiteCrawlSummary] = Field(default_factory=list)
    total: int = 0


class SiteCrawlPage(BaseModel):
    """One crawled URL as the list endpoint returns it.

    `outlinks` is deliberately absent: it is stored (it is the site graph) but
    500 pages x 300 links is not a list response. The detail endpoint has it.
    """

    url: str
    depth: int = 0
    discovered_from: str = ""
    final_url: str = ""
    status_code: int | None = None
    content_type: str = ""
    elapsed_ms: float = 0.0
    truncated: bool = False
    title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    lang: str = ""
    noindex: bool = False
    nofollow: bool = False
    h1_count: int = 0
    h2_count: int = 0
    word_count: int = 0
    images: int = 0
    images_missing_alt: int = 0
    internal_links: int = 0
    internal_nofollow_links: int = 0
    external_links: int = 0
    outlinks_dropped: int = 0
    #: None when page scoring was switched off - not the same as a score of 0.
    score: float | None = None
    grade: str | None = None
    critical_count: int = 0
    warning_count: int = 0
    #: "product" | "collection" | "" - read from the URL path, not the markup.
    page_type: str = ""
    #: False for a page fetched only to reach the ones that were asked for.
    is_target: bool = True
    redirect_chain: list[str] = Field(default_factory=list)
    skipped: str = ""
    error: str = ""
    #: True when nothing else in this crawl links to the page.
    orphan: bool = False


class SiteCrawlPageDetail(SiteCrawlPage):
    h1: list[str] = Field(default_factory=list)
    robots_meta: str = ""
    #: Per-analyzer score, e.g. {"on_page": 84.0, "technical": 100.0}.
    analyzer_scores: dict[str, float | None] = Field(default_factory=dict)
    #: Non-passed findings, most urgent first. Same shape as an audit fix.
    findings: list[dict[str, Any]] = Field(default_factory=list)
    outlinks: list[str] = Field(default_factory=list)
    #: Crawled pages that link TO this one. The other half of the graph, and what
    #: makes "why is this page not ranking" answerable.
    inlinks: list[str] = Field(default_factory=list)


class SiteCrawlPagesResponse(BaseModel):
    crawl_id: str
    host: str
    pages: list[SiteCrawlPage] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0
    issue: str | None = None
    #: True when navigation-only pages were included in this listing.
    include_navigation: bool = False


class SiteCrawlIssuesResponse(BaseModel):
    """Per-issue page counts plus the orphan list, in one call.

    One request rather than twelve: this is what a report page opens with, and
    every count is an indexed count on the same collection.
    """

    crawl_id: str
    host: str
    status: SiteCrawlStatus
    counts: dict[str, int] = Field(default_factory=dict)
    orphans: list[str] = Field(default_factory=list)
    orphan_count: int = 0
    #: True when the orphan list was cut short - the count is still exact.
    orphans_truncated: bool = False


def page_from_doc(doc: dict[str, Any]) -> SiteCrawlPage:
    """Build a list item from a Mongo doc, ignoring keys the schema does not know.

    `orphan` is read from the doc: the worker marks it once the whole graph is
    known, so it is a stored fact rather than something a read has to recompute.
    """
    fields = SiteCrawlPage.model_fields
    return SiteCrawlPage(**{k: v for k, v in doc.items() if k in fields})
