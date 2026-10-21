"""Shopify connection controller: orchestration, plus the ONE place Shopify
domain errors become HTTP statuses."""

from __future__ import annotations

from collections.abc import Awaitable, MutableMapping
from typing import Any

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from exceptions.shopify import (
    ShopifyAPIError,
    ShopifyError,
    ShopifyInvalidShop,
    ShopifyNotConfigured,
    ShopifyNotConnected,
    ShopifyPermissionError,
    ShopifyReauthorizeRequired,
    ShopifyThrottled,
    ShopifyUnreachable,
)
from schemas import ShopifyConnectionOut, ShopifyProductsResponse, ShopifyStatusResponse
from services import shopify_service


def http_for(exc: ShopifyError) -> HTTPException:
    """Domain error -> HTTP. The messages are ours and never carry a token."""
    if isinstance(exc, ShopifyNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ShopifyInvalidShop):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ShopifyNotConnected):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ShopifyReauthorizeRequired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, ShopifyPermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ShopifyThrottled):
        headers = {"Retry-After": str(int(exc.retry_after_s or 2) + 1)}
        return HTTPException(status_code=429, detail=str(exc), headers=headers)
    if isinstance(exc, ShopifyUnreachable | ShopifyAPIError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail="Shopify request failed")


async def _call[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except ShopifyError as exc:
        raise http_for(exc) from exc


def app_home() -> RedirectResponse:
    return shopify_service.app_home()


async def install(session: MutableMapping[str, Any], shop: str | None, reconnect: bool) -> RedirectResponse:
    return await shopify_service.install(session, shop, reconnect=reconnect)


async def callback(session: MutableMapping[str, Any], query_params: Any) -> RedirectResponse:
    return await shopify_service.callback(session, query_params)


async def status(session: MutableMapping[str, Any]) -> ShopifyStatusResponse:
    return await _call(shopify_service.status(session))


async def check(session: MutableMapping[str, Any], connection_id: str) -> ShopifyConnectionOut:
    return await _call(shopify_service.check(session, connection_id))


async def products(
    session: MutableMapping[str, Any], connection_id: str, *, first: int, after: str | None, search: str | None
) -> ShopifyProductsResponse:
    return await _call(shopify_service.products(session, connection_id, first=first, after=after, search=search))


async def disconnect(session: MutableMapping[str, Any], connection_id: str) -> ShopifyStatusResponse:
    return await _call(shopify_service.disconnect(session, connection_id))
