"""Pydantic schemas for every API input/output."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from services.analyzers.base import AnalyzerResult, Finding
from services.backlinks.reddit_post_generator import PostAngle
from services.backlinks.store_profile import normalize_store_url

AuditStatus = Literal["pending", "running", "complete", "failed"]
RedditPostKind = Literal["self", "link"]
RedditPostStatus = Literal["draft", "published", "failed"]


class AuditCreateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048, description="URL to audit")

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        url = value.strip()
        if not url:
            raise ValueError("URL must not be empty")
        if "://" not in url:
            url = f"https://{url}"
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError("Only http(s) URLs are supported")
        hostname = parts.hostname or ""
        if not hostname or ("." not in hostname and hostname != "localhost"):
            raise ValueError("URL must include a valid host")
        return urlunsplit(parts)


class AuditCreatedResponse(BaseModel):
    audit_id: str


class PageMeta(BaseModel):
    requested_url: str | None = None
    final_url: str | None = None
    status_code: int | None = None
    load_time_ms: float | None = None
    fetched_at: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    #: True when the page exceeded the fetcher's size cap and was analyzed only
    #: as far as it was read - word counts and link counts are then lower bounds.
    truncated: bool = False


class PrioritizedFix(Finding):
    analyzer: str
    category: str


class CategoryReport(BaseModel):
    name: str
    score: float | None = None
    analyzers: list[AnalyzerResult] = Field(default_factory=list)


class AuditResponse(BaseModel):
    audit_id: str
    url: str
    status: AuditStatus
    created_at: datetime
    completed_at: datetime | None = None
    overall_score: float | None = None
    grade: str | None = None
    categories: list[CategoryReport] = Field(default_factory=list)
    prioritized_fixes: list[PrioritizedFix] = Field(default_factory=list)
    page: PageMeta | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    mongodb: Literal["connected", "unavailable"] = "unavailable"
    reddit: Literal["configured", "unconfigured"] = "unconfigured"
    gemini: Literal["configured", "unconfigured"] = "unconfigured"


# --- Google Search Console -------------------------------------------------


class ConnectionStatus(BaseModel):
    connected: bool


class GoogleSite(BaseModel):
    siteUrl: str
    permissionLevel: str


class GoogleSitesResponse(BaseModel):
    sites: list[GoogleSite]


class SearchAnalyticsRow(BaseModel):
    keys: list[str] = Field(default_factory=list)
    clicks: float = 0
    impressions: float = 0
    ctr: float = 0
    position: float = 0


class SearchAnalyticsResponse(BaseModel):
    rows: list[SearchAnalyticsRow]
    rowCount: int
    stored: int = Field(default=0, description="Rows written to MongoDB for this query")


# --- Stored (MongoDB) Search Console data ----------------------------------


class StoredGscSite(BaseModel):
    site_url: str
    permission_level: str = "unknown"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class StoredGscSitesResponse(BaseModel):
    sites: list[StoredGscSite]


class StoredAnalyticsRow(BaseModel):
    site_url: str
    start_date: str
    end_date: str
    dimensions: str
    keys: list[str] = Field(default_factory=list)
    clicks: float = 0
    impressions: float = 0
    ctr: float = 0
    position: float = 0
    fetched_at: datetime | None = None
    first_fetched_at: datetime | None = None


class StoredAnalyticsResponse(BaseModel):
    rows: list[StoredAnalyticsRow]
    rowCount: int


class GscSyncLogEntry(BaseModel):
    site_url: str
    start_date: str
    end_date: str
    dimensions: str
    row_count: int = 0
    rows_written: int = 0
    requested_at: datetime | None = None


class GscSyncLogResponse(BaseModel):
    syncs: list[GscSyncLogEntry]


# --- Reddit backlink -------------------------------------------------------


def _clean_subreddit(value: str) -> str:
    """Accept "r/name", "/r/name" or "name"; store the bare name."""
    name = value.strip().strip("/")
    for prefix in ("r/", "R/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    name = name.strip("/")
    if not name:
        raise ValueError("Subreddit must not be empty")
    # str.isalnum() is true for any Unicode letter or digit ("tEst", "١"), which
    # Reddit does not accept - the request would fail there instead of here.
    if not all((char.isascii() and char.isalnum()) or char == "_" for char in name):
        raise ValueError("Subreddit names contain only letters, digits and underscores")
    if len(name) > 40:
        raise ValueError("Subreddit name is too long")
    return name


class RedditConnectionStatus(BaseModel):
    configured: bool = False
    connected: bool = False
    username: str | None = None
    link_karma: int | None = None
    comment_karma: int | None = None
    account_age_days: int | None = None


class SubredditSummary(BaseModel):
    name: str
    title: str = ""
    subscribers: int = 0
    you_moderate: bool = False


class SubredditsResponse(BaseModel):
    moderated: list[SubredditSummary] = Field(default_factory=list)
    subscribed: list[SubredditSummary] = Field(default_factory=list)


class SubredditRuleOut(BaseModel):
    short_name: str = ""
    description: str = ""


class LinkFlairOut(BaseModel):
    id: str = ""
    text: str = ""


class SubredditRulesResponse(BaseModel):
    subreddit: str
    rules: list[SubredditRuleOut] = Field(default_factory=list)
    flairs: list[LinkFlairOut] = Field(default_factory=list)
    self_promo_warning: bool = False
    public_description: str = ""


class StoreProfileSummary(BaseModel):
    store_url: str
    store_name: str = ""
    tagline: str = ""
    is_shopify: bool = False
    product_count: int = 0
    categories: list[str] = Field(default_factory=list)
    products_source: str = "none"


class PublishResult(BaseModel):
    draft_id: str
    status: RedditPostStatus
    subreddit: str
    permalink: str | None = None
    reddit_post_id: str | None = None
    published_at: datetime | None = None
    auto_published: bool = False
    error_message: str | None = None
    ratelimit_remaining: float | None = None


class RedditDraftRequest(BaseModel):
    store_url: str = Field(min_length=1, max_length=2048)
    subreddit: str = Field(min_length=1, max_length=64)
    angle: PostAngle = PostAngle.INTRO
    kind: RedditPostKind = "self"
    flair_id: str | None = Field(default=None, max_length=64)
    extra_notes: str | None = Field(default=None, max_length=2000)
    #: Review-before-publish is the default; this is an explicit opt-in per request.
    auto_publish: bool = False

    @field_validator("store_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_store_url(value)

    @field_validator("subreddit")
    @classmethod
    def _subreddit(cls, value: str) -> str:
        return _clean_subreddit(value)


class RedditDraftResponse(BaseModel):
    draft_id: str
    status: RedditPostStatus
    subreddit: str
    angle: PostAngle
    kind: RedditPostKind
    title: str
    body_markdown: str
    suggested_flair_text: str | None = None
    flair_id: str | None = None
    disclosure_included: bool = False
    fit_score: int | None = None
    rule_check_notes: list[str] = Field(default_factory=list)
    gemini_model: str | None = None
    created_at: datetime
    store_profile: StoreProfileSummary
    #: Present only when auto_publish was requested.
    publish: PublishResult | None = None


class RedditPublishRequest(BaseModel):
    """Edited fields override what was stored on the draft."""

    title: str | None = Field(default=None, max_length=300)
    body_markdown: str | None = Field(default=None, max_length=40_000)
    flair_id: str | None = Field(default=None, max_length=64)
    kind: RedditPostKind | None = None


class RedditPostSummary(BaseModel):
    draft_id: str
    created_at: datetime
    published_at: datetime | None = None
    store_url: str
    store_name: str | None = None
    subreddit: str
    angle: str
    kind: RedditPostKind = "self"
    title: str
    status: RedditPostStatus
    fit_score: int | None = None
    permalink: str | None = None
    error_message: str | None = None
    auto_published: bool = False


class RedditPostsResponse(BaseModel):
    posts: list[RedditPostSummary] = Field(default_factory=list)
