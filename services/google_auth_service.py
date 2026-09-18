"""Google OAuth: login redirect, state-validated callback, status, disconnect.

Logic moved verbatim from api/routes/google_auth.py. The route passes the
session mapping (`request.session`) and the query params in, so this layer needs
no Request object; everything else - state/PKCE handling, the error-code hint
table, the exact HTTPException details - is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from config import get_settings
from integrations.google.oauth import (
    build_flow,
    delete_credentials,
    load_credentials,
    save_credentials,
)
from schemas import ConnectionStatus

logger = logging.getLogger("seo.google.auth")


async def login(session: MutableMapping[str, Any]) -> RedirectResponse:
    """Send the browser to Google's consent screen."""
    if "user_id" not in session:
        session["user_id"] = str(uuid4())
    flow = build_flow()
    # Deliberately NOT passing include_granted_scopes: this OAuth client may be
    # shared with other tools (Ads, Ad Manager, ...), and bundling previously
    # granted scopes into our token would over-privilege it.
    auth_url, state = flow.authorization_url(
        access_type="offline",  # ask for a refresh token
        prompt="consent",  # guarantees a refresh token even on repeat connects
    )
    # Validated in the callback - CSRF protection for the OAuth flow.
    session["oauth_state"] = state
    # PKCE: authorization_url() generated a code_verifier and sent its challenge
    # to Google; the callback needs the verifier for the token exchange.
    session["pkce_code_verifier"] = flow.code_verifier
    return RedirectResponse(auth_url)


async def callback(
    session: MutableMapping[str, Any], query_params: Mapping[str, str]
) -> RedirectResponse:
    """Handle Google's redirect: validate state, exchange the code, store tokens."""
    frontend = get_settings().frontend_url.rstrip("/")

    expected_state = session.pop("oauth_state", None)
    code_verifier = session.pop("pkce_code_verifier", None)
    returned_state = query_params.get("state")
    if not expected_state or not returned_state or expected_state != returned_state:
        raise HTTPException(
            status_code=400, detail="Invalid OAuth state (possible CSRF) - please retry the login"
        )

    error = query_params.get("error")
    if error:  # e.g. the user clicked "Cancel" on the consent screen
        # Google's value is interpolated into a URL, so it is encoded, not pasted:
        # an unescaped "&" or space would silently rewrite the frontend's query.
        return RedirectResponse(f"{frontend}/?{urlencode({'connected': '0', 'reason': error})}")

    code = query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    user_id = session.get("user_id")
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


async def status(session: MutableMapping[str, Any]) -> ConnectionStatus:
    user_id = session.get("user_id")
    connected = bool(user_id) and (await load_credentials(user_id)) is not None
    return ConnectionStatus(connected=connected)


async def disconnect(session: MutableMapping[str, Any]) -> ConnectionStatus:
    user_id = session.get("user_id")
    if user_id:
        await delete_credentials(user_id)
    return ConnectionStatus(connected=False)
