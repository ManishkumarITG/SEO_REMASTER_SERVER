"""Audit creation and retrieval.

Logic moved verbatim from api/routes/audits.py. The background job is still
registered through FastAPI's BackgroundTasks and still funnels through the
single entry point workers.tasks.run_audit.
"""

from __future__ import annotations

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Audit
from schemas import AuditCreatedResponse, AuditCreateRequest, AuditResponse
from workers.tasks import run_audit


async def create_audit(
    payload: AuditCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    user_id: str | None,
) -> AuditCreatedResponse:
    # Recording the session user lets the search_console analyzer attach this
    # page's real Search Console performance during the run.
    audit = Audit(url=payload.url, status="pending", user_id=user_id)
    session.add(audit)
    await session.commit()
    background_tasks.add_task(run_audit, audit.id)
    return AuditCreatedResponse(audit_id=audit.id)


async def get_audit(audit_id: str, session: AsyncSession) -> AuditResponse:
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
