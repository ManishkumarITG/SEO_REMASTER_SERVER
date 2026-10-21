"""Shopify domain errors.

`integrations/shopify/` and `services/shopify_service.py` raise these, never
HTTPException: the HTTP mapping lives in one place, controller/shopify.py, so
the same failure answers the same status whichever endpoint hit it.

No message here ever carries a token or the client secret.
"""

from __future__ import annotations


class ShopifyError(Exception):
    """Base error for every Shopify failure."""


class ShopifyNotConfigured(ShopifyError):
    """SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET (or an encryption key) are unset. 503."""


class ShopifyInvalidShop(ShopifyError):
    """The domain is not a `<name>.myshopify.com` store domain. 422."""


class ShopifyOAuthError(ShopifyError):
    """The authorization callback failed a check or the code exchange failed.

    `reason` is a short fixed code the frontend can map to a message; it never
    carries Shopify's raw response body.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class ShopifyNotConnected(ShopifyError):
    """No connection with that id belongs to this session. 404."""


class ShopifyReauthorizeRequired(ShopifyError):
    """The stored credentials can no longer be used: the refresh token expired,
    the merchant uninstalled the app, or access was revoked. The merchant has to
    reconnect. 401."""


class ShopifyPermissionError(ShopifyError):
    """The token works but lacks a scope this request needs. 403."""


class ShopifyThrottled(ShopifyError):
    """Shopify kept answering THROTTLED / 429 after our retries. 429."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ShopifyUnreachable(ShopifyError):
    """The request never got an answer: DNS, connect, TLS or timeout. 502."""


class ShopifyAPIError(ShopifyError):
    """Any other Shopify-side failure: 5xx, a locked or frozen store, GraphQL errors. 502."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
