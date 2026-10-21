"""Shopify store connection: OAuth install and callback, status, the reusable
Admin GraphQL entry point, products, check, disconnect.

Same session model as Google and Reddit: the signed session cookie carries a
`user_id`, and every connection belongs to exactly one. One session may connect
several stores (an agency works on many); each store appears once per session.

Using a connection from anywhere in the backend:

    data = await execute_admin_graphql(user_id, connection_id, QUERY, {"first": 10})

That one call loads the connection, refreshes the 60-minute access token when
it is about to lapse (one refresh per connection at a time), runs the query,
and on a 401 refreshes once and asks again before declaring the connection
revoked. Tokens never leave this module and `integrations/shopify/`.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastapi.responses import RedirectResponse

from config import get_settings
from exceptions.shopify import (
    ShopifyAPIError,
    ShopifyError,
    ShopifyInvalidShop,
    ShopifyNotConfigured,
    ShopifyNotConnected,
    ShopifyOAuthError,
    ShopifyReauthorizeRequired,
    ShopifyUnreachable,
)
from integrations.shopify import credentials
from integrations.shopify.admin_graphql import AdminGraphQLClient, ShopifyTokenRejected
from integrations.shopify.credentials import ShopInfo, aware
from integrations.shopify.oauth import (
    build_authorize_url,
    exchange_code,
    is_valid_shop_domain,
    missing_scopes,
    normalize_shop_domain,
    refresh_tokens,
    require_shopify_config,
    verify_callback_hmac,
)
from models.db import ShopifyConnection
from schemas import ShopifyConnectionOut, ShopifyProduct, ShopifyProductsResponse, ShopifyStatusResponse

logger = logging.getLogger("seo.shopify")

_SESSION_KEY = "shopify_oauth"
#: The merchant has this long between clicking Connect and approving.
STATE_TTL_S = 15 * 60
#: Refresh this long before the access token lapses, so no request races it.
REFRESH_SKEW = timedelta(minutes=5)
MAX_PRODUCTS_PAGE = 100

SHOP_QUERY = """
query ShopInfo {
  shop {
    name
    myshopifyDomain
    primaryDomain { host }
    plan { displayName }
    currencyCode
  }
}
"""

PRODUCTS_QUERY = """
query Products($first: Int!, $after: String, $query: String) {
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT, reverse: true) {
    edges {
      node {
        id
        title
        handle
        status
        vendor
        productType
        updatedAt
        onlineStoreUrl
        variantsCount { count }
        priceRangeV2 { minVariantPrice { amount currencyCode } }
        featuredMedia { preview { image { url altText } } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

#: One refresh per connection at a time. Shopify rotates the refresh token on
#: every use, so two concurrent refreshes would leave one holding a token the
#: other already replaced.
_refresh_locks: dict[str, asyncio.Lock] = {}


# --- redirects back to the frontend -------------------------------------------------


def _frontend(**params: str) -> RedirectResponse:
    """Back to the Shopify section. Values are encoded, never pasted into the URL;
    the destination is always FRONTEND_URL, never anything from the request."""
    frontend = get_settings().frontend_url.rstrip("/")
    return RedirectResponse(f"{frontend}/?{urlencode({'tab': 'shopify', **params})}", status_code=302)


def _fail(reason: str, **extra: str) -> RedirectResponse:
    return _frontend(shopify="error", reason=reason, **extra)


def app_home() -> RedirectResponse:
    """Where the App URL lands: the frontend's Shopify section, nothing else."""
    return _frontend()


# --- OAuth ------------------------------------------------------------------------------


async def install(session: MutableMapping[str, Any], raw_shop: str | None, *, reconnect: bool) -> RedirectResponse:
    """Connect (or reconnect) a store: validate it, then send the merchant to Shopify.

    A store this session already has a working connection to is not sent
    through OAuth again unless `reconnect` is asked for.
    """
    try:
        require_shopify_config()
        shop = normalize_shop_domain(raw_shop)
    except ShopifyNotConfigured:
        return _fail("not_configured")
    except ShopifyInvalidShop:
        return _fail("invalid_shop", shop=(raw_shop or "")[:255])

    if "user_id" not in session:
        session["user_id"] = str(uuid4())
    if not reconnect:
        existing = await credentials.find_connection(session["user_id"], shop)
        if existing is not None and existing.status == credentials.ACTIVE:
            return _frontend(shopify="already_connected", shop=shop, connection=existing.id)

    state = secrets.token_urlsafe(32)
    # One pending authorization per session, checked and consumed by the callback.
    session[_SESSION_KEY] = {"state": state, "shop": shop, "issued_at": time.time()}
    return RedirectResponse(build_authorize_url(shop, state), status_code=302)


async def callback(session: MutableMapping[str, Any], query_params: Any) -> RedirectResponse:
    """Shopify's redirect after the merchant approves. Every check passes or the
    code is never used: shop format, HMAC, state, shop match, state age."""
    pending = session.pop(_SESSION_KEY, None)  # single use, whatever happens next
    params: list[tuple[str, str]] = list(query_params.multi_items())
    query: Mapping[str, str] = dict(params)
    shop = (query.get("shop") or "").strip().lower()
    try:
        require_shopify_config()
        if not is_valid_shop_domain(shop):
            raise ShopifyOAuthError("invalid_shop", "The callback named a shop that is not a myshopify.com store")
        if not verify_callback_hmac(params, get_settings().shopify_client_secret):
            raise ShopifyOAuthError("invalid_hmac", "The callback's signature did not verify")
        returned_state = query.get("state") or ""
        if not isinstance(pending, dict) or not hmac.compare_digest(
            str(pending.get("state", "")).encode(), returned_state.encode()
        ):
            raise ShopifyOAuthError("invalid_state", "The callback's state did not match this session")
        if pending.get("shop") != shop:
            raise ShopifyOAuthError("shop_mismatch", "The callback came from a different store")
        if time.time() - float(pending.get("issued_at", 0)) > STATE_TTL_S:
            raise ShopifyOAuthError("state_expired", "The authorization took too long")
        if query.get("error"):
            raise ShopifyOAuthError("denied", "The authorization was not approved")
        code = query.get("code")
        if not code:
            raise ShopifyOAuthError("missing_code", "Shopify did not send an authorization code")
        user_id = session.get("user_id")
        if not user_id:
            raise ShopifyOAuthError("session_expired", "The session ended during the authorization")

        tokens = await exchange_code(shop, code)
        missing = missing_scopes(get_settings().shopify_scope_list, tokens.scopes)
        if missing:
            # The token is not kept: a connection that cannot do its job would
            # only fail later, further from the cause.
            return _fail("missing_scopes", shop=shop, missing=",".join(missing))

        info: ShopInfo | None = None
        try:
            client = AdminGraphQLClient(shop, tokens.access_token, api_version=get_settings().shopify_api_version)
            info = _shop_info(await client.execute(SHOP_QUERY))
        except ShopifyError as exc:  # the connection is still good; the name can come later
            logger.warning("Connected %s but could not read its details: %s", shop, type(exc).__name__)
        row = await credentials.save_connection(user_id, shop, tokens, info)
    except ShopifyOAuthError as exc:
        logger.warning("Shopify callback rejected (%s) for %s", exc.reason, shop or "?")
        return _fail(exc.reason, shop=shop if is_valid_shop_domain(shop) else "")
    except ShopifyNotConfigured:
        return _fail("not_configured")
    except ShopifyUnreachable:
        return _fail("unreachable", shop=shop)
    except ShopifyAPIError:
        return _fail("shopify_error", shop=shop)

    logger.info("Shopify store %s connected (scopes: %s)", shop, ",".join(tokens.scopes))
    return _frontend(shopify="connected", shop=shop, connection=row.id)


def _shop_info(data: dict[str, Any]) -> ShopInfo:
    shop = data.get("shop") or {}
    return ShopInfo(
        name=str(shop.get("name") or ""),
        primary_domain=str((shop.get("primaryDomain") or {}).get("host") or ""),
        plan_name=str((shop.get("plan") or {}).get("displayName") or ""),
        currency_code=str(shop.get("currencyCode") or ""),
    )


# --- tokens and the reusable GraphQL entry point ---------------------------------------


def _expiring(row: ShopifyConnection) -> bool:
    expires_at = aware(row.access_token_expires_at)
    return expires_at is not None and expires_at - REFRESH_SKEW <= datetime.now(UTC)


async def _mark(row: ShopifyConnection, status: str, reason: str) -> None:
    await credentials.set_status(row.id, status, reason)
    row.status, row.status_reason = status, reason


async def _access_token(row: ShopifyConnection, *, rejected_enc: str | None = None) -> str:
    """A usable access token, refreshing first when it is about to lapse or when
    Shopify just rejected `rejected_enc`."""
    if rejected_enc is None and not _expiring(row):
        return credentials.access_token(row)
    if not row.refresh_token_enc:
        if rejected_enc is None:
            return credentials.access_token(row)  # a non-expiring token: nothing to refresh
        reason = ("Shopify rejected the access token and there is no refresh token to renew it. "
                  "Reconnect the store.")
        await _mark(row, credentials.REVOKED, reason)
        raise ShopifyReauthorizeRequired(reason)

    lock = _refresh_locks.setdefault(row.id, asyncio.Lock())
    async with lock:
        latest = await credentials.load_connection(row.id)
        if latest is None:
            raise ShopifyNotConnected("That Shopify connection no longer exists")
        stale = rejected_enc or row.access_token_enc
        if latest.access_token_enc != stale and not _expiring(latest):
            # Another request refreshed while this one waited for the lock.
            _copy_tokens(latest, row)
            return credentials.access_token(latest)
        refresh_expiry = aware(latest.refresh_token_expires_at)
        if refresh_expiry is not None and refresh_expiry <= datetime.now(UTC):
            reason = "The connection was unused for 90 days and its refresh token expired. Reconnect the store."
            await _mark(row, credentials.REAUTHORIZE_REQUIRED, reason)
            raise ShopifyReauthorizeRequired(reason)
        try:
            tokens = await refresh_tokens(latest.shop_domain, credentials.refresh_token(latest) or "")
        except ShopifyReauthorizeRequired as exc:
            await _mark(row, credentials.REAUTHORIZE_REQUIRED, str(exc))
            raise
        except (ShopifyUnreachable, ShopifyAPIError):
            expires_at = aware(latest.access_token_expires_at)
            if rejected_enc is None and expires_at is not None and expires_at > datetime.now(UTC):
                # Refresh failed for a passing reason, but the current token still
                # has minutes left: use it, and refresh on the next request.
                return credentials.access_token(latest)
            raise
        updated = await credentials.store_refreshed_tokens(row.id, tokens)
        if updated is not None:
            _copy_tokens(updated, row)
        logger.info("Refreshed the Shopify access token for %s", row.shop_domain)
        return tokens.access_token


def _copy_tokens(source: ShopifyConnection, target: ShopifyConnection) -> None:
    target.access_token_enc = source.access_token_enc
    target.refresh_token_enc = source.refresh_token_enc
    target.access_token_expires_at = source.access_token_expires_at
    target.refresh_token_expires_at = source.refresh_token_expires_at


async def execute_for_connection(
    row: ShopifyConnection, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run one Admin GraphQL operation as this connection. -> `data`."""
    require_shopify_config()
    if row.status != credentials.ACTIVE:
        raise ShopifyReauthorizeRequired(row.status_reason or "This store must be reconnected.")
    try:
        return await _execute(row, query, variables)
    except ShopifyReauthorizeRequired as exc:
        if row.status == credentials.ACTIVE:
            # Not yet recorded (an unreadable token after a key change, say):
            # record it, so the UI shows Reconnect instead of a dead Active.
            await _mark(row, credentials.REAUTHORIZE_REQUIRED, str(exc))
        raise


async def _execute(row: ShopifyConnection, query: str, variables: dict[str, Any] | None) -> dict[str, Any]:
    version = get_settings().shopify_api_version
    token = await _access_token(row)
    try:
        return await AdminGraphQLClient(row.shop_domain, token, api_version=version).execute(query, variables)
    except ShopifyTokenRejected:
        rejected = row.access_token_enc
    # A 401 on a token we believed valid: it may have been retired early. One
    # refresh and one more try, then the connection is declared revoked.
    token = await _access_token(row, rejected_enc=rejected)
    try:
        return await AdminGraphQLClient(row.shop_domain, token, api_version=version).execute(query, variables)
    except ShopifyTokenRejected as exc:
        reason = ("Shopify rejected a freshly issued access token: the app was uninstalled from "
                  "the store or its access was revoked. Reconnect the store.")
        await _mark(row, credentials.REVOKED, reason)
        raise ShopifyReauthorizeRequired(reason) from exc


async def execute_admin_graphql(
    user_id: str | None, connection_id: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The reusable entry point: GraphQL Admin API as one of this user's stores."""
    row = await credentials.get_connection(user_id, connection_id)
    return await execute_for_connection(row, query, variables)


# --- endpoints ----------------------------------------------------------------------------


def _out(row: ShopifyConnection) -> ShopifyConnectionOut:
    granted = [s for s in (row.scopes or "").split(",") if s]
    return ShopifyConnectionOut(
        id=row.id,
        shop_domain=row.shop_domain,
        shop_name=row.shop_name,
        primary_domain=row.primary_domain,
        plan_name=row.plan_name,
        currency_code=row.currency_code,
        status=row.status if row.status in ("active", "reauthorize_required", "revoked") else "revoked",
        status_reason=row.status_reason,
        scopes=granted,
        missing_scopes=missing_scopes(get_settings().shopify_scope_list, granted),
        access_token_expires_at=aware(row.access_token_expires_at),
        refresh_token_expires_at=aware(row.refresh_token_expires_at),
        connected_at=aware(row.connected_at) or datetime.now(UTC),
        last_checked_at=aware(row.last_checked_at),
        last_refreshed_at=aware(row.last_refreshed_at),
    )


async def status(session: MutableMapping[str, Any]) -> ShopifyStatusResponse:
    settings = get_settings()
    configuration_error = None
    try:
        require_shopify_config()
    except ShopifyNotConfigured as exc:
        configuration_error = str(exc)
    user_id = session.get("user_id")
    rows = await credentials.list_connections(user_id) if user_id else []
    return ShopifyStatusResponse(
        configured=configuration_error is None,
        configuration_error=configuration_error,
        requested_scopes=settings.shopify_scope_list,
        api_version=settings.shopify_api_version,
        connections=[_out(row) for row in rows],
    )


async def check(session: MutableMapping[str, Any], connection_id: str) -> ShopifyConnectionOut:
    """Prove the connection works now: read the shop through the API.

    A connection that turns out to need reconnecting is an answer, not an
    error, so it comes back with its new status rather than as a failure.
    """
    row = await credentials.get_connection(session.get("user_id"), connection_id)
    if row.status != credentials.ACTIVE:
        # A previous failure may have been transient on our side: try once more.
        row.status = credentials.ACTIVE
    try:
        data = await execute_for_connection(row, SHOP_QUERY)
    except ShopifyReauthorizeRequired:
        return _out(await credentials.get_connection(session.get("user_id"), connection_id))
    updated = await credentials.record_check(row.id, _shop_info(data))
    return _out(updated or row)


async def products(
    session: MutableMapping[str, Any],
    connection_id: str,
    *,
    first: int = 25,
    after: str | None = None,
    search: str | None = None,
) -> ShopifyProductsResponse:
    row = await credentials.get_connection(session.get("user_id"), connection_id)
    variables = {
        "first": max(1, min(first, MAX_PRODUCTS_PAGE)),
        "after": after or None,
        "query": (search or "").strip() or None,
    }
    data = await execute_for_connection(row, PRODUCTS_QUERY, variables)
    connection = data.get("products") or {}
    items: list[ShopifyProduct] = []
    for edge in connection.get("edges") or []:
        node = (edge or {}).get("node") or {}
        price = ((node.get("priceRangeV2") or {}).get("minVariantPrice") or {})
        image = (((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {})
        items.append(ShopifyProduct(
            id=str(node.get("id") or ""),
            title=str(node.get("title") or ""),
            handle=str(node.get("handle") or ""),
            status=str(node.get("status") or ""),
            vendor=str(node.get("vendor") or ""),
            product_type=str(node.get("productType") or ""),
            updated_at=node.get("updatedAt"),
            online_store_url=node.get("onlineStoreUrl"),
            image_url=image.get("url"),
            image_alt=image.get("altText"),
            min_price=price.get("amount"),
            currency_code=price.get("currencyCode"),
            variants_count=(node.get("variantsCount") or {}).get("count"),
        ))
    page = connection.get("pageInfo") or {}
    return ShopifyProductsResponse(
        connection_id=row.id,
        shop_domain=row.shop_domain,
        products=items,
        has_next_page=bool(page.get("hasNextPage")),
        end_cursor=page.get("endCursor"),
    )


async def disconnect(session: MutableMapping[str, Any], connection_id: str) -> ShopifyStatusResponse:
    """Forget the store: both tokens are deleted from this app.

    The app is NOT uninstalled from the store - that is irreversible and removes
    its configuration there, which is the merchant's call to make in Shopify
    admin. Without the refresh token, the last access token lapses within an hour.
    """
    if not await credentials.delete_connection(session.get("user_id"), connection_id):
        raise ShopifyNotConnected("That Shopify connection does not exist in this session")
    return await status(session)


__all__ = [
    "app_home",
    "PRODUCTS_QUERY",
    "SHOP_QUERY",
    "callback",
    "check",
    "disconnect",
    "execute_admin_graphql",
    "execute_for_connection",
    "install",
    "products",
    "status",
]
