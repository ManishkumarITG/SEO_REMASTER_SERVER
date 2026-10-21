"""Shopify connection schemas. None of them has a field for a token, by design."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ShopifyConnectionState = Literal["active", "reauthorize_required", "revoked"]


class ShopifyConnectionOut(BaseModel):
    id: str
    shop_domain: str
    shop_name: str | None = None
    primary_domain: str | None = None
    plan_name: str | None = None
    currency_code: str | None = None
    status: ShopifyConnectionState
    #: Why it needs attention, in words a merchant can act on.
    status_reason: str | None = None
    scopes: list[str] = Field(default_factory=list)
    #: Scopes this app now asks for that the stored grant lacks. Non-empty means
    #: reconnect to grant them (the scopes setting changed after connecting).
    missing_scopes: list[str] = Field(default_factory=list)
    #: The access token renews itself; this is when the current one lapses.
    access_token_expires_at: datetime | None = None
    #: The connection needs a reconnect by this date if nothing used it before then.
    refresh_token_expires_at: datetime | None = None
    connected_at: datetime
    last_checked_at: datetime | None = None
    last_refreshed_at: datetime | None = None


class ShopifyStatusResponse(BaseModel):
    #: False when SHOPIFY_CLIENT_ID / SECRET or an encryption key are unset.
    configured: bool
    #: Why it is not configured, when it is not.
    configuration_error: str | None = None
    requested_scopes: list[str] = Field(default_factory=list)
    api_version: str
    connections: list[ShopifyConnectionOut] = Field(default_factory=list)


class ShopifyProduct(BaseModel):
    id: str
    title: str
    handle: str
    status: str
    vendor: str = ""
    product_type: str = ""
    updated_at: datetime | None = None
    online_store_url: str | None = None
    image_url: str | None = None
    image_alt: str | None = None
    min_price: str | None = None
    currency_code: str | None = None
    variants_count: int | None = None


class ShopifyProductsResponse(BaseModel):
    connection_id: str
    shop_domain: str
    products: list[ShopifyProduct] = Field(default_factory=list)
    has_next_page: bool = False
    #: Pass back as `after` for the next page.
    end_cursor: str | None = None
