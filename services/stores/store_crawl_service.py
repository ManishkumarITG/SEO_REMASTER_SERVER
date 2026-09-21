"""Store catalog crawl service: the HTTP-facing layer.

Create-then-schedule mirrors audit_service.py: the row is committed BEFORE the
background task is registered, because the worker re-fetches it by id on its own
session. Only the id crosses into the worker.

The POST runs a synchronous probe (2-4 small requests) so a non-store answers
422 and a robots-blocked one 403 immediately, instead of a job that fails two
seconds later. Full pagination and enrichment happen in the worker, so endpoint
latency is independent of catalog size. The worker re-probes (idempotent, ~1 s)
rather than receiving the probe object - keeping the "only the id crosses" rule.

Reads: 200 with an empty catalog for a never-crawled store (a legitimate empty
state, the UI's cue to offer a crawl), 404 for an unknown crawl id or absent
item, and 503 with the SAME string search_console_service uses when MongoDB is
unreachable so the frontend's error handling stays uniform. No 401 anywhere:
crawls are anonymous-friendly by design.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from db import store_catalog
from exceptions.crawler import FetchError
from exceptions.stores import CatalogUnavailable, RobotsDisallowed
from models.db import StoreCrawl
from schemas import (
    StoreCatalogItem,
    StoreCatalogItemDetailResponse,
    StoreCatalogItemRevision,
    StoreCatalogResponse,
    StoreCrawlCreatedResponse,
    StoreCrawlCreateRequest,
    StoreCrawlListResponse,
    StoreCrawlStatusResponse,
    StoreCrawlSummary,
    StoreFlagCountsResponse,
)
from services.crawler.http_client import crawl_client
from services.stores.discovery import probe_store
from services.stores.store_key import store_domain_of
from workers.tasks import run_store_crawl

logger = logging.getLogger("seo.stores")

_TERMINAL = ("complete", "partial", "failed")


def _mongo_503(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}")


def _progress_pct(row: StoreCrawl) -> int:
    if row.status in _TERMINAL:
        return 100
    if row.status == "pending":
        return 0
    if row.status == "discovering":
        return 5
    total = row.products_total + row.collections_total
    if total <= 0:
        return 5
    done = row.products_enriched + row.collections_enriched + row.enrich_failures
    return min(99, 5 + int(95 * done / total))


def _status_response(row: StoreCrawl) -> StoreCrawlStatusResponse:
    return StoreCrawlStatusResponse(
        crawl_id=row.id,
        store_url=row.store_url,
        store_domain=row.store_domain,
        store_name=row.store_name,
        status=row.status,  # type: ignore[arg-type]
        source=row.source,  # type: ignore[arg-type]
        products_total=row.products_total,
        products_enriched=row.products_enriched,
        collections_total=row.collections_total,
        collections_enriched=row.collections_enriched,
        enrich_failures=row.enrich_failures,
        enrich_skipped=row.enrich_skipped,
        progress_pct=_progress_pct(row),
        items_persisted=row.items_persisted,
        revisions_written=row.revisions_written,
        persistence_ok=row.persistence_ok,
        shop_suffix=row.shop_suffix,
        flag_counts=dict(row.flag_counts or {}),
        error=row.error_message,
        created_at=row.created_at,
        discovered_at=row.discovered_at,
        completed_at=row.completed_at,
    )


def _summary(row: StoreCrawl) -> StoreCrawlSummary:
    return StoreCrawlSummary(
        crawl_id=row.id,
        store_url=row.store_url,
        store_domain=row.store_domain,
        store_name=row.store_name,
        status=row.status,  # type: ignore[arg-type]
        source=row.source,  # type: ignore[arg-type]
        products_total=row.products_total,
        collections_total=row.collections_total,
        products_enriched=row.products_enriched,
        collections_enriched=row.collections_enriched,
        created_at=row.created_at,
        completed_at=row.completed_at,
        error=row.error_message,
    )


# --- crawls ------------------------------------------------------------------------------


async def create_store_crawl(
    payload: StoreCrawlCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    user_id: str | None,
) -> StoreCrawlCreatedResponse:
    store_url = payload.store_url
    store_domain = store_domain_of(store_url)
    if not store_domain:
        raise HTTPException(status_code=422, detail="Store URL must include a host")

    # Duplicate-crawl guard: a double-clicked button must not start a second
    # multi-minute job. Hand back the one already in flight.
    in_flight = await session.scalar(
        select(StoreCrawl)
        .where(StoreCrawl.store_domain == store_domain, StoreCrawl.status.notin_(_TERMINAL))
        .order_by(desc(StoreCrawl.created_at))
        .limit(1)
    )
    if in_flight is not None:
        return StoreCrawlCreatedResponse(
            crawl_id=in_flight.id, store_domain=store_domain, status=in_flight.status,  # type: ignore[arg-type]
            source=in_flight.source, reused=True,  # type: ignore[arg-type]
        )

    settings = get_settings()
    try:
        async with crawl_client(user_agent=settings.crawler_user_agent, http2=settings.crawl_http2) as client:
            probe = await probe_store(client, store_url)
    except RobotsDisallowed as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc
    except CatalogUnavailable as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=f"Could not reach the store: {exc}") from exc

    if probe.has_products_json:
        source = "products_json"
    elif probe.has_collections_json:
        source = "mixed"
    else:
        source = "sitemap"

    row = StoreCrawl(
        store_url=store_url,
        store_domain=store_domain,
        store_name=probe.origin.site_name[:255] or None,
        user_id=user_id,
        status="pending",
        source=source,
    )
    session.add(row)
    await session.commit()
    background_tasks.add_task(run_store_crawl, row.id)
    return StoreCrawlCreatedResponse(crawl_id=row.id, store_domain=store_domain, status="pending", source=source)  # type: ignore[arg-type]


async def get_store_crawl(crawl_id: str, session: AsyncSession) -> StoreCrawlStatusResponse:
    row = await session.get(StoreCrawl, crawl_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Store crawl not found")
    return _status_response(row)


async def list_store_crawls(
    session: AsyncSession, *, store_domain: str | None, limit: int
) -> StoreCrawlListResponse:
    query = select(StoreCrawl).order_by(desc(StoreCrawl.created_at)).limit(limit)
    if store_domain:
        query = query.where(StoreCrawl.store_domain == _clean_domain(store_domain))
    result = await session.execute(query)
    return StoreCrawlListResponse(crawls=[_summary(row) for row in result.scalars()])


# --- catalog reads ----------------------------------------------------------------------


def _clean_domain(value: str) -> str:
    return store_domain_of(value if "://" in value else f"https://{value}") or value.lower()


async def _latest_crawl(session: AsyncSession, store_domain: str) -> StoreCrawl | None:
    return await session.scalar(
        select(StoreCrawl).where(StoreCrawl.store_domain == store_domain).order_by(desc(StoreCrawl.created_at)).limit(1)
    )


def _item(doc: dict[str, Any]) -> StoreCatalogItem:
    return StoreCatalogItem.model_validate({k: v for k, v in doc.items() if k in StoreCatalogItem.model_fields})


async def get_catalog(
    store_domain: str,
    kind: str,
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    sort: str,
    only_gaps: bool,
    flag: str | None,
) -> StoreCatalogResponse:
    domain = _clean_domain(store_domain)
    try:
        docs, total = await store_catalog.load_catalog_items(
            domain, kind, limit=limit, offset=offset, sort=sort, only_gaps=only_gaps, flag=flag
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean 503 when Mongo is down
        raise _mongo_503(exc) from exc
    latest = await _latest_crawl(session, domain)
    return StoreCatalogResponse(
        store_domain=domain,
        kind=kind,  # type: ignore[arg-type]
        items=[_item(d) for d in docs],
        total=total,
        limit=limit,
        offset=offset,
        last_crawl_id=latest.id if latest else None,
        last_crawl_status=latest.status if latest else None,  # type: ignore[arg-type]
        last_crawled_at=(latest.completed_at or latest.created_at) if latest else None,
    )


async def get_catalog_item(store_domain: str, kind: str, handle: str) -> StoreCatalogItemDetailResponse:
    domain = _clean_domain(store_domain)
    try:
        doc = await store_catalog.load_catalog_item(domain, kind, handle)
        revisions = await store_catalog.load_item_revisions(domain, kind, handle) if doc else []
    except Exception as exc:  # noqa: BLE001
        raise _mongo_503(exc) from exc
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Item not found in the crawled catalog for {domain}")
    return StoreCatalogItemDetailResponse(
        item=_item(doc),
        description=str(doc.get("description") or ""),
        revisions=[StoreCatalogItemRevision.model_validate(r) for r in revisions],
    )


async def get_flag_counts(store_domain: str, kind: str | None) -> StoreFlagCountsResponse:
    domain = _clean_domain(store_domain)
    try:
        counts = await store_catalog.flag_counts(domain, kind)
    except Exception as exc:  # noqa: BLE001
        raise _mongo_503(exc) from exc
    return StoreFlagCountsResponse(store_domain=domain, kind=kind, counts=counts)  # type: ignore[arg-type]


__all__ = [
    "create_store_crawl",
    "get_catalog",
    "get_catalog_item",
    "get_flag_counts",
    "get_store_crawl",
    "list_store_crawls",
]

