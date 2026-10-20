"""SQLAlchemy ORM models. Kept lean on purpose: results live in a JSON column."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Audit(Base):
    __tablename__ = "audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: Session user that started the audit, when known. Enables per-user
    #: integrations during the run (Search Console). Nullable: audits can be
    #: created without a session (scripts, future scheduled crawls).
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    results_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class User(Base):
    """Anonymous browser-session users for now; becomes the real account table later."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class GoogleCredential(Base):
    """Google OAuth tokens, one row per user. Never serialized into API responses."""

    __tablename__ = "google_credentials"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class RedditCredential(Base):
    """Reddit OAuth tokens, one row per user. Never serialized into API responses.

    Keyed by the same users.id as GoogleCredential, so one browser session can be
    connected to Google and Reddit at once.
    """

    __tablename__ = "reddit_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reddit_username: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Karma / account age are shown in the UI so the user can judge their own posting limits.
    link_karma: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_karma: Mapped[int | None] = mapped_column(Integer, nullable=True)
    account_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class RedditPost(Base):
    """One generated draft and, once published, its Reddit permalink.

    Also the guardrail ledger: the daily cap, the minimum gap between posts and
    the store+subreddit dedupe window are all computed from published rows here.
    """

    __tablename__ = "reddit_posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    store_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subreddit: Mapped[str] = mapped_column(String(64), nullable=False)
    angle: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="self")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    flair_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flair_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_check_notes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    reddit_post_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    permalink: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    gemini_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auto_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StoreCrawl(Base):
    """One catalog crawl of one store. The client polls this row for progress.

    Job state deliberately lives in SQL, not MongoDB: Mongo writes are best-effort
    in this codebase, and a crawl the client cannot poll is worse than a crawl
    whose catalog was not kept. The catalog itself lives in MongoDB
    (db/store_catalog.py), keyed by store_domain.

    Every counter ships in v1 on purpose. db/session.py::_add_missing_columns can
    only ALTER TABLE ADD a column that is nullable or has a server_default, and
    `Integer, nullable=False, default=0` is a Python-side default that does NOT
    qualify - a counter added later must be nullable and read as `row.x or 0`.
    """

    __tablename__ = "store_crawls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    store_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: Lowercased host without "www." - the MongoDB catalog identity. Denormalised
    #: onto the row so the read endpoints never re-derive it with different rules.
    store_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Session user that asked, when known. Nullable and deliberately NOT a
    #: foreign key, for the same reason as Audit.user_id: most sessions have no
    #: user_id (it is only set after an OAuth round trip).
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: pending | discovering | enriching | complete | partial | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    #: products_json | sitemap | mixed | none ("none" = discovery never finished)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    products_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_enriched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collections_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collections_enriched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Items phase 2 tried and could not read a <head> for.
    enrich_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Items phase 2 never attempted because MAX_ENRICH_ITEMS or the deadline bound first.
    enrich_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Docs MongoDB accepted. 0 with status "complete" means Mongo was down - the
    #: crawl ran, the catalog was not kept. persistence_ok says so explicitly.
    items_persisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revisions_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persistence_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Shop-name suffix inferred from the titles, e.g. "clean eatz kitchen".
    shop_suffix: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Per-flag item counts at completion, e.g. {"meta_title_is_default": 3}.
    flag_counts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    #: Phase 1 finished - the catalog is readable while phase 2 still runs.
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SiteCrawl(Base):
    """One site-wide crawl of one host. The client polls this row for progress.

    Sibling of StoreCrawl and deliberately a separate table, not a `kind` column
    on it: a catalog crawl counts products and collections, a site crawl counts
    pages and depth, and the two share almost no counter. Fusing them would mean
    a row where half the columns are always null.

    Job state lives in SQL for StoreCrawl's reason: Mongo writes are best-effort
    here, and a crawl the client cannot poll is worse than one whose pages were
    not kept. The pages themselves live in MongoDB (db/site_pages.py), keyed by
    crawl id.

    Every counter ships in v1 on purpose - see the same note on StoreCrawl:
    `Integer, nullable=False, default=0` is a Python-side default that
    db/session.py::_add_missing_columns cannot ALTER TABLE ADD later.
    """

    __tablename__ = "site_crawls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    start_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: Lower-cased host of the seed, after normalisation. The crawl identity, and
    #: what every same-site decision was made against.
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    site_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Nullable and deliberately not a foreign key, as on Audit.user_id.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: pending | crawling | complete | partial | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # --- the bounds this crawl actually ran under ---------------------------------
    #: Stored, not just configured: the limits can be overridden per request, and a
    #: report that says "we stopped at 500 pages" needs to know it was 500.
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- progress -----------------------------------------------------------------
    pages_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    client_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    server_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transport_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_by_robots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_asset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_non_html: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_off_site: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Inside the site but past max_depth. Tells the user which bound bound first.
    beyond_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seeded_from_sitemap: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_depth_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- why it stopped -------------------------------------------------------------
    hit_page_budget: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deadline_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    aborted_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- page-type filter ----------------------------------------------------------------
    #: Comma-joined catalog page types this crawl was restricted to, e.g.
    #: "product,collection". Null or empty means every page. Denormalised onto the
    #: row (it also lives in `options`) because every report reads it.
    page_types: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Pages matching the filter - what the crawl was asked for.
    target_pages: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    #: Pages fetched only to reach the targets. Shown so a filtered crawl can say
    #: what it cost beyond the pages it returned.
    navigation_pages: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    #: URLs refused outright for being outside the filter.
    skipped_page_type: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    #: Rate-limit responses received, and pages given up to them. Nullable for the
    #: same reason as the counters above: added after the table existed.
    throttled: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    gave_up_throttled: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    #: Distinct rate-limit episodes (a burst of 429s is one), the seconds of pause
    #: they imposed, and the request rate right now. Nullable: added later.
    throttle_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    throttle_pause_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_rate_rps: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The sustainable rate this host showed us. The next crawl of the same host
    #: starts here instead of rediscovering it by drawing 429s.
    learned_rate_rps: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The time limit actually applied, after sizing it to the site.
    deadline_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Catalog-first phase (Shopify only): read the whole catalog as JSON before
    #: fetching pages. NULL status = not a Shopify store, phase not run. The items
    #: themselves live in MongoDB (db/site_catalog.CATALOG_COLLECTION).
    catalog_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    catalog_products: Mapped[int | None] = mapped_column(Integer, nullable=True)
    catalog_collections: Mapped[int | None] = mapped_column(Integer, nullable=True)
    catalog_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    catalog_elapsed_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: {gap: count} over the catalog, e.g. {"image_alt_missing": 412}.
    catalog_gaps: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: Catalog URLs admitted to the frontier (the rest were already queued).
    seeded_from_catalog: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Distinct templates seen, and how many got a Lighthouse report. The reports
    #: themselves live in MongoDB (db/site_pages.TEMPLATES_COLLECTION).
    templates_found: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    templates_measured: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    # --- analysis ----------------------------------------------------------------------
    #: Pages an analyzer scored, and the mean of their scores. A null avg_score
    #: means page scoring was switched off, which is not "every page scored 0".
    #:
    #: These four are nullable where every counter above is NOT NULL, and that is
    #: deliberate: they were added after `site_crawls` already existed in dev
    #: databases, and db/session.py::_add_missing_columns can only ALTER TABLE ADD
    #: a column that is nullable or carries a server_default. The class docstring
    #: spells this out - read them as `row.x or 0`.
    pages_analyzed: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    avg_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    critical_findings: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    warning_findings: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    # --- headline findings -----------------------------------------------------------
    #: Fetched pages that no other crawled page links to. The finding a site-wide
    #: crawl exists to produce; a single-page audit cannot see it.
    orphan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    noindex_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_title_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_description_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Pages answering 4xx/5xx that something on the site links to - broken links.
    broken_page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- persistence ------------------------------------------------------------------
    #: Docs MongoDB accepted. 0 in a terminal status means Mongo was down: the
    #: crawl ran, the pages were not kept. persistence_ok says so explicitly.
    pages_persisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persistence_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: The non-default CrawlLimits fields this crawl was asked for, e.g.
    #: {"include_subdomains": true}. max_pages/max_depth have their own columns
    #: because reports filter on them; the rest live here so the worker can
    #: rebuild the exact limits without a column per knob.
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: Free-text notes from CrawlStats, e.g. "sitemap seeding stopped at 1000 URLs".
    notes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    elapsed_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Last sign of life from the worker, refreshed on every progress flush.
    #:
    #: Startup reconciliation used to fail EVERY non-terminal row, on the
    #: assumption that a fresh process means the old one died. That is wrong the
    #: moment two processes share this database: a second server starting - even
    #: one that then fails to bind its port - killed crawls that were running
    #: perfectly well in the first. Reconciliation now only claims rows whose
    #: heartbeat has gone quiet.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SiteAnalysis(Base):
    """One full-store analysis: the pipeline that orchestrates crawl + GSC + report.

    A thin coordinator row. It spawns a normal SiteCrawl (referenced by
    `crawl_id`) and reuses that crawl's pages, scores and per-template Lighthouse
    wholesale rather than re-doing any of it. What it adds - the Search Console
    read and the sampled schema check - is small and lives in `result_json`; the
    heavy page data stays in the crawl's MongoDB collection.

    A new table, so NOT NULL defaults are fine (create_all builds it whole); the
    nullable-column rule only bites columns ADDED to a table that already exists.
    """

    __tablename__ = "site_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    start_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    site_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Session user, when known. Nullable and not a foreign key, as on Audit.user_id.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: pending | running | complete | partial | failed | stopped
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    #: The step currently running, for the progress UI: crawl | gsc | report | done.
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    #: The crawl this analysis spawned; its pages/scores/templates are reused whole.
    crawl_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # --- Search Console outcome (best-effort) ------------------------------------------
    gsc_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gsc_site_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    gsc_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- headline roll-ups, for the list/summary without opening result_json -----------
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    indexed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_indexed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    schema_gap_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    templates_measured: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The crawl options this analysis was asked for, so the worker rebuilds the
    #: same limits and a report can state what was requested.
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: The GSC read and the sampled schema aggregate. The crawl's own data lives in
    #: MongoDB; this holds only what the pipeline adds on top.
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Last sign of life from the worker; startup reconciliation only reaps stale rows.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
