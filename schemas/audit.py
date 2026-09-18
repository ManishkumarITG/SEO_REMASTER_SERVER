"""Audit request/response schemas.

Split out of the former `models/schemas.py` during the layered-architecture
reorganisation. Field names, types, defaults and validators are copied verbatim,
so the request/response contract is byte-identical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

from services.analyzers.base import AnalyzerResult, Finding

AuditStatus = Literal["pending", "running", "complete", "failed"]


class AuditCreateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048, description="URL to audit")

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        url = value.strip()
        if not url:
            raise ValueError("URL must not be empty")
        if "://" not in url:
            url = f"https://{url}"
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError("Only http(s) URLs are supported")
        hostname = parts.hostname or ""
        if not hostname or ("." not in hostname and hostname != "localhost"):
            raise ValueError("URL must include a valid host")
        return urlunsplit(parts)


class AuditCreatedResponse(BaseModel):
    audit_id: str


class PageMeta(BaseModel):
    requested_url: str | None = None
    final_url: str | None = None
    status_code: int | None = None
    load_time_ms: float | None = None
    fetched_at: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    #: True when the page exceeded the fetcher's size cap and was analyzed only
    #: as far as it was read - word counts and link counts are then lower bounds.
    truncated: bool = False


class PrioritizedFix(Finding):
    analyzer: str
    category: str


class CategoryReport(BaseModel):
    name: str
    score: float | None = None
    analyzers: list[AnalyzerResult] = Field(default_factory=list)


class AuditResponse(BaseModel):
    audit_id: str
    url: str
    status: AuditStatus
    created_at: datetime
    completed_at: datetime | None = None
    overall_score: float | None = None
    grade: str | None = None
    categories: list[CategoryReport] = Field(default_factory=list)
    prioritized_fixes: list[PrioritizedFix] = Field(default_factory=list)
    page: PageMeta | None = None
    error: str | None = None
