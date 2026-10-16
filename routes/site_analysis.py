"""Full-store-analysis endpoints.

One prefix, /api/analyses. The pipeline spawns a normal crawl and reuses its
exports, so /export.{fmt} here is the crawl's export by another name; /report.html
is the richer analysis report (crawl + Search Console + schema).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from controller import site_analysis as controller
from db.session import get_session
from schemas import (
    ReportEngine,
    SiteAnalysisCreatedResponse,
    SiteAnalysisCreateRequest,
    SiteAnalysisListResponse,
    SiteAnalysisStatusResponse,
)

router = APIRouter(prefix="/api/analyses", tags=["analyses"])


@router.post("", response_model=SiteAnalysisCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_analysis(
    payload: SiteAnalysisCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SiteAnalysisCreatedResponse:
    """Start a full store analysis: crawl, then Search Console and schema on top."""
    return await controller.create_analysis(payload, session, request.session.get("user_id"))


@router.get("", response_model=SiteAnalysisListResponse)
async def list_analyses(
    host: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> SiteAnalysisListResponse:
    return await controller.list_analyses(session, host=host, limit=limit)


@router.get("/{analysis_id}", response_model=SiteAnalysisStatusResponse)
async def get_analysis(
    analysis_id: str, session: AsyncSession = Depends(get_session)
) -> SiteAnalysisStatusResponse:
    """Poll the pipeline: phase, progress and the roll-up findings."""
    return await controller.get_analysis(analysis_id, session)


@router.post("/{analysis_id}/stop", response_model=SiteAnalysisStatusResponse)
async def stop_analysis(
    analysis_id: str, session: AsyncSession = Depends(get_session)
) -> SiteAnalysisStatusResponse:
    """Stop a running analysis and the crawl it spawned, keeping what was found."""
    return await controller.stop_analysis(analysis_id, session)


@router.get("/{analysis_id}/export.{fmt}")
async def export_analysis(
    analysis_id: str,
    fmt: str = Path(pattern="^(csv|json)$", description="csv or json"),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Download the analysis's page data as CSV or JSON (the spawned crawl's export)."""
    stream, media_type, filename = await controller.get_analysis_export(
        analysis_id, session, fmt=fmt
    )
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get("/{analysis_id}/report.html")
async def report_analysis(
    analysis_id: str,
    engine: ReportEngine = Query(
        default="gemini",
        description=(
            "Who writes it: `gemini` (hosted), `ollama` (the local model server) or "
            "`claude` (the Claude Code CLI logged in on the server)"
        ),
    ),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Generate the AI SEO audit report for the whole analysis and download it."""
    document, filename = await controller.generate_analysis_report(
        analysis_id, session, engine=engine
    )
    return Response(
        content=document,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
