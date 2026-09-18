"""Google Search Console schemas - live (Google) and stored (MongoDB).

Split out of the former `models/schemas.py` during the layered-architecture
reorganisation. Field names, types, defaults and validators are copied verbatim,
so the request/response contract is byte-identical.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ConnectionStatus(BaseModel):
    connected: bool


class GoogleSite(BaseModel):
    siteUrl: str
    permissionLevel: str


class GoogleSitesResponse(BaseModel):
    sites: list[GoogleSite]


class SearchAnalyticsRow(BaseModel):
    keys: list[str] = Field(default_factory=list)
    clicks: float = 0
    impressions: float = 0
    ctr: float = 0
    position: float = 0


class SearchAnalyticsResponse(BaseModel):
    rows: list[SearchAnalyticsRow]
    rowCount: int
    stored: int = Field(default=0, description="Rows written to MongoDB for this query")


# --- Stored (MongoDB) Search Console data ----------------------------------


class StoredGscSite(BaseModel):
    site_url: str
    permission_level: str = "unknown"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class StoredGscSitesResponse(BaseModel):
    sites: list[StoredGscSite]


class StoredAnalyticsRow(BaseModel):
    site_url: str
    start_date: str
    end_date: str
    dimensions: str
    keys: list[str] = Field(default_factory=list)
    clicks: float = 0
    impressions: float = 0
    ctr: float = 0
    position: float = 0
    fetched_at: datetime | None = None
    first_fetched_at: datetime | None = None


class StoredAnalyticsResponse(BaseModel):
    rows: list[StoredAnalyticsRow]
    rowCount: int


class GscSyncLogEntry(BaseModel):
    site_url: str
    start_date: str
    end_date: str
    dimensions: str
    row_count: int = 0
    rows_written: int = 0
    requested_at: datetime | None = None


class GscSyncLogResponse(BaseModel):
    syncs: list[GscSyncLogEntry]
