"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from config import get_settings
from models.schemas import HealthResponse
from services.integrations.mongo import ping as mongo_ping

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # MongoDB is optional: the API works without it, so report status instead of failing.
    # Reddit / Gemini report only whether keys are present - never which values.
    settings = get_settings()
    return HealthResponse(
        version="0.1.0",
        mongodb="connected" if await mongo_ping() else "unavailable",
        reddit="configured" if settings.reddit_configured else "unconfigured",
        gemini="configured" if settings.gemini_configured else "unconfigured",
    )
