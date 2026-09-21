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
