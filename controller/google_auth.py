"""Google OAuth controller: orchestration only."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from fastapi.responses import RedirectResponse

from schemas import ConnectionStatus
from services import google_auth_service


async def login(session: MutableMapping[str, Any]) -> RedirectResponse:
    return await google_auth_service.login(session)


async def callback(
    session: MutableMapping[str, Any], query_params: Mapping[str, str]
) -> RedirectResponse:
    return await google_auth_service.callback(session, query_params)


async def status(session: MutableMapping[str, Any]) -> ConnectionStatus:
    return await google_auth_service.status(session)


async def disconnect(session: MutableMapping[str, Any]) -> ConnectionStatus:
    return await google_auth_service.disconnect(session)
