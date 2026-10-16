"""Full-store-analysis controller: orchestration only."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from schemas import (
    ReportEngine,
    SiteAnalysisCreatedResponse,
    SiteAnalysisCreateRequest,
    SiteAnalysisListResponse,
    SiteAnalysisStatusResponse,
)
from services import site_analysis_service


async def create_analysis(
    payload: SiteAnalysisCreateRequest, session: AsyncSession, user_id: str | None
) -> SiteAnalysisCreatedResponse:
    return await site_analysis_service.create_analysis(payload, session, user_id)


async def get_analysis(analysis_id: str, session: AsyncSession) -> SiteAnalysisStatusResponse:
    return await site_analysis_service.get_analysis(analysis_id, session)


async def list_analyses(
    session: AsyncSession, *, host: str | None, limit: int
) -> SiteAnalysisListResponse:
    return await site_analysis_service.list_analyses(session, host=host, limit=limit)


async def stop_analysis(analysis_id: str, session: AsyncSession) -> SiteAnalysisStatusResponse:
    return await site_analysis_service.stop_analysis(analysis_id, session)


async def get_analysis_export(
    analysis_id: str, session: AsyncSession, *, fmt: str
) -> tuple[AsyncIterator[bytes], str, str]:
    return await site_analysis_service.get_analysis_export(analysis_id, session, fmt=fmt)


async def generate_analysis_report(
    analysis_id: str, session: AsyncSession, *, engine: ReportEngine
) -> tuple[bytes, str]:
    return await site_analysis_service.generate_analysis_report(analysis_id, session, engine=engine)
