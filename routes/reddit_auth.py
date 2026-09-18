"""Reddit OAuth endpoints: login redirect, state-validated callback, status, disconnect.

Mirrors routes/google_auth.py, and deliberately reuses the SAME signed-cookie
session and the same `users` row, so one browser session can be connected to both
Google Search Console and Reddit at once.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from controller import reddit_auth as reddit_auth_controller
from schemas import RedditConnectionStatus

router = APIRouter(prefix="/auth/reddit", tags=["reddit-auth"])


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Send the browser to Reddit's consent screen."""
    return await reddit_auth_controller.login(request.session)


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Handle Reddit's redirect: validate state, exchange the code, store tokens."""
    return await reddit_auth_controller.callback(request.session, request.query_params)


@router.get("/status", response_model=RedditConnectionStatus)
async def status(request: Request) -> RedditConnectionStatus:
    return await reddit_auth_controller.status(request.session)


@router.post("/disconnect", response_model=RedditConnectionStatus)
async def disconnect(request: Request) -> RedditConnectionStatus:
    return await reddit_auth_controller.disconnect(request.session)
