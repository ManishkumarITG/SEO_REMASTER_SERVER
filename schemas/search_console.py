"""Google Search Console schemas.

Live Google responses only. Search Console data is not persisted, so there are
no stored-history schemas here.
"""

from __future__ import annotations

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
