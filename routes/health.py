"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from controller import health as health_controller
from schemas import HealthResponse

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return await health_controller.get_health()
