"""Store catalog controller: orchestration only."""

from __future__ import annotations

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from schemas import (
    StoreCatalogItemDetailResponse,
    StoreCatalogResponse,
    StoreCrawlCreatedResponse,
    StoreCrawlCreateRequest,
    StoreCrawlListResponse,
    StoreCrawlStatusResponse,
    StoreFlagCountsResponse,
)
from services.stores import store_crawl_service


async def create_store_crawl(
    payload: StoreCrawlCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    user_id: str | None,
) -> StoreCrawlCreatedResponse:
    return await store_crawl_service.create_store_crawl(payload, background_tasks, session, user_id)


async def get_store_crawl(crawl_id: str, session: AsyncSession) -> StoreCrawlStatusResponse:
    return await store_crawl_service.get_store_crawl(crawl_id, session)


async def list_store_crawls(
    session: AsyncSession, *, store_domain: str | None, limit: int
) -> StoreCrawlListResponse:
    return await store_crawl_service.list_store_crawls(session, store_domain=store_domain, limit=limit)


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
    return await store_crawl_service.get_catalog(
        store_domain, kind, session, limit=limit, offset=offset, sort=sort, only_gaps=only_gaps, flag=flag
    )


async def get_catalog_item(store_domain: str, kind: str, handle: str) -> StoreCatalogItemDetailResponse:
    return await store_crawl_service.get_catalog_item(store_domain, kind, handle)


async def get_flag_counts(store_domain: str, kind: str | None) -> StoreFlagCountsResponse:
    return await store_crawl_service.get_flag_counts(store_domain, kind)
