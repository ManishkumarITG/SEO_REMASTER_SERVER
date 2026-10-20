"""Site-wide crawl controller: orchestration only."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from schemas import (
    ReportEngine,
    SiteCrawlCatalogResponse,
    SiteCrawlCreatedResponse,
    SiteCrawlCreateRequest,
    SiteCrawlIssuesResponse,
    SiteCrawlListResponse,
    SiteCrawlPageDetail,
    SiteCrawlPagesResponse,
    SiteCrawlStatusResponse,
    SiteCrawlTemplatesResponse,
)
from services import site_crawl_service


async def create_site_crawl(
    payload: SiteCrawlCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    user_id: str | None,
) -> SiteCrawlCreatedResponse:
    return await site_crawl_service.create_site_crawl(payload, background_tasks, session, user_id)


async def get_site_crawl(crawl_id: str, session: AsyncSession) -> SiteCrawlStatusResponse:
    return await site_crawl_service.get_site_crawl(crawl_id, session)


async def stop_site_crawl(crawl_id: str, session: AsyncSession) -> SiteCrawlStatusResponse:
    return await site_crawl_service.stop_site_crawl(crawl_id, session)


async def list_site_crawls(
    session: AsyncSession, *, host: str | None, limit: int
) -> SiteCrawlListResponse:
    return await site_crawl_service.list_site_crawls(session, host=host, limit=limit)


async def get_site_crawl_pages(
    crawl_id: str,
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    sort: str,
    issue: str | None,
    include_navigation: bool,
) -> SiteCrawlPagesResponse:
    return await site_crawl_service.get_site_crawl_pages(
        crawl_id, session, limit=limit, offset=offset, sort=sort, issue=issue,
        include_navigation=include_navigation,
    )


async def get_site_crawl_page(crawl_id: str, url: str, session: AsyncSession) -> SiteCrawlPageDetail:
    return await site_crawl_service.get_site_crawl_page(crawl_id, url, session)


async def get_site_crawl_issues(crawl_id: str, session: AsyncSession) -> SiteCrawlIssuesResponse:
    return await site_crawl_service.get_site_crawl_issues(crawl_id, session)


async def get_site_crawl_templates(
    crawl_id: str, session: AsyncSession
) -> SiteCrawlTemplatesResponse:
    return await site_crawl_service.get_site_crawl_templates(crawl_id, session)


async def get_site_crawl_catalog(
    crawl_id: str,
    session: AsyncSession,
    *,
    kind: str | None,
    gap: str | None,
    offset: int,
    limit: int,
) -> SiteCrawlCatalogResponse:
    return await site_crawl_service.get_site_crawl_catalog(
        crawl_id, session, kind=kind, gap=gap, offset=offset, limit=limit
    )


async def get_site_crawl_export(
    crawl_id: str, session: AsyncSession, *, fmt: str, include_navigation: bool
) -> tuple[AsyncIterator[bytes], str, str]:
    return await site_crawl_service.get_site_crawl_export(
        crawl_id, session, fmt=fmt, include_navigation=include_navigation
    )


async def generate_site_crawl_report(
    crawl_id: str, session: AsyncSession, *, engine: ReportEngine
) -> tuple[bytes, str]:
    return await site_crawl_service.generate_site_crawl_report(crawl_id, session, engine=engine)
