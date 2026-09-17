"""Google Search Console data endpoints.

Live endpoints call Google and mirror every result into MongoDB. The
`/api/gsc/stored/*` endpoints read that MongoDB history back without touching
Google, so past pulls stay queryable and API quota is preserved.

Responses never include tokens.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from models.schemas import (
    GoogleSite,
    GoogleSitesResponse,
    GscSyncLogResponse,
    SearchAnalyticsResponse,
    SearchAnalyticsRow,
    StoredAnalyticsResponse,
    StoredGscSitesResponse,
)
from services.integrations import mongo
from services.integrations.google_oauth import execute_google_call, get_search_console_service

router = APIRouter(prefix="/api", tags=["search-console"])

_ALLOWED_DIMENSIONS = {"query", "page", "country", "device", "date", "searchAppearance"}


def _require_user(request: Request) -> str:
    user_id = request.session.get("user_id")
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


@router.get("/sites", response_model=GoogleSitesResponse)
async def list_sites(request: Request) -> GoogleSitesResponse:
    """Verified Search Console properties for the connected Google account."""
    user_id = _require_user(request)
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


@router.get("/search-analytics", response_model=SearchAnalyticsResponse)
async def search_analytics(
    request: Request,
    siteUrl: str = Query(min_length=1, description="Property URL exactly as listed by /api/sites"),
    startDate: str = Query(description="YYYY-MM-DD"),
    endDate: str = Query(description="YYYY-MM-DD"),
    dimensions: str = Query(default="query,page", description="Comma-separated GSC dimensions"),
    rowLimit: int = Query(default=500, ge=1, le=5000),
) -> SearchAnalyticsResponse:
    user_id = _require_user(request)
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


@router.get("/gsc/stored/sites", response_model=StoredGscSitesResponse)
async def stored_sites(request: Request) -> StoredGscSitesResponse:
    """Properties previously saved to MongoDB - served without calling Google."""
    user_id = _require_user(request)
    try:
        docs = await mongo.load_sites(user_id)
    except Exception as exc:  # noqa: BLE001 - surface a clean 503 when Mongo is down
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}") from exc
    return StoredGscSitesResponse(sites=docs)


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
    user_id = _require_user(request)
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


@router.get("/gsc/syncs", response_model=GscSyncLogResponse)
async def sync_log(
    request: Request, limit: int = Query(default=25, ge=1, le=200)
) -> GscSyncLogResponse:
    """Audit log of what was pulled from Google and when."""
    user_id = _require_user(request)
    try:
        docs = await mongo.load_syncs(user_id, limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}") from exc
    return GscSyncLogResponse(syncs=docs)
