"""Shopify OAuth, authorization code grant, for a standalone app.

Flow (https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps):

1. Send the merchant to `https://{shop}/admin/oauth/authorize` with our client
   id, scopes, redirect URI and a one-time `state` nonce.
2. Shopify redirects back with `code`, `hmac`, `shop`, `state`, `timestamp` and
   `host`. Every one of them is checked before the code is used: the shop is a
   real `*.myshopify.com` host, the HMAC is ours, the state is the one we issued.
3. The code is exchanged at `https://{shop}/admin/oauth/access_token` with
   `expiring=1`, which returns an EXPIRING offline token (60 minutes) and a
   refresh token (90 days, rotated on every refresh). New public apps must use
   these for the GraphQL Admin API, and non-expiring ones stop working for
   public apps on 2027-01-01, so there is no point building on the old kind.

Nothing here logs a token, the client secret or a raw response body.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from config import get_settings
from exceptions.shopify import (
    ShopifyAPIError,
    ShopifyInvalidShop,
    ShopifyNotConfigured,
    ShopifyOAuthError,
    ShopifyReauthorizeRequired,
    ShopifyUnreachable,
)
from integrations.shopify.crypto import encryption_ready

logger = logging.getLogger("seo.shopify.oauth")

#: Shopify's own check for a store domain. Anything else - a custom domain, an
#: IP, a host with a port - is never sent a request, so the code exchange can
#: not be pointed at an arbitrary server.
SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_ADMIN_STORE_PATH_RE = re.compile(r"^/store/([a-z0-9][a-z0-9-]*)(?:/|$)")
TIMEOUT_S = 30.0
#: Transient refresh failures are retried with the SAME refresh token, as Shopify
#: documents: a lost response must not strand the store.
REFRESH_ATTEMPTS = 3

_INVALID_SHOP_MESSAGE = (
    "Enter the store's myshopify.com address, for example your-store.myshopify.com. "
    "A custom domain such as www.example.com cannot be used to connect; the "
    "myshopify.com address is in Shopify admin under Settings > Domains."
)


def require_shopify_config() -> None:
    """The app boots with no Shopify keys; the Shopify endpoints gate on them."""
    if not get_settings().shopify_configured:
        raise ShopifyNotConfigured(
            "Shopify is not configured - set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET in server/.env"
        )
    if not encryption_ready():
        raise ShopifyNotConfigured(
            "Shopify tokens cannot be stored safely - set SHOPIFY_TOKEN_ENCRYPTION_KEY "
            "(or SESSION_SECRET) in server/.env"
        )


def is_valid_shop_domain(value: str | None) -> bool:
    return bool(value) and len(value) <= 255 and SHOP_DOMAIN_RE.fullmatch(value) is not None


def normalize_shop_domain(raw: str | None) -> str:
    """What a merchant types -> `<name>.myshopify.com`, or ShopifyInvalidShop.

    Accepts `your-store`, `your-store.myshopify.com`, a pasted
    `https://your-store.myshopify.com/admin` and the new admin URL
    `https://admin.shopify.com/store/your-store`.
    """
    value = (raw or "").strip().lower()
    if not value:
        raise ShopifyInvalidShop("Enter your store's myshopify.com address.")
    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.port is not None or parts.username or parts.password:
        raise ShopifyInvalidShop(_INVALID_SHOP_MESSAGE)
    if host == "admin.shopify.com":
        match = _ADMIN_STORE_PATH_RE.match(parts.path or "")
        host = f"{match.group(1)}.myshopify.com" if match else ""
    elif host and "." not in host:
        host = f"{host}.myshopify.com"
    if not is_valid_shop_domain(host):
        raise ShopifyInvalidShop(_INVALID_SHOP_MESSAGE)
    return host


def build_authorize_url(shop: str, state: str) -> str:
    """Shopify's permission prompt for `shop`. Offline access (the default)."""
    require_shopify_config()
    settings = get_settings()
    query = urlencode({
        "client_id": settings.shopify_client_id,
        "scope": ",".join(settings.shopify_scope_list),
        "redirect_uri": settings.shopify_redirect_uri,
        "state": state,
    })
    return f"https://{shop}/admin/oauth/authorize?{query}"


def verify_callback_hmac(params: Iterable[tuple[str, str]], secret: str) -> bool:
    """Shopify signs the callback query: every parameter except `hmac`, sorted,
    joined as `key=value&...`, HMAC-SHA256 with the client secret, hex encoded.
    Compared in constant time."""
    items = list(params)
    provided = next((value for key, value in items if key == "hmac"), "")
    if not provided or not secret:
        return False
    message = "&".join(
        f"{key}={value}" for key, value in sorted((k, v) for k, v in items if k not in ("hmac", "signature"))
    )
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest.encode(), provided.strip().lower().encode())


@dataclass(slots=True)
class TokenSet:
    """One token response, exchange or refresh."""

    access_token: str
    scopes: list[str] = field(default_factory=list)
    #: None for a non-expiring token.
    expires_at: datetime | None = None
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None


def _in(seconds: Any) -> datetime | None:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=value) if value > 0 else None


def _token_set(payload: dict[str, Any]) -> TokenSet:
    return TokenSet(
        access_token=str(payload["access_token"]),
        scopes=[s.strip() for s in str(payload.get("scope") or "").split(",") if s.strip()],
        expires_at=_in(payload.get("expires_in")),
        refresh_token=payload.get("refresh_token") or None,
        refresh_expires_at=_in(payload.get("refresh_token_expires_in")),
    )


def missing_scopes(requested: Iterable[str], granted: Iterable[str]) -> list[str]:
    """Requested scopes the token does not carry. A `write_x` grant includes
    `read_x`, and Shopify may return only the write scope, so that counts."""
    have = set(granted)
    return [
        scope for scope in requested
        if scope not in have and not (scope.startswith("read_") and f"write_{scope[5:]}" in have)
    ]


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, headers={"Accept": "application/json"})


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def exchange_code(shop: str, code: str, *, client: httpx.AsyncClient | None = None) -> TokenSet:
    """Authorization code -> expiring offline access token + refresh token."""
    require_shopify_config()
    settings = get_settings()
    form = {
        "client_id": settings.shopify_client_id,
        "client_secret": settings.shopify_client_secret,
        "code": code,
        "expiring": "1",
    }
    try:
        if client is None:
            async with _http_client() as owned:
                response = await owned.post(f"https://{shop}/admin/oauth/access_token", data=form)
        else:
            response = await client.post(f"https://{shop}/admin/oauth/access_token", data=form)
    except httpx.HTTPError as exc:
        raise ShopifyUnreachable(f"Could not reach {shop}: {type(exc).__name__}") from exc

    payload = _json(response)
    if response.status_code >= 400 or not payload.get("access_token"):
        error = str(payload.get("error") or "")
        logger.warning("Shopify code exchange for %s failed: HTTP %s %s", shop, response.status_code, error)
        if response.status_code >= 500:
            raise ShopifyAPIError(f"Shopify answered HTTP {response.status_code} to the code exchange",
                                  status_code=response.status_code)
        raise ShopifyOAuthError(
            "token_exchange_failed",
            "Shopify did not accept the authorization code. It may have been used already or "
            "expired, or SHOPIFY_CLIENT_SECRET does not match the app. Try connecting again.",
        )
    return _token_set(payload)


async def refresh_tokens(
    shop: str,
    refresh_token: str,
    *,
    client: httpx.AsyncClient | None = None,
    sleep: Any = asyncio.sleep,
) -> TokenSet:
    """Refresh token -> a NEW access token and a NEW refresh token. Store both.

    A 400/401 is final: Shopify answers the same way for an expired, replaced,
    unknown or revoked refresh token and for an uninstalled app, so it is never
    retried and always means "reconnect". Network errors, 429 and 5xx are
    retried with the same refresh token, which stays valid until the new one is
    first used.
    """
    require_shopify_config()
    settings = get_settings()
    form = {
        "client_id": settings.shopify_client_id,
        "client_secret": settings.shopify_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    url = f"https://{shop}/admin/oauth/access_token"
    last_error: Exception | None = None
    for attempt in range(REFRESH_ATTEMPTS):
        if attempt:
            await sleep(2 ** (attempt - 1))
        try:
            if client is None:
                async with _http_client() as owned:
                    response = await owned.post(url, data=form)
            else:
                response = await client.post(url, data=form)
        except httpx.HTTPError as exc:
            last_error = ShopifyUnreachable(f"Could not reach {shop} to refresh its token: {type(exc).__name__}")
            continue
        if response.status_code in (400, 401, 403):
            logger.warning("Shopify refused to refresh the token for %s: HTTP %s", shop, response.status_code)
            raise ShopifyReauthorizeRequired(
                "Shopify no longer accepts this store's refresh token: it expired, the app was "
                "uninstalled, or access was revoked. Reconnect the store."
            )
        if response.status_code == 429 or response.status_code >= 500:
            last_error = ShopifyAPIError(
                f"Shopify answered HTTP {response.status_code} to a token refresh",
                status_code=response.status_code,
            )
            continue
        payload = _json(response)
        if not payload.get("access_token"):
            last_error = ShopifyAPIError("Shopify returned a token refresh without an access token")
            continue
        return _token_set(payload)
    assert last_error is not None
    raise last_error
