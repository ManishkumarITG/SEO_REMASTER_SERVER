"""Audit endpoints: create (returns immediately, runs in background) and read."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from controller import audits as audits_controller
from db.session import get_session
from schemas import AuditCreatedResponse, AuditCreateRequest, AuditResponse

router = APIRouter(prefix="/api/audits", tags=["audits"])


@router.post("", response_model=AuditCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_audit(
    payload: AuditCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AuditCreatedResponse:
    return await audits_controller.create_audit(
        payload, background_tasks, session, request.session.get("user_id")
    )


@router.get("/{audit_id}", response_model=AuditResponse)
async def get_audit(audit_id: str, session: AsyncSession = Depends(get_session)) -> AuditResponse:
    return await audits_controller.get_audit(audit_id, session)
