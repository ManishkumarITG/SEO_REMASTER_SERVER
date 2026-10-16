"""Full-store-analysis request/response schemas. snake_case, like the crawl schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from schemas.site_crawl import MAX_REQUESTABLE_DEPTH, MAX_REQUESTABLE_PAGES, SiteCrawlPageType
from services.crawler.urls import normalize_seed_url

SiteAnalysisStatus = Literal["pending", "running", "complete", "partial", "failed", "stopped"]
SiteAnalysisPhase = Literal["pending", "crawl", "gsc", "schema", "done"]


class SiteAnalysisCreateRequest(BaseModel):
    """The store to analyse, plus every crawler option the site-crawl form exposes."""

    start_url: str = Field(min_length=1, max_length=2048, description="Any page on the store")
    # The full crawler surface, mirroring SiteCrawlCreateRequest so the analysis
    # form offers the same controls.
    max_pages: int | None = Field(default=None, ge=0, le=MAX_REQUESTABLE_PAGES)
    max_depth: int | None = Field(default=None, ge=0, le=MAX_REQUESTABLE_DEPTH)
    include_subdomains: bool | None = None
    follow_nofollow: bool | None = None
    seed_from_sitemap: bool | None = None
    page_types: list[SiteCrawlPageType] = Field(default_factory=list)
    max_navigation_pages: int | None = Field(default=None, ge=0, le=MAX_REQUESTABLE_PAGES)

    @field_validator("start_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        normalized = normalize_seed_url(value)
        if normalized is None:
            raise ValueError("start_url must be an http(s) URL with a host")
        return normalized

    def crawl_options(self) -> dict[str, Any]:
        """The options to persist and hand to the spawned crawl (only what was set)."""
        options: dict[str, Any] = {
            name: value
            for name, value in (
                ("max_pages", self.max_pages),
                ("max_depth", self.max_depth),
                ("include_subdomains", self.include_subdomains),
                ("follow_nofollow", self.follow_nofollow),
                ("seed_from_sitemap", self.seed_from_sitemap),
                ("max_navigation_pages", self.max_navigation_pages),
            )
            if value is not None
        }
        if self.page_types:
            options["page_types"] = [t for t in ("product", "collection") if t in self.page_types]
        return options


class SiteAnalysisCreatedResponse(BaseModel):
    analysis_id: str
    start_url: str
    host: str
    status: SiteAnalysisStatus = "pending"
    #: True when an analysis of this host was already running and was handed back.
    reused: bool = False


class SiteAnalysisStatusResponse(BaseModel):
    analysis_id: str
    start_url: str
    host: str
    site_name: str | None = None
    status: SiteAnalysisStatus
    phase: SiteAnalysisPhase
    #: 0-100 across the pipeline's phases, for the progress bar.
    progress_pct: int = 0
    crawl_id: str | None = None

    gsc_connected: bool = False
    gsc_site_url: str | None = None
    gsc_error: str | None = None

    pages_fetched: int = 0
    avg_score: float | None = None
    grade: str | None = None
    indexed_count: int = 0
    not_indexed_count: int = 0
    schema_gap_count: int = 0
    templates_measured: int = 0

    #: The Search Console read and the sampled schema aggregate, when finished.
    result: dict[str, Any] | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SiteAnalysisSummary(BaseModel):
    analysis_id: str
    start_url: str
    host: str
    site_name: str | None = None
    status: SiteAnalysisStatus
    pages_fetched: int = 0
    avg_score: float | None = None
    grade: str | None = None
    gsc_connected: bool = False
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class SiteAnalysisListResponse(BaseModel):
    analyses: list[SiteAnalysisSummary] = Field(default_factory=list)
    total: int = 0
