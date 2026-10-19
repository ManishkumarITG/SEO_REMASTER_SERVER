"""Search Console controller: orchestration only."""

from __future__ import annotations

from schemas import GoogleSitesResponse, SearchAnalyticsResponse
from services import search_console_service


async def list_sites(user_id: str | None) -> GoogleSitesResponse:
    return await search_console_service.list_sites(user_id)


async def search_analytics(
    user_id: str | None,
    siteUrl: str,
    startDate: str,
    endDate: str,
    dimensions: str,
    rowLimit: int,
) -> SearchAnalyticsResponse:
    return await search_console_service.search_analytics(
        user_id, siteUrl, startDate, endDate, dimensions, rowLimit
    )
