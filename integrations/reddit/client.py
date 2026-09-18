"""Reddit OAuth + Data API access.

Every Reddit concern lives here so routes stay thin:
- OAuth (authorize URL, code exchange, refresh) against www.reddit.com
- the authenticated Data API against oauth.reddit.com
- token persistence in the reddit_credentials table, keyed by users.id - the SAME
  users row Google uses, so one browser session can be connected to both
- error translation into domain exceptions

Unlike google_oauth.py this module raises domain errors rather than HTTPException:
the route layer owns the HTTP mapping (see api/routes/backlinks.py::_http_for), because
Reddit's own error strings must reach the UI verbatim and must never be retried.

Tokens, the client secret and the Authorization header are never logged.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from config import get_settings
from db.session import async_session_factory, ensure_user
from exceptions.reddit import (
    RedditAPIError,
    RedditAuthError,
    RedditNotConfigured,
    RedditNotConnected,
    RedditRateLimited,
)
from models.db import RedditCredential
from utils.cache import get_cache

logger = logging.getLogger("seo.reddit")

AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE = "https://oauth.reddit.com"

_TIMEOUT_SECONDS = 20.0
#: Refresh a little early so a call never races the expiry.
_REFRESH_SKEW_SECONDS = 60
_RULES_CACHE_TTL = 3600

#: Rule text matching any of these gets the subreddit a self-promo warning in the UI.
_SELF_PROMO_MARKERS = (
    "self-promo",
    "self promo",
    "selfpromo",
    "advertis",
    "promotion",
    "promotional",
    "no spam",
    "spam",
    "soliciting",
    "referral",
    "affiliate",
    "shilling",
    "vendor",
    "no selling",
    "no sales",
)


def require_reddit_config() -> None:
    """The app must boot with zero keys set; Reddit endpoints gate on config instead."""
    if not get_settings().reddit_configured:
        raise RedditNotConfigured(
            "Reddit is not configured - set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET "
            "in app/server/.env"
        )


def _basic_auth_header() -> str:
    settings = get_settings()
    raw = f"{settings.reddit_client_id}:{settings.reddit_client_secret}".encode()
    return f"Basic {base64.b64encode(raw).decode()}"


def has_self_promo_rule(texts: list[str]) -> bool:
    """True when any rule text mentions self-promotion / advertising / spam.

    Pure function - unit-tested without network access.
    """
    haystack = " ".join(texts).lower()
    return any(marker in haystack for marker in _SELF_PROMO_MARKERS)


class RedditClient:
    """Thin async wrapper over httpx. The User-Agent goes on EVERY request."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        #: When set, a refreshed access token is persisted for this user.
        self._user_id = user_id
        #: Latest X-Ratelimit-* values seen, so routes can pass them to the UI.
        self.ratelimit: dict[str, float | None] = {"used": None, "remaining": None, "reset": None}

    # --- OAuth ------------------------------------------------------------

    @staticmethod
    def build_authorize_url(state: str) -> str:
        """Reddit's consent URL. duration=permanent is what yields a refresh token."""
        require_reddit_config()
        settings = get_settings()
        params = {
            "client_id": settings.reddit_client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": settings.reddit_redirect_uri,
            "duration": "permanent",
            "scope": " ".join(settings.reddit_scope_list),
        }
        return f"{AUTHORIZE_URL}?{httpx.QueryParams(params)}"

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        require_reddit_config()
        settings = get_settings()
        headers = {
            "Authorization": _basic_auth_header(),
            "User-Agent": settings.reddit_user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(TOKEN_URL, data=form, headers=headers)
        if response.status_code == 401:
            raise RedditAuthError(
                "Reddit rejected the client credentials - check REDDIT_CLIENT_ID and "
                "REDDIT_CLIENT_SECRET"
            )
        if response.status_code == 429:
            raise RedditRateLimited("Reddit rate-limited the token request", None)
        if response.status_code >= 400:
            raise RedditAPIError("token_request_failed", f"Reddit returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RedditAPIError("token_request_failed", "Reddit returned a non-JSON token response") from exc
        if "error" in payload:
            # e.g. invalid_grant when the code was reused or the redirect URI differs.
            raise RedditAuthError(f"Reddit token exchange failed: {payload['error']}")
        return payload

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Swap the authorization code for tokens. Returns the parsed token payload."""
        settings = get_settings()
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.reddit_redirect_uri,
            }
        )
        self._access_token = payload.get("access_token")
        self._refresh_token = payload.get("refresh_token") or self._refresh_token
        return payload

    async def refresh(self, refresh_token: str | None = None) -> dict[str, Any]:
        """Refresh the access token. Reddit does not rotate the refresh token here."""
        token = refresh_token or self._refresh_token
        if not token:
            raise RedditAuthError("No Reddit refresh token available - please reconnect")
        payload = await self._token_request({"grant_type": "refresh_token", "refresh_token": token})
        self._access_token = payload.get("access_token")
        if self._user_id and self._access_token:
            await update_access_token(
                self._user_id, self._access_token, _expiry_from(payload.get("expires_in"))
            )
        return payload

    # --- Authenticated API ------------------------------------------------

    def _capture_ratelimit(self, response: httpx.Response) -> None:
        for key, header in (
            ("used", "X-Ratelimit-Used"),
            ("remaining", "X-Ratelimit-Remaining"),
            ("reset", "X-Ratelimit-Reset"),
        ):
            value = response.headers.get(header)
            if value is None:
                continue
            try:
                self.ratelimit[key] = float(value)
            except ValueError:
                self.ratelimit[key] = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> Any:
        if not self._access_token:
            raise RedditNotConnected("Reddit is not connected for this session")
        settings = get_settings()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            # Reddit throttles generic user agents hard - this header is not optional.
            "User-Agent": settings.reddit_user_agent,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method, f"{OAUTH_BASE}{path}", params=params, data=data, headers=headers
            )
        self._capture_ratelimit(response)

        if response.status_code == 401 and not _retried:
            # The access token expired mid-flight: refresh once, then retry once.
            await self.refresh()
            return await self._request(method, path, params=params, data=data, _retried=True)
        if response.status_code == 401:
            raise RedditAuthError("Reddit access expired or was revoked - please reconnect")
        if response.status_code == 429:
            reset = self.ratelimit.get("reset")
            raise RedditRateLimited(
                "Reddit rate-limited this request (HTTP 429). Nothing was retried.", reset
            )
        if response.status_code == 403:
            raise RedditAPIError("forbidden", "Reddit refused this request (HTTP 403)")
        if response.status_code >= 400:
            raise RedditAPIError("http_error", f"Reddit returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise RedditAPIError("bad_response", "Reddit returned a non-JSON response") from exc

    async def get_me(self) -> dict[str, Any]:
        payload = await self._request("GET", "/api/v1/me")
        return {
            "name": payload.get("name", ""),
            "link_karma": payload.get("link_karma"),
            "comment_karma": payload.get("comment_karma"),
            "created_utc": payload.get("created_utc"),
        }

    async def get_my_subreddits(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(moderated, subscribed) - moderated first, subscribed excludes those."""
        moderated = await self._listing("/subreddits/mine/moderator", you_moderate=True)
        subscribed = await self._listing("/subreddits/mine/subscriber", you_moderate=False)
        moderated_names = {sub["name"].lower() for sub in moderated}
        subscribed = [sub for sub in subscribed if sub["name"].lower() not in moderated_names]
        return moderated, subscribed

    async def _listing(self, path: str, *, you_moderate: bool) -> list[dict[str, Any]]:
        payload = await self._request("GET", path, params={"limit": 100})
        children = ((payload or {}).get("data") or {}).get("children") or []
        subs = []
        for child in children:
            data = child.get("data") or {}
            name = data.get("display_name")
            if not name:
                continue
            subs.append(
                {
                    "name": name,
                    "title": data.get("title") or "",
                    "subscribers": data.get("subscribers") or 0,
                    "you_moderate": you_moderate,
                    "public_description": data.get("public_description") or "",
                    "submission_type": data.get("submission_type") or "any",
                }
            )
        subs.sort(key=lambda sub: -(sub["subscribers"] or 0))
        return subs

    async def get_subreddit_rules(self, name: str) -> dict[str, Any]:
        """Subreddit rules + public description, cached for an hour."""
        cache = get_cache()
        cache_key = f"reddit:rules:{name.lower()}"
        cached = await cache.get(cache_key)
        if cached is not None:
            return cached

        payload = await self._request("GET", f"/r/{name}/about/rules")
        rules = [
            {
                "short_name": rule.get("short_name") or "",
                "description": rule.get("description") or "",
                "kind": rule.get("kind") or "",
                "violation_reason": rule.get("violation_reason") or "",
            }
            for rule in (payload or {}).get("rules") or []
        ]
        site_rules = [str(rule) for rule in (payload or {}).get("site_rules") or []]
        about = await self._request("GET", f"/r/{name}/about")
        about_data = (about or {}).get("data") or {}
        result = {
            "rules": rules,
            "site_rules": site_rules,
            "public_description": about_data.get("public_description") or "",
            "title": about_data.get("title") or "",
            "submission_type": about_data.get("submission_type") or "any",
            "self_promo_warning": has_self_promo_rule(
                [f"{rule['short_name']} {rule['description']}" for rule in rules]
            ),
        }
        await cache.set(cache_key, result, _RULES_CACHE_TTL)
        return result

    async def get_link_flairs(self, name: str) -> list[dict[str, Any]]:
        """Link flairs for a subreddit. A 403 just means flair is not exposed - not an error."""
        try:
            payload = await self._request("GET", f"/r/{name}/api/link_flair_v2")
        except RedditAPIError as exc:
            if exc.code == "forbidden":
                return []
            raise
        if not isinstance(payload, list):
            return []
        return [
            {
                "id": flair.get("id") or "",
                "text": flair.get("text") or "",
                "text_editable": bool(flair.get("text_editable")),
            }
            for flair in payload
            if flair.get("id")
        ]

    async def submit_post(
        self,
        *,
        subreddit: str,
        title: str,
        kind: str = "self",
        text: str | None = None,
        url: str | None = None,
        flair_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish one post. Returns {id, name, url}; raises with Reddit's own message."""
        form: dict[str, Any] = {
            "api_type": "json",
            "sr": subreddit,
            "kind": kind,
            "title": title,
            "sendreplies": "true",
            "nsfw": "false",
            "spoiler": "false",
        }
        if kind == "link":
            form["url"] = url or ""
        else:
            form["text"] = text or ""
        if flair_id:
            form["flair_id"] = flair_id

        payload = await self._request("POST", "/api/submit", data=form)
        envelope = (payload or {}).get("json") or {}
        errors = envelope.get("errors") or []
        if errors:
            code, message = _first_error(errors)
            if code == "RATELIMIT":
                # Reddit's own wording ("try again in 9 minutes") is the most useful
                # thing we can show, so pass it through untouched and do not retry.
                raise RedditRateLimited(message, self.ratelimit.get("reset"))
            raise RedditAPIError(code, message)

        data = envelope.get("data") or {}
        return {
            "id": data.get("id") or "",
            "name": data.get("name") or "",
            "url": data.get("url") or "",
        }


def _first_error(errors: list[Any]) -> tuple[str, str]:
    """Reddit sends errors as [[CODE, message, field], ...]."""
    first = errors[0]
    if isinstance(first, (list, tuple)):
        code = str(first[0]) if len(first) > 0 else "UNKNOWN"
        message = str(first[1]) if len(first) > 1 else "Reddit rejected the post"
        return code, message
    return "UNKNOWN", str(first)


def _expiry_from(expires_in: Any) -> datetime | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds)


# --- Credential persistence (mirrors google_oauth.py) ----------------------


async def save_credentials(
    user_id: str,
    *,
    username: str,
    access_token: str | None,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: str,
    link_karma: int | None = None,
    comment_karma: int | None = None,
    account_created_at: datetime | None = None,
) -> None:
    """Upsert tokens. If Reddit did not return a refresh token, keep the stored one."""
    async with async_session_factory() as session:
        await ensure_user(session, user_id)
        row = await _select_credential(session, user_id)
        kept = refresh_token or (row.refresh_token if row is not None else None)
        if not kept:
            raise ValueError("No Reddit refresh token available to store")
        if row is None:
            row = RedditCredential(user_id=user_id, reddit_username=username, refresh_token=kept)
            session.add(row)
        row.reddit_username = username or row.reddit_username
        row.access_token = access_token
        row.refresh_token = kept
        row.token_expires_at = expires_at
        row.scopes = scopes
        if link_karma is not None:
            row.link_karma = link_karma
        if comment_karma is not None:
            row.comment_karma = comment_karma
        if account_created_at is not None:
            row.account_created_at = account_created_at
        row.updated_at = datetime.now(UTC)
        await session.commit()


async def update_access_token(user_id: str, access_token: str, expires_at: datetime | None) -> None:
    """Persist a token rotated mid-request (same idea as execute_google_call)."""
    async with async_session_factory() as session:
        row = await _select_credential(session, user_id)
        if row is None:
            return
        row.access_token = access_token
        row.token_expires_at = expires_at
        row.updated_at = datetime.now(UTC)
        await session.commit()


async def load_credentials(user_id: str) -> RedditCredential | None:
    async with async_session_factory() as session:
        return await _select_credential(session, user_id)


async def delete_credentials(user_id: str) -> None:
    async with async_session_factory() as session:
        row = await _select_credential(session, user_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def _select_credential(session: Any, user_id: str) -> RedditCredential | None:
    from sqlalchemy import select

    result = await session.execute(
        select(RedditCredential).where(RedditCredential.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_client_for_user(user_id: str) -> RedditClient:
    """Build a client for this user, refreshing the access token if it is about to expire."""
    require_reddit_config()
    row = await load_credentials(user_id)
    if row is None:
        raise RedditNotConnected("Reddit is not connected for this session")

    client = RedditClient(
        access_token=row.access_token, refresh_token=row.refresh_token, user_id=user_id
    )
    expires_at = row.token_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    needs_refresh = (
        not row.access_token
        or expires_at is None
        or expires_at - timedelta(seconds=_REFRESH_SKEW_SECONDS) <= datetime.now(UTC)
    )
    if needs_refresh:
        await client.refresh(row.refresh_token)
    return client
