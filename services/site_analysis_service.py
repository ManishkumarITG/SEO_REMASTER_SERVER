"""Full-store-analysis HTTP layer: create, poll, list, stop, export, report.

Create-then-schedule mirrors the crawl and audit services. Starting an analysis
is gated on a live Search Console connection - the whole point of the pipeline is
to fold GSC in, so without it the analysis would silently drop a third of the
report. The exports and the report reuse the spawned crawl's own endpoints and the
analysis-specific Gemini report respectively; nothing is duplicated.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from integrations.google.oauth import load_credentials
from models.db import SiteAnalysis, SiteCrawl
from schemas import (
    ReportEngine,
    SiteAnalysisCreatedResponse,
    SiteAnalysisCreateRequest,
    SiteAnalysisListResponse,
    SiteAnalysisStatusResponse,
    SiteAnalysisSummary,
)
from services import site_analysis_report, site_crawl_service
from services.crawler.urls import host_of
from services.report_engines import report_errors_as_http
from services.reports.scoring import grade_for
from workers.tasks import (
    ANALYSIS_STALE_AFTER_S,
    request_site_analysis_stop,
    schedule_site_analysis,
    site_analysis_is_running,
)

logger = logging.getLogger("seo.site_analysis")

_TERMINAL = ("complete", "partial", "failed", "stopped")
#: The pipeline's phases in order, for a progress percentage while it runs.
_PHASE_PCT = {"pending": 0, "crawl": 15, "gsc": 75, "schema": 90, "done": 100}


def _progress_pct(row: SiteAnalysis) -> int:
    if row.status in _TERMINAL:
        return 100
    return _PHASE_PCT.get(row.phase, 5)


def _status_response(row: SiteAnalysis) -> SiteAnalysisStatusResponse:
    return SiteAnalysisStatusResponse(
        analysis_id=row.id,
        start_url=row.start_url,
        host=row.host,
        site_name=row.site_name,
        status=row.status,  # type: ignore[arg-type]
        phase=row.phase,  # type: ignore[arg-type]
        progress_pct=_progress_pct(row),
        crawl_id=row.crawl_id,
        gsc_connected=row.gsc_connected,
        gsc_site_url=row.gsc_site_url,
        gsc_error=row.gsc_error,
        pages_fetched=row.pages_fetched,
        avg_score=row.avg_score,
        grade=grade_for(row.avg_score) if row.avg_score is not None else None,
        indexed_count=row.indexed_count,
        not_indexed_count=row.not_indexed_count,
        schema_gap_count=row.schema_gap_count,
        templates_measured=row.templates_measured,
        result=row.result_json,
        options=dict(row.options or {}),
        notes=[str(n) for n in (row.notes or [])],
        error=row.error_message,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _summary(row: SiteAnalysis) -> SiteAnalysisSummary:
    return SiteAnalysisSummary(
        analysis_id=row.id,
        start_url=row.start_url,
        host=row.host,
        site_name=row.site_name,
        status=row.status,  # type: ignore[arg-type]
        pages_fetched=row.pages_fetched,
        avg_score=row.avg_score,
        grade=grade_for(row.avg_score) if row.avg_score is not None else None,
        gsc_connected=row.gsc_connected,
        created_at=row.created_at,
        completed_at=row.completed_at,
        error=row.error_message,
    )


async def _reap_if_abandoned(row: SiteAnalysis, session: AsyncSession) -> SiteAnalysis:
    """Close out an analysis whose worker died, the moment it is read. Mirrors the crawl reaper."""
    if row.status in _TERMINAL or site_analysis_is_running(row.id):
        return row
    last = row.heartbeat_at or row.started_at or row.created_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if last is None or datetime.now(UTC) - last < timedelta(seconds=ANALYSIS_STALE_AFTER_S):
        return row
    row.status = "failed"
    row.phase = "done"
    row.error_message = "The analysis stopped because the server restarted."
    row.completed_at = datetime.now(UTC)
    await session.commit()
    return row


async def _require(analysis_id: str, session: AsyncSession) -> SiteAnalysis:
    row = await session.get(SiteAnalysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found")
    return await _reap_if_abandoned(row, session)


# --- create / poll / list / stop -------------------------------------------------------


async def gsc_connected(user_id: str | None) -> bool:
    """True when this session has a usable Search Console credential.

    A named seam so the create gate is one obvious check, and so tests can drive
    the connected/not-connected paths without forging a signed session cookie.
    """
    return bool(user_id) and await load_credentials(user_id) is not None


async def create_analysis(
    payload: SiteAnalysisCreateRequest, session: AsyncSession, user_id: str | None
) -> SiteAnalysisCreatedResponse:
    host = host_of(payload.start_url)
    if not host:
        raise HTTPException(status_code=422, detail="start_url must include a host")

    # Gate: a full analysis folds in Search Console, so it requires a connection.
    if not await gsc_connected(user_id):
        raise HTTPException(
            status_code=400,
            detail="Connect Google Search Console before running a full store analysis.",
        )

    in_flight = await session.scalar(
        select(SiteAnalysis)
        .where(SiteAnalysis.host == host, SiteAnalysis.status.notin_(_TERMINAL))
        .order_by(desc(SiteAnalysis.created_at))
        .limit(1)
    )
    if in_flight is not None:
        return SiteAnalysisCreatedResponse(
            analysis_id=in_flight.id, start_url=in_flight.start_url, host=host,
            status=in_flight.status, reused=True,  # type: ignore[arg-type]
        )

    row = SiteAnalysis(
        start_url=payload.start_url, host=host, user_id=user_id,
        status="pending", phase="pending", options=payload.crawl_options() or None,
    )
    session.add(row)
    await session.commit()
    schedule_site_analysis(row.id)
    return SiteAnalysisCreatedResponse(analysis_id=row.id, start_url=row.start_url, host=row.host)


async def get_analysis(analysis_id: str, session: AsyncSession) -> SiteAnalysisStatusResponse:
    return _status_response(await _require(analysis_id, session))


async def list_analyses(session: AsyncSession, *, host: str | None, limit: int) -> SiteAnalysisListResponse:
    conditions = [SiteAnalysis.host == host] if host else []
    total = await session.scalar(select(func.count()).select_from(SiteAnalysis).where(*conditions)) or 0
    rows = await session.scalars(
        select(SiteAnalysis).where(*conditions).order_by(desc(SiteAnalysis.created_at)).limit(limit)
    )
    return SiteAnalysisListResponse(analyses=[_summary(r) for r in rows], total=total)


async def stop_analysis(analysis_id: str, session: AsyncSession) -> SiteAnalysisStatusResponse:
    row = await _require(analysis_id, session)
    if row.status in _TERMINAL:
        return _status_response(row)
    request_site_analysis_stop(analysis_id)
    return _status_response(row)


# --- outputs: export (reuse the crawl) + report ----------------------------------------


async def _require_finished_with_crawl(analysis_id: str, session: AsyncSession) -> tuple[SiteAnalysis, SiteCrawl]:
    row = await _require(analysis_id, session)
    if row.status not in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"This analysis is still {row.status}. Wait for it to finish.",
        )
    if not row.crawl_id:
        raise HTTPException(status_code=409, detail="This analysis produced no crawl to export.")
    crawl = await session.get(SiteCrawl, row.crawl_id)
    if crawl is None:
        raise HTTPException(status_code=404, detail="The analysis's crawl data is no longer available.")
    return row, crawl


async def get_analysis_export(
    analysis_id: str, session: AsyncSession, *, fmt: str
) -> tuple[AsyncIterator[bytes], str, str]:
    """CSV/JSON of the analysis, served straight from its spawned crawl's export."""
    row, _crawl = await _require_finished_with_crawl(analysis_id, session)
    # The crawl export already streams, names and formats the page data; reuse it.
    return await site_crawl_service.get_site_crawl_export(row.crawl_id, session, fmt=fmt)


async def generate_analysis_report(
    analysis_id: str, session: AsyncSession, *, engine: ReportEngine = "gemini"
) -> tuple[bytes, str]:
    """The AI audit report for the whole analysis (crawl + GSC + schema)."""
    row, crawl = await _require_finished_with_crawl(analysis_id, session)
    with report_errors_as_http(engine):
        return await site_analysis_report.generate_analysis_report_html(row, crawl, engine=engine)
