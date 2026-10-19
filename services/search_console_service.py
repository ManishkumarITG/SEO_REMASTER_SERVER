"""Google Search Console data service.

Every read goes to Google and is returned straight to the caller. Nothing is
persisted: Google stays the single source of truth for search data, and none of
it accumulates in our own storage.

Responses never include tokens.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from integrations.google.oauth import execute_google_call, get_search_console_service
from schemas import (
    GoogleSite,
    GoogleSitesResponse,
    SearchAnalyticsResponse,
    SearchAnalyticsRow,
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
    return SearchAnalyticsResponse(rows=rows, rowCount=len(rows))
