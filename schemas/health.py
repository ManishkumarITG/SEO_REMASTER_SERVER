"""Health endpoint schema.

Split out of the former `models/schemas.py` during the layered-architecture
reorganisation. Field names, types, defaults and validators are copied verbatim,
so the request/response contract is byte-identical.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    mongodb: Literal["connected", "unavailable"] = "unavailable"
    reddit: Literal["configured", "unconfigured"] = "unconfigured"
    gemini: Literal["configured", "unconfigured"] = "unconfigured"
