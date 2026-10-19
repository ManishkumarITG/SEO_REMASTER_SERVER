"""Google Search Console data endpoints.

Every endpoint reads live from Google and returns the answer straight to the
caller. Search Console data is deliberately not persisted, so there are no
"stored" endpoints to read history back from.

Responses never include tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from controller import search_console as search_console_controller
from schemas import GoogleSitesResponse, SearchAnalyticsResponse

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
