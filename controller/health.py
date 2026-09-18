"""Health controller: orchestration only."""

from __future__ import annotations

from schemas import HealthResponse
from services import health_service


async def get_health() -> HealthResponse:
    return await health_service.get_health()
