"""Audit endpoints: create (returns immediately, runs in background) and read."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from models.db import Audit
from models.schemas import AuditCreatedResponse, AuditCreateRequest, AuditResponse
from workers.tasks import run_audit

router = APIRouter(prefix="/api/audits", tags=["audits"])


@router.post("", response_model=AuditCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_audit(
    payload: AuditCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AuditCreatedResponse:
    # Recording the session user lets the search_console analyzer attach this
    # page's real Search Console performance during the run.
    audit = Audit(url=payload.url, status="pending", user_id=request.session.get("user_id"))
    session.add(audit)
    await session.commit()
    background_tasks.add_task(run_audit, audit.id)
    return AuditCreatedResponse(audit_id=audit.id)


@router.get("/{audit_id}", response_model=AuditResponse)
async def get_audit(audit_id: str, session: AsyncSession = Depends(get_session)) -> AuditResponse:
    audit = await session.get(Audit, audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit not found")
    results = audit.results_json or {}
    return AuditResponse(
        audit_id=audit.id,
        url=audit.url,
        status=audit.status,
        created_at=audit.created_at,
        completed_at=audit.completed_at,
        overall_score=audit.overall_score,
        grade=results.get("grade"),
        categories=results.get("categories", []),
        prioritized_fixes=results.get("prioritized_fixes", []),
        page=results.get("page"),
        error=results.get("error"),
    )
