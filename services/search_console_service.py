"""Google Search Console data service.

Logic moved verbatim from api/routes/search_console.py.

Live functions call Google and mirror every result into MongoDB. The `stored_*`
functions read that MongoDB history back without touching Google, so past pulls
stay queryable and API quota is preserved.

Responses never include tokens.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from db import mongo
from integrations.google.oauth import execute_google_call, get_search_console_service
from schemas import (
    GoogleSite,
    GoogleSitesResponse,
    GscSyncLogResponse,
    SearchAnalyticsResponse,
    SearchAnalyticsRow,
    StoredAnalyticsResponse,
    StoredGscSitesResponse,
)

_ALLOWED_DIMENSIONS = {"query", "page", "country", "device", "date", "searchAppearance"}


def require_user(user_id: str | None) -> str:
    if not user_id:
        raise HTTPException(status_code=401, detail="Google Search Console is not connected")
    return user_id


def _validate_date(name: str, value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be formatted YYYY-MM-DD") from None


def _parse_dimensions(dimensions: str) -> list[str]:
    dims = [d.strip() for d in dimensions.split(",") if d.strip()]
    invalid = sorted(set(dims) - _ALLOWED_DIMENSIONS)
    if not dims or invalid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported dimensions: {', '.join(invalid) or '(none given)'}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_DIMENSIONS))}"
            ),
        )
    return dims


# --- Live (Google) ---------------------------------------------------------


async def list_sites(user_id: str | None) -> GoogleSitesResponse:
    """Verified Search Console properties for the connected Google account."""
    user_id = require_user(user_id)
    service, creds = await get_search_console_service(user_id)
    result = await execute_google_call(user_id, creds, service.sites().list())
    # Every field is read defensively: an entry without siteUrl would otherwise
    # be a KeyError, i.e. a 500 on a response we do not control.
    entries = [
        {"siteUrl": entry["siteUrl"], "permissionLevel": entry.get("permissionLevel", "unknown")}
        for entry in result.get("siteEntry", []) or []
        if entry.get("siteUrl") and entry.get("permissionLevel") != "siteUnverifiedUser"
    ]
    await mongo.save_sites(user_id, entries)  # best-effort mirror
    return GoogleSitesResponse(sites=[GoogleSite(**entry) for entry in entries])


async def search_analytics(
    user_id: str | None,
    siteUrl: str,
    startDate: str,
    endDate: str,
    dimensions: str,
    rowLimit: int,
) -> SearchAnalyticsResponse:
    user_id = require_user(user_id)
    _validate_date("startDate", startDate)
    _validate_date("endDate", endDate)
    if startDate > endDate:
        raise HTTPException(status_code=422, detail="startDate must be on or before endDate")
    dims = _parse_dimensions(dimensions)

    service, creds = await get_search_console_service(user_id)
    body = {"startDate": startDate, "endDate": endDate, "dimensions": dims, "rowLimit": rowLimit}
    result = await execute_google_call(
        user_id, creds, service.searchanalytics().query(siteUrl=siteUrl, body=body)
    )
    raw_rows = result.get("rows", [])
    stored = await mongo.save_search_analytics(  # best-effort mirror
        user_id, siteUrl, startDate, endDate, dims, raw_rows
    )
    rows = [
        SearchAnalyticsRow(
            keys=row.get("keys", []),
            clicks=row.get("clicks", 0),
            impressions=row.get("impressions", 0),
            ctr=row.get("ctr", 0),
            position=row.get("position", 0),
        )
        for row in raw_rows
    ]
    return SearchAnalyticsResponse(rows=rows, rowCount=len(rows), stored=stored)


# --- Stored (MongoDB) ------------------------------------------------------


async def stored_sites(user_id: str | None) -> StoredGscSitesResponse:
    """Properties previously saved to MongoDB - served without calling Google."""
    user_id = require_user(user_id)
    try:
        docs = await mongo.load_sites(user_id)
    except Exception as exc:  # noqa: BLE001 - surface a clean 503 when Mongo is down
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}") from exc
    return StoredGscSitesResponse(sites=docs)


async def stored_search_analytics(
    user_id: str | None,
    siteUrl: str,
    startDate: str | None,
    endDate: str | None,
    dimensions: str | None,
    limit: int,
) -> StoredAnalyticsResponse:
    """Metric rows previously saved to MongoDB - served without calling Google."""
    user_id = require_user(user_id)
    if startDate:
        _validate_date("startDate", startDate)
    if endDate:
        _validate_date("endDate", endDate)
    if dimensions:
        _parse_dimensions(dimensions)
    try:
        docs = await mongo.load_search_analytics(
            user_id, siteUrl, startDate, endDate, dimensions, limit
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}") from exc
    return StoredAnalyticsResponse(rows=docs, rowCount=len(docs))


async def sync_log(user_id: str | None, limit: int) -> GscSyncLogResponse:
    """Audit log of what was pulled from Google and when."""
    user_id = require_user(user_id)
    try:
        docs = await mongo.load_syncs(user_id, limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}") from exc
    return GscSyncLogResponse(syncs=docs)
