"""Audit controller: orchestration only."""

from __future__ import annotations

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from schemas import AuditCreatedResponse, AuditCreateRequest, AuditResponse
from services import audit_service


async def create_audit(
    payload: AuditCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    user_id: str | None,
) -> AuditCreatedResponse:
    return await audit_service.create_audit(payload, background_tasks, session, user_id)


async def get_audit(audit_id: str, session: AsyncSession) -> AuditResponse:
    return await audit_service.get_audit(audit_id, session)
