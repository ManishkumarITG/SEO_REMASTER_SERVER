"""Google OAuth endpoints: login redirect, state-validated callback, status, disconnect."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from controller import google_auth as google_auth_controller
from schemas import ConnectionStatus

router = APIRouter(prefix="/auth", tags=["google-auth"])


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Send the browser to Google's consent screen."""
    return await google_auth_controller.login(request.session)


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Handle Google's redirect: validate state, exchange the code, store tokens."""
    return await google_auth_controller.callback(request.session, request.query_params)


@router.get("/status", response_model=ConnectionStatus)
async def status(request: Request) -> ConnectionStatus:
    return await google_auth_controller.status(request.session)


@router.post("/disconnect", response_model=ConnectionStatus)
async def disconnect(request: Request) -> ConnectionStatus:
    return await google_auth_controller.disconnect(request.session)
