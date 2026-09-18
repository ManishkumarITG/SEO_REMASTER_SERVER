"""Health reporting.

Logic moved verbatim from api/routes/health.py. MongoDB is optional: the API
works without it, so report status instead of failing. Reddit / Gemini report
only whether keys are present - never which values.
"""

from __future__ import annotations

from config import get_settings
from db.mongo import ping as mongo_ping
from schemas import HealthResponse


async def get_health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        version="0.1.0",
        mongodb="connected" if await mongo_ping() else "unavailable",
        reddit="configured" if settings.reddit_configured else "unconfigured",
        gemini="configured" if settings.gemini_configured else "unconfigured",
    )
