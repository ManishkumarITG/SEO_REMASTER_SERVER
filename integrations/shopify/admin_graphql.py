"""A small client for the Shopify GraphQL Admin API.

One request shape (`POST /admin/api/{version}/graphql.json` with the
`X-Shopify-Access-Token` header) and one job: turn every way Shopify says no
into a domain error, and wait out the ones that are only "not yet".

Throttling. The Admin API is cost-based: every store gives an app a bucket of
query points that refills at `restoreRate` per second. A query that would
overdraw it comes back as HTTP 200 with a `THROTTLED` error and the bucket's
state in `extensions.cost.throttleStatus`, which says exactly how long to wait.
HTTP 429 and 5xx are retried too, with backoff. Nothing else is retried.

A 401 is not handled here: this client cannot refresh a token. It raises
`ShopifyTokenRejected`, and `services/shopify_service.py`, which can, decides
whether to refresh and ask again or to tell the merchant to reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from exceptions.shopify import (
    ShopifyAPIError,
    ShopifyError,
    ShopifyPermissionError,
    ShopifyThrottled,
    ShopifyUnreachable,
)

logger = logging.getLogger("seo.shopify.graphql")

TIMEOUT_S = 30.0
MAX_ATTEMPTS = 4
#: Never sleep longer than this for one throttle, whatever Shopify suggests.
MAX_WAIT_S = 20.0


class ShopifyTokenRejected(ShopifyError):
    """HTTP 401 from the Admin API: the token expired, was retired or was revoked."""


def _throttle_wait(payload: dict[str, Any]) -> float:
    """Seconds until the bucket holds enough points for the query that was refused."""
    cost = (payload.get("extensions") or {}).get("cost") or {}
    status = cost.get("throttleStatus") or {}
    try:
        needed = float(cost.get("requestedQueryCost") or 0) - float(status.get("currentlyAvailable") or 0)
        rate = float(status.get("restoreRate") or 0)
    except (TypeError, ValueError):
        return 1.0
    if rate <= 0:
        return 1.0
    return min(MAX_WAIT_S, max(0.5, needed / rate))


def _retry_after(response: httpx.Response, fallback: float) -> float:
    try:
        return min(MAX_WAIT_S, max(0.5, float(response.headers.get("retry-after", ""))))
    except ValueError:
        return fallback


def _error_codes(errors: list[Any]) -> list[str]:
    codes = []
    for error in errors:
        if isinstance(error, dict):
            code = (error.get("extensions") or {}).get("code")
            if code:
                codes.append(str(code))
    return codes


def _messages(errors: list[Any]) -> str:
    texts = [str(e.get("message")) for e in errors if isinstance(e, dict) and e.get("message")]
    return "; ".join(texts[:3]) or "Shopify returned a GraphQL error"


class AdminGraphQLClient:
    """GraphQL Admin API access for one store with one access token."""

    def __init__(
        self,
        shop: str,
        access_token: str,
        *,
        api_version: str,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.shop = shop
        self.url = f"https://{shop}/admin/api/{api_version}/graphql.json"
        self._token = access_token
        self._client = client
        self._sleep = sleep
        self._max_attempts = max_attempts
        #: `extensions.cost` of the last successful response, for diagnostics.
        self.last_cost: dict[str, Any] | None = None

    async def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one query or mutation. -> its `data`. Raises a ShopifyError subclass."""
        body = {"query": query, "variables": variables or {}}
        if self._client is not None:
            return await self._execute(self._client, body)
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            return await self._execute(client, body)

    async def _execute(self, client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "X-Shopify-Access-Token": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        failure: ShopifyError | None = None
        for attempt in range(self._max_attempts):
            if attempt and failure is not None:
                await self._sleep(getattr(failure, "retry_after_s", None) or min(MAX_WAIT_S, 2.0 ** (attempt - 1)))
            try:
                response = await client.post(self.url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                failure = ShopifyUnreachable(f"Could not reach {self.shop}: {type(exc).__name__}")
                continue

            status = response.status_code
            if status == 401:
                raise ShopifyTokenRejected(f"{self.shop} rejected the access token (HTTP 401)")
            if status == 402:
                raise ShopifyAPIError(
                    f"{self.shop} is frozen or has an unpaid Shopify bill (HTTP 402)",
                    status_code=402, code="payment_required",
                )
            if status == 403:
                raise ShopifyPermissionError(
                    f"{self.shop} refused the request (HTTP 403): the app is missing a permission it needs"
                )
            if status == 404:
                raise ShopifyAPIError(
                    f"{self.shop} was not found (HTTP 404); the store may be closed or renamed",
                    status_code=404, code="not_found",
                )
            if status == 423:
                raise ShopifyAPIError(f"{self.shop} is locked (HTTP 423)", status_code=423, code="locked")
            if status == 429:
                failure = ShopifyThrottled(
                    f"{self.shop} is rate limiting the app (HTTP 429)", _retry_after(response, 2.0)
                )
                continue
            if status >= 500:
                failure = ShopifyAPIError(f"{self.shop} answered HTTP {status}", status_code=status)
                continue
            if status >= 400:
                raise ShopifyAPIError(f"{self.shop} answered HTTP {status}", status_code=status)

            try:
                payload = response.json()
            except ValueError:
                failure = ShopifyAPIError(f"{self.shop} returned a response that is not JSON")
                continue
            if not isinstance(payload, dict):
                raise ShopifyAPIError(f"{self.shop} returned an unexpected response")

            errors = payload.get("errors")
            if errors:
                if isinstance(errors, str):  # e.g. {"errors": "[API] Invalid API key or access token"}
                    raise ShopifyAPIError(f"{self.shop}: {errors[:200]}", status_code=status)
                codes = _error_codes(errors if isinstance(errors, list) else [])
                if "THROTTLED" in codes:
                    failure = ShopifyThrottled(
                        f"{self.shop} is out of GraphQL query budget", _throttle_wait(payload)
                    )
                    continue
                if "ACCESS_DENIED" in codes:
                    raise ShopifyPermissionError(
                        f"The connection to {self.shop} lacks a permission this request needs: "
                        f"{_messages(errors)}. Reconnect the store to grant it."
                    )
                raise ShopifyAPIError(
                    f"{self.shop}: {_messages(errors)}", status_code=status, code=codes[0] if codes else "",
                )

            self.last_cost = (payload.get("extensions") or {}).get("cost")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ShopifyAPIError(f"{self.shop} returned no data")
            return data

        assert failure is not None
        logger.warning("Shopify GraphQL request to %s gave up after %d attempts: %s",
                       self.shop, self._max_attempts, failure)
        raise failure
