"""Google OAuth endpoints: login redirect, state-validated callback, status, disconnect."""

from __future__ import annotations

import logging
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from config import get_settings
from models.schemas import ConnectionStatus
from services.integrations.google_oauth import (
    build_flow,
    delete_credentials,
    load_credentials,
    save_credentials,
)

logger = logging.getLogger("seo.google.auth")

router = APIRouter(prefix="/auth", tags=["google-auth"])


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Send the browser to Google's consent screen."""
    if "user_id" not in request.session:
        request.session["user_id"] = str(uuid4())
    flow = build_flow()
    # Deliberately NOT passing include_granted_scopes: this OAuth client may be
    # shared with other tools (Ads, Ad Manager, ...), and bundling previously
    # granted scopes into our token would over-privilege it.
    auth_url, state = flow.authorization_url(
        access_type="offline",  # ask for a refresh token
        prompt="consent",  # guarantees a refresh token even on repeat connects
    )
    # Validated in the callback - CSRF protection for the OAuth flow.
    request.session["oauth_state"] = state
    # PKCE: authorization_url() generated a code_verifier and sent its challenge
    # to Google; the callback needs the verifier for the token exchange.
    request.session["pkce_code_verifier"] = flow.code_verifier
    return RedirectResponse(auth_url)


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Handle Google's redirect: validate state, exchange the code, store tokens."""
    frontend = get_settings().frontend_url.rstrip("/")

    expected_state = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("pkce_code_verifier", None)
    returned_state = request.query_params.get("state")
    if not expected_state or not returned_state or expected_state != returned_state:
        raise HTTPException(
            status_code=400, detail="Invalid OAuth state (possible CSRF) - please retry the login"
        )

    error = request.query_params.get("error")
    if error:  # e.g. the user clicked "Cancel" on the consent screen
        # Google's value is interpolated into a URL, so it is encoded, not pasted:
        # an unescaped "&" or space would silently rewrite the frontend's query.
        return RedirectResponse(f"{frontend}/?{urlencode({'connected': '0', 'reason': error})}")

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Session expired during login - please retry")

    flow = build_flow(state=expected_state, code_verifier=code_verifier)
    try:
        await run_in_threadpool(flow.fetch_token, code=code)
    except Exception as exc:  # noqa: BLE001 - never let oauthlib internals reach the client
        # Surface only the safe parts: the exception type plus the RFC 6749 error
        # code/description (e.g. invalid_client). Never the raw exception string,
        # which can echo request parameters.
        error_code = getattr(exc, "error", None)
        description = getattr(exc, "description", None)
        logger.warning(
            "Token exchange with Google failed: %s error=%s description=%s",
            type(exc).__name__,
            error_code,
            description,
        )
        hint = {
            "invalid_client": "the GOOGLE_CLIENT_SECRET in app/server/.env is wrong or was rotated",
            "invalid_grant": "the authorization code was already used or expired - retry the login",
            "redirect_uri_mismatch": "GOOGLE_REDIRECT_URI does not match the Google Cloud Console entry",
        }.get(error_code or "", "verify the client secret and redirect URI")
        suffix = f" [{description}]" if description else ""
        raise HTTPException(
            status_code=400,
            detail=f"Token exchange with Google failed ({error_code or type(exc).__name__}) - {hint}{suffix}",
        ) from exc

    try:
        await save_credentials(user_id, flow.credentials)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google did not return a refresh token. Remove this app's access at "
                "myaccount.google.com/permissions, then connect again."
            ),
        ) from exc

    return RedirectResponse(f"{frontend}/?connected=1")


@router.get("/status", response_model=ConnectionStatus)
async def status(request: Request) -> ConnectionStatus:
    user_id = request.session.get("user_id")
    connected = bool(user_id) and (await load_credentials(user_id)) is not None
    return ConnectionStatus(connected=connected)


@router.post("/disconnect", response_model=ConnectionStatus)
async def disconnect(request: Request) -> ConnectionStatus:
    user_id = request.session.get("user_id")
    if user_id:
        await delete_credentials(user_id)
    return ConnectionStatus(connected=False)
