"""Site-wide crawl endpoints.

One prefix, `/api/site-crawls`, so there is no literal-versus-parameter ordering
trap of the kind routes/stores.py documents: every path below is either the
collection itself or a child of `/{crawl_id}`, and no literal segment competes
with the parameter.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from controller import site_crawl as site_crawl_controller
from db.session import get_session
from schemas import (
    SiteCrawlCreatedResponse,
    SiteCrawlCreateRequest,
    SiteCrawlIssue,
    SiteCrawlIssuesResponse,
    SiteCrawlListResponse,
    SiteCrawlPageDetail,
    SiteCrawlPageSort,
    SiteCrawlPagesResponse,
    SiteCrawlStatusResponse,
)

router = APIRouter(prefix="/api/site-crawls", tags=["site-crawls"])


@router.post("", response_model=SiteCrawlCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_site_crawl(
    payload: SiteCrawlCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SiteCrawlCreatedResponse:
    """Pre-flight the site (2 requests), then crawl it breadth-first in the background."""
    return await site_crawl_controller.create_site_crawl(
        payload, background_tasks, session, request.session.get("user_id")
    )


@router.get("", response_model=SiteCrawlListResponse)
async def list_site_crawls(
    host: str | None = Query(default=None, description="Filter to one host"),
    limit: int = Query(default=25, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> SiteCrawlListResponse:
    """Recent site crawls, newest first."""
    return await site_crawl_controller.list_site_crawls(session, host=host, limit=limit)


@router.get("/{crawl_id}", response_model=SiteCrawlStatusResponse)
async def get_site_crawl(
    crawl_id: str, session: AsyncSession = Depends(get_session)
) -> SiteCrawlStatusResponse:
    """Poll a crawl: status, progress and the headline findings."""
    return await site_crawl_controller.get_site_crawl(crawl_id, session)


@router.get("/{crawl_id}/pages", response_model=SiteCrawlPagesResponse)
async def get_site_crawl_pages(
    crawl_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: SiteCrawlPageSort = Query(default="depth"),
    issue: SiteCrawlIssue | None = Query(default=None, description="Only pages with this problem"),
    include_navigation: bool = Query(
        default=False,
        description="Also list pages fetched only to reach the ones that were asked for",
    ),
    session: AsyncSession = Depends(get_session),
) -> SiteCrawlPagesResponse:
    """The crawled pages, shallowest first by default.

    Counts under-report while a crawl is in flight: pages are written in batches
    as they are fetched, and `orphan` is only set once the whole graph is known.
    Wait for a terminal status before reading them as final.
    """
    return await site_crawl_controller.get_site_crawl_pages(
        crawl_id, session, limit=limit, offset=offset, sort=sort, issue=issue,
        include_navigation=include_navigation,
    )


@router.get("/{crawl_id}/page", response_model=SiteCrawlPageDetail)
async def get_site_crawl_page(
    crawl_id: str,
    url: str = Query(min_length=1, max_length=2048, description="The exact crawled URL"),
    session: AsyncSession = Depends(get_session),
) -> SiteCrawlPageDetail:
    """One page with its outgoing links and the pages that link to it."""
    return await site_crawl_controller.get_site_crawl_page(crawl_id, url, session)


@router.get("/{crawl_id}/issues", response_model=SiteCrawlIssuesResponse)
async def get_site_crawl_issues(
    crawl_id: str, session: AsyncSession = Depends(get_session)
) -> SiteCrawlIssuesResponse:
    """Per-issue page counts plus the orphan list - what a report opens with."""
    return await site_crawl_controller.get_site_crawl_issues(crawl_id, session)
