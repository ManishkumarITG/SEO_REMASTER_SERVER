"""Store catalog crawl endpoints.

Declaration order is load-bearing: every literal-prefixed path (`/crawl`,
`/crawls`, `/crawl/{id}`) must come BEFORE `/{store_domain}/...`, or a request
for `/api/stores/crawls` is captured as a store domain named "crawls".
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from controller import stores as stores_controller
from db.session import get_session
from schemas import (
    StoreCatalogItemDetailResponse,
    StoreCatalogItemKind,
    StoreCatalogResponse,
    StoreCatalogSort,
    StoreCrawlCreatedResponse,
    StoreCrawlCreateRequest,
    StoreCrawlListResponse,
    StoreCrawlStatusResponse,
    StoreFlagCountsResponse,
)

router = APIRouter(prefix="/api/stores", tags=["stores"])


# --- Crawl jobs (literal paths first) --------------------------------------------


@router.post("/crawl", response_model=StoreCrawlCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_store_crawl(
    payload: StoreCrawlCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StoreCrawlCreatedResponse:
    """Probe the store (2-4 requests), then crawl every product and collection in the background."""
    return await stores_controller.create_store_crawl(
        payload, background_tasks, session, request.session.get("user_id")
    )


@router.get("/crawls", response_model=StoreCrawlListResponse)
async def list_store_crawls(
    store_domain: str | None = Query(default=None, description="Filter to one store"),
    limit: int = Query(default=25, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> StoreCrawlListResponse:
    """Recent crawls, newest first."""
    return await stores_controller.list_store_crawls(session, store_domain=store_domain, limit=limit)


@router.get("/crawl/{crawl_id}", response_model=StoreCrawlStatusResponse)
async def get_store_crawl(crawl_id: str, session: AsyncSession = Depends(get_session)) -> StoreCrawlStatusResponse:
    """Poll a crawl: status, progress counters and flag tallies."""
    return await stores_controller.get_store_crawl(crawl_id, session)


# --- Catalog reads (MongoDB) -----------------------------------------------------------


@router.get("/{store_domain}/flags", response_model=StoreFlagCountsResponse)
async def get_flag_counts(
    store_domain: str,
    kind: StoreCatalogItemKind | None = Query(default=None),
) -> StoreFlagCountsResponse:
    """How many items carry each SEO gap flag."""
    return await stores_controller.get_flag_counts(store_domain, kind)


@router.get("/{store_domain}/products", response_model=StoreCatalogResponse)
async def get_products(
    store_domain: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: StoreCatalogSort = Query(default="gaps"),
    only_gaps: bool = Query(default=False),
    flag: str | None = Query(default=None, description="Only items carrying this flag"),
    session: AsyncSession = Depends(get_session),
) -> StoreCatalogResponse:
    """The crawled products, worst SEO gaps first by default.

    `only_gaps` under-reports while a crawl is in flight: phase-1 docs carry no
    flags until enrichment reaches them. Wait for a terminal crawl status.
    """
    return await stores_controller.get_catalog(
        store_domain, "product", session, limit=limit, offset=offset, sort=sort, only_gaps=only_gaps, flag=flag
    )


@router.get("/{store_domain}/collections", response_model=StoreCatalogResponse)
async def get_collections(
    store_domain: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: StoreCatalogSort = Query(default="gaps"),
    only_gaps: bool = Query(default=False),
    flag: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> StoreCatalogResponse:
    """The crawled collections, worst SEO gaps first by default."""
    return await stores_controller.get_catalog(
        store_domain, "collection", session, limit=limit, offset=offset, sort=sort, only_gaps=only_gaps, flag=flag
    )


@router.get("/{store_domain}/items/{kind}/{handle}", response_model=StoreCatalogItemDetailResponse)
async def get_catalog_item(
    store_domain: str, kind: StoreCatalogItemKind, handle: str
) -> StoreCatalogItemDetailResponse:
    """One item with its full description and change history."""
    return await stores_controller.get_catalog_item(store_domain, kind, handle)
