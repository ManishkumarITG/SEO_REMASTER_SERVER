"""Reddit OAuth: login redirect, state-validated callback, status, disconnect.

Logic moved verbatim from api/routes/reddit_auth.py. Mirrors
google_auth_service.py, and deliberately reuses the SAME signed-cookie session
and the same `users` row, so one browser session can be connected to both
Google Search Console and Reddit at once.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from config import get_settings
from exceptions.reddit import RedditError, RedditNotConfigured
from integrations.reddit.client import (
    RedditClient,
    delete_credentials,
    load_credentials,
    require_reddit_config,
    save_credentials,
)
from schemas import RedditConnectionStatus

logger = logging.getLogger("seo.reddit.auth")

_SESSION_STATE_KEY = "reddit_oauth_state"


def _frontend_redirect(**params: str) -> RedirectResponse:
    """Bounce back to the Backlink tab. Values are encoded, never interpolated raw.

    `reason` carries Reddit's own error string and `username` an account name, so
    an unescaped "&" or space would rewrite the frontend's query string.
    """
    frontend = get_settings().frontend_url.rstrip("/")
    query = urlencode({"tab": "backlink", **params})
    return RedirectResponse(f"{frontend}/?{query}")


async def login(session: MutableMapping[str, Any]) -> RedirectResponse:
    """Send the browser to Reddit's consent screen."""
    try:
        require_reddit_config()
    except RedditNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if "user_id" not in session:
        session["user_id"] = str(uuid4())
    state = uuid4().hex
    # Validated in the callback - CSRF protection for the OAuth flow.
    session[_SESSION_STATE_KEY] = state
    return RedirectResponse(RedditClient.build_authorize_url(state))


async def callback(
    session: MutableMapping[str, Any], query_params: Mapping[str, str]
) -> RedirectResponse:
    """Handle Reddit's redirect: validate state, exchange the code, store tokens."""
    expected_state = session.pop(_SESSION_STATE_KEY, None)
    returned_state = query_params.get("state")
    if not expected_state or not returned_state or expected_state != returned_state:
        raise HTTPException(
            status_code=400, detail="Invalid OAuth state (possible CSRF) - please retry the login"
        )

    error = query_params.get("error")
    if error:  # e.g. the user clicked "Decline" on the consent screen
        return _frontend_redirect(reddit="error", reason=error)

    code = query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    user_id = session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Session expired during login - please retry")

    client = RedditClient()
    try:
        payload = await client.exchange_code(code)
        identity = await client.get_me()
    except RedditError as exc:
        # Domain errors only - never the raw exception chain, which can echo parameters.
        logger.warning("Reddit connect failed: %s: %s", type(exc).__name__, exc)
        return _frontend_redirect(reddit="error", reason=type(exc).__name__)

    created_utc = identity.get("created_utc")
    account_created_at = (
        datetime.fromtimestamp(float(created_utc), tz=UTC)
        if isinstance(created_utc, (int, float))
        else None
    )
    try:
        await save_credentials(
            user_id,
            username=identity.get("name") or "",
            access_token=payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
            expires_at=_expires_at(payload.get("expires_in")),
            scopes=str(payload.get("scope") or ""),
            link_karma=identity.get("link_karma"),
            comment_karma=identity.get("comment_karma"),
            account_created_at=account_created_at,
        )
    except ValueError:
        return _frontend_redirect(reddit="error", reason="no_refresh_token")

    return _frontend_redirect(reddit="connected", username=identity.get("name") or "")


def _expires_at(expires_in: object) -> datetime | None:
    try:
        seconds = int(expires_in)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def status(session: MutableMapping[str, Any]) -> RedditConnectionStatus:
    configured = get_settings().reddit_configured
    user_id = session.get("user_id")
    row = await load_credentials(user_id) if user_id else None
    if row is None:
        return RedditConnectionStatus(configured=configured, connected=False)

    created = row.account_created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - created).days if created is not None else None
    return RedditConnectionStatus(
        configured=configured,
        connected=True,
        username=row.reddit_username,
        link_karma=row.link_karma,
        comment_karma=row.comment_karma,
        account_age_days=age_days,
    )


async def disconnect(session: MutableMapping[str, Any]) -> RedditConnectionStatus:
    user_id = session.get("user_id")
    if user_id:
        await delete_credentials(user_id)
    return RedditConnectionStatus(configured=get_settings().reddit_configured, connected=False)
