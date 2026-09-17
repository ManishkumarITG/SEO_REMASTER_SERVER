"""Google Search Console OAuth + API access.

Every Google OAuth/API concern lives here so routes stay thin:
- flow construction (client config comes from .env via Settings - never hardcoded)
- token persistence in the google_credentials table (keyed by users.id, so the
  anonymous session ids can later be promoted to real accounts without moving data)
- a safe execute helper: automatic access-token refresh, refreshed-token
  persistence, and error translation

Tokens are never logged and never returned by any endpoint. googleapiclient is
synchronous, so its calls run in the request threadpool.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from config import get_settings
from db.session import async_session_factory, ensure_user
from models.db import GoogleCredential

logger = logging.getLogger("seo.google")

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def require_google_config() -> None:
    """The app must boot with zero keys set; OAuth endpoints gate on config instead."""
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured - set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in app/server/.env"
            ),
        )


def _configure_oauthlib() -> None:
    settings = get_settings()
    # oauthlib refuses plain-http redirect URIs unless explicitly allowed;
    # local development runs on http://localhost, so relax it for that case only.
    if settings.google_redirect_uri.startswith("http://"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    # Google may return a slightly different scope list than requested;
    # without this, oauthlib treats that as a hard error.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


# Configure oauthlib at import time so the flags exist in EVERY worker process
# before any request runs. Setting them lazily (e.g. only during /auth/login)
# breaks when the dev server reloads between the login redirect and the callback:
# the fresh process would then reject Google's widened scope list with a Warning.
_configure_oauthlib()


def build_flow(state: str | None = None, code_verifier: str | None = None) -> Any:
    """Build the OAuth flow.

    google-auth-oauthlib enables PKCE automatically: authorization_url() generates
    a code_verifier and sends its challenge to Google. Because login and callback
    run on DIFFERENT flow instances (and possibly different processes), the login
    endpoint must persist flow.code_verifier in the session and the callback must
    pass it back in here - otherwise the token exchange fails with
    "invalid_grant: Missing code verifier".
    """
    from google_auth_oauthlib.flow import Flow  # lazy import keeps startup key-free

    require_google_config()
    _configure_oauthlib()
    settings = get_settings()
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config, scopes=GOOGLE_SCOPES, state=state, code_verifier=code_verifier
    )
    flow.redirect_uri = settings.google_redirect_uri
    return flow


async def save_credentials(user_id: str, creds: Any) -> None:
    """Upsert tokens. If Google did not return a new refresh token, keep the stored one."""
    async with async_session_factory() as session:
        await ensure_user(session, user_id)
        row = await session.get(GoogleCredential, user_id)
        refresh_token = creds.refresh_token or (row.refresh_token if row is not None else None)
        if not refresh_token:
            raise ValueError("No refresh token available to store")
        if row is None:
            row = GoogleCredential(user_id=user_id, refresh_token=refresh_token)
            session.add(row)
        row.refresh_token = refresh_token
        row.access_token = creds.token
        row.token_expiry = creds.expiry  # google-auth uses naive UTC datetimes
        row.scopes = " ".join(creds.scopes or [])
        row.updated_at = datetime.now(UTC)
        await session.commit()


async def load_credentials(user_id: str) -> Any | None:
    """Rebuild google.oauth2 Credentials from storage.

    google-auth refreshes the access token automatically on use when it is
    missing or expired, because refresh_token + client id/secret + token_uri
    are all attached.
    """
    from google.oauth2.credentials import Credentials

    async with async_session_factory() as session:
        row = await session.get(GoogleCredential, user_id)
    if row is None:
        return None
    settings = get_settings()
    expiry = row.token_expiry
    if expiry is not None and expiry.tzinfo is not None:
        expiry = expiry.astimezone(UTC).replace(tzinfo=None)  # google-auth expects naive UTC
    return Credentials(
        token=row.access_token,
        refresh_token=row.refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=row.scopes.split(),
        expiry=expiry,
    )


async def delete_credentials(user_id: str) -> None:
    async with async_session_factory() as session:
        row = await session.get(GoogleCredential, user_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def get_search_console_service(user_id: str) -> tuple[Any, Any]:
    from googleapiclient.discovery import build

    creds = await load_credentials(user_id)
    if creds is None:
        raise HTTPException(status_code=401, detail="Google Search Console is not connected")
    service = await run_in_threadpool(
        build, "searchconsole", "v1", credentials=creds, cache_discovery=False
    )
    return service, creds


async def execute_google_call(user_id: str, creds: Any, api_request: Any) -> dict[str, Any]:
    """Run a googleapiclient request safely.

    The (possibly refreshed) access token is persisted afterwards, and errors
    are translated to clean HTTP responses - token values never leak into
    responses or logs.
    """
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError

    previous_token = creds.token
    try:
        result = await run_in_threadpool(api_request.execute)
    except RefreshError as exc:
        # Refresh token invalid or revoked: the stored credential is useless now.
        await delete_credentials(user_id)
        logger.warning(
            "Google token refresh failed (%s) - stored credential removed", type(exc).__name__
        )
        raise HTTPException(
            status_code=401, detail="Google access expired or was revoked - please reconnect"
        ) from exc
    except HttpError as exc:
        status = getattr(exc.resp, "status", None) or 502
        # _get_reason() is googleapiclient-private; if a future version drops it,
        # an AttributeError here would turn a handled Google error into a 500.
        try:
            reason = exc._get_reason() or "Google API request failed"
        except Exception:  # noqa: BLE001
            reason = "Google API request failed"
        logger.warning("Google API error %s: %s", status, reason)
        raise HTTPException(
            status_code=status if 400 <= status < 500 else 502,
            detail=f"Google API error: {reason}",
        ) from exc

    if creds.token and creds.token != previous_token:
        await save_credentials(user_id, creds)
    return result
