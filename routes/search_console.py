"""Google Search Console data endpoints.

Live endpoints call Google and mirror every result into MongoDB. The
`/api/gsc/stored/*` endpoints read that MongoDB history back without touching
Google, so past pulls stay queryable and API quota is preserved.

Responses never include tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from controller import search_console as search_console_controller
from schemas import (
    GoogleSitesResponse,
    GscSyncLogResponse,
    SearchAnalyticsResponse,
    StoredAnalyticsResponse,
    StoredGscSitesResponse,
)

router = APIRouter(prefix="/api", tags=["search-console"])


# --- Live (Google) ---------------------------------------------------------


@router.get("/sites", response_model=GoogleSitesResponse)
async def list_sites(request: Request) -> GoogleSitesResponse:
    """Verified Search Console properties for the connected Google account."""
    return await search_console_controller.list_sites(request.session.get("user_id"))


@router.get("/search-analytics", response_model=SearchAnalyticsResponse)
async def search_analytics(
    request: Request,
    siteUrl: str = Query(min_length=1, description="Property URL exactly as listed by /api/sites"),
    startDate: str = Query(description="YYYY-MM-DD"),
    endDate: str = Query(description="YYYY-MM-DD"),
    dimensions: str = Query(default="query,page", description="Comma-separated GSC dimensions"),
    rowLimit: int = Query(default=500, ge=1, le=5000),
) -> SearchAnalyticsResponse:
    return await search_console_controller.search_analytics(
        request.session.get("user_id"), siteUrl, startDate, endDate, dimensions, rowLimit
    )


# --- Stored (MongoDB) ------------------------------------------------------


@router.get("/gsc/stored/sites", response_model=StoredGscSitesResponse)
async def stored_sites(request: Request) -> StoredGscSitesResponse:
    """Properties previously saved to MongoDB - served without calling Google."""
    return await search_console_controller.stored_sites(request.session.get("user_id"))


@router.get("/gsc/stored/search-analytics", response_model=StoredAnalyticsResponse)
async def stored_search_analytics(
    request: Request,
    siteUrl: str = Query(min_length=1),
    startDate: str | None = Query(default=None, description="YYYY-MM-DD; omit for all stored ranges"),
    endDate: str | None = Query(default=None, description="YYYY-MM-DD; omit for all stored ranges"),
    dimensions: str | None = Query(default=None, description="Exact dimension string, e.g. 'query'"),
    limit: int = Query(default=500, ge=1, le=5000),
) -> StoredAnalyticsResponse:
    """Metric rows previously saved to MongoDB - served without calling Google."""
    return await search_console_controller.stored_search_analytics(
        request.session.get("user_id"), siteUrl, startDate, endDate, dimensions, limit
    )


@router.get("/gsc/syncs", response_model=GscSyncLogResponse)
async def sync_log(
    request: Request, limit: int = Query(default=25, ge=1, le=200)
) -> GscSyncLogResponse:
    """Audit log of what was pulled from Google and when."""
    return await search_console_controller.sync_log(request.session.get("user_id"), limit)
