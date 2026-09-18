"""Search Console controller: orchestration only."""

from __future__ import annotations

from schemas import (
    GoogleSitesResponse,
    GscSyncLogResponse,
    SearchAnalyticsResponse,
    StoredAnalyticsResponse,
    StoredGscSitesResponse,
)
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


async def stored_sites(user_id: str | None) -> StoredGscSitesResponse:
    return await search_console_service.stored_sites(user_id)


async def stored_search_analytics(
    user_id: str | None,
    siteUrl: str,
    startDate: str | None,
    endDate: str | None,
    dimensions: str | None,
    limit: int,
) -> StoredAnalyticsResponse:
    return await search_console_service.stored_search_analytics(
        user_id, siteUrl, startDate, endDate, dimensions, limit
    )


async def sync_log(user_id: str | None, limit: int) -> GscSyncLogResponse:
    return await search_console_service.sync_log(user_id, limit)
