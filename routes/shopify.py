"""Shopify store connection endpoints.

Browser navigations (they answer with redirects, never JSON):
  GET  /auth/shopify/install?shop=&reconnect=   -> Shopify's permission prompt
  GET  /auth/shopify/callback                   -> back to the frontend's Shopify section

JSON, scoped to the session's own connections:
  GET  /api/shopify/status
  POST /api/shopify/connections/{id}/check
  GET  /api/shopify/connections/{id}/products
  POST /api/shopify/connections/{id}/disconnect

Same signed-cookie session as Google and Reddit. No endpoint returns a token,
and there is deliberately no generic GraphQL passthrough: the browser can only
ask for what these routes expose.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from controller import shopify as shopify_controller
from schemas import ShopifyConnectionOut, ShopifyProductsResponse, ShopifyStatusResponse

auth_router = APIRouter(prefix="/auth/shopify", tags=["shopify-auth"])
router = APIRouter(prefix="/api/shopify", tags=["shopify"])


@auth_router.get("/app")
async def app_home() -> RedirectResponse:
    """The App URL to configure on the Shopify app. Shopify sends a merchant here
    when they open the app from their admin, and requires it to share a host with
    the redirect URL, so it lives on the API. It only forwards to the frontend's
    Shopify section: nothing from the query string is read or passed on."""
    return shopify_controller.app_home()


@auth_router.get("/install")
async def install(
    request: Request,
    shop: str | None = Query(default=None, max_length=512),
    reconnect: bool = False,
) -> RedirectResponse:
    """Validate the store domain, then send the merchant to Shopify to approve."""
    return await shopify_controller.install(request.session, shop, reconnect)


@auth_router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    """Verify HMAC, state and shop, exchange the code, store the tokens encrypted."""
    return await shopify_controller.callback(request.session, request.query_params)


@router.get("/status", response_model=ShopifyStatusResponse)
async def status(request: Request) -> ShopifyStatusResponse:
    return await shopify_controller.status(request.session)


@router.post("/connections/{connection_id}/check", response_model=ShopifyConnectionOut)
async def check(request: Request, connection_id: str) -> ShopifyConnectionOut:
    """Call the Admin API now and report whether the connection works."""
    return await shopify_controller.check(request.session, connection_id)


@router.get("/connections/{connection_id}/products", response_model=ShopifyProductsResponse)
async def products(
    request: Request,
    connection_id: str,
    first: int = Query(default=25, ge=1, le=100),
    after: str | None = Query(default=None, max_length=512),
    q: str | None = Query(default=None, max_length=200, description="Shopify product search syntax"),
) -> ShopifyProductsResponse:
    return await shopify_controller.products(request.session, connection_id, first=first, after=after, search=q)


@router.post("/connections/{connection_id}/disconnect", response_model=ShopifyStatusResponse)
async def disconnect(request: Request, connection_id: str) -> ShopifyStatusResponse:
    """Delete the stored credentials for this store."""
    return await shopify_controller.disconnect(request.session, connection_id)
