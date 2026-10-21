"""Shopify store connection: OAuth checks, encrypted storage, refresh, GraphQL errors.

No network: Shopify is mocked with respx. Rows live in the real dev SQLite, each
test on its own `t<hex>.myshopify.com` store, removed again afterwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import delete
from starlette.datastructures import QueryParams

import main
from config import get_settings
from db.session import async_session_factory, init_db
from exceptions.shopify import (
    ShopifyAPIError,
    ShopifyInvalidShop,
    ShopifyPermissionError,
    ShopifyReauthorizeRequired,
    ShopifyThrottled,
)
from integrations.shopify import credentials
from integrations.shopify.admin_graphql import AdminGraphQLClient
from integrations.shopify.crypto import decrypt_token, encrypt_token
from integrations.shopify.oauth import (
    TokenSet,
    missing_scopes,
    normalize_shop_domain,
    verify_callback_hmac,
)
from models.db import ShopifyConnection
from services import shopify_service

SECRET = "test-client-secret"
VERSION = "2026-10"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "shopify_client_id", "test-client-id")
    monkeypatch.setattr(settings, "shopify_client_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_scopes", "read_products")
    monkeypatch.setattr(settings, "shopify_api_version", VERSION)
    monkeypatch.setattr(settings, "shopify_token_encryption_key", "test-encryption-key-not-for-production")
    monkeypatch.setattr(settings, "frontend_url", "http://localhost:5173")


@pytest.fixture
async def shop() -> str:
    await init_db()
    domain = f"t{uuid4().hex[:10]}.myshopify.com"
    yield domain
    async with async_session_factory() as session:
        await session.execute(delete(ShopifyConnection).where(ShopifyConnection.shop_domain == domain))
        await session.commit()


def _signed(params: dict[str, str]) -> dict[str, str]:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "hmac": hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()}


def _token_payload(access: str = "shpat_first", refresh: str = "shprt_first", scope: str = "read_products") -> dict:
    return {"access_token": access, "scope": scope, "expires_in": 3600,
            "refresh_token": refresh, "refresh_token_expires_in": 7_776_000}


def _graphql(shop: str) -> respx.Route:
    return respx.post(f"https://{shop}/admin/api/{VERSION}/graphql.json")


def _shop_data(name: str = "Ember & Oak") -> dict:
    return {"data": {"shop": {"name": name, "myshopifyDomain": "x", "primaryDomain": {"host": "emberoak.com"},
                              "plan": {"displayName": "Basic"}, "currencyCode": "EUR"}}}


def _location(response: httpx.Response) -> tuple[str, dict[str, str]]:
    parts = urlsplit(response.headers["location"])
    return f"{parts.scheme}://{parts.netloc}{parts.path}", {k: v[0] for k, v in parse_qs(parts.query).items()}


async def _connect(user_id: str, shop: str, *, expires_in_s: int = 3600, refresh: str | None = "shprt_first",
                   access: str = "shpat_first") -> ShopifyConnection:
    tokens = TokenSet(
        access_token=access, scopes=["read_products"],
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_s),
        refresh_token=refresh, refresh_expires_at=datetime.now(UTC) + timedelta(days=90) if refresh else None,
    )
    return await credentials.save_connection(user_id, shop, tokens, None)


# --- pure checks -----------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "ember-oak", "Ember-Oak.myshopify.com", " https://ember-oak.myshopify.com/admin/products ",
    "admin.shopify.com/store/ember-oak/products", "https://admin.shopify.com/store/ember-oak",
])
def test_what_a_merchant_types_becomes_the_myshopify_domain(raw: str) -> None:
    assert normalize_shop_domain(raw) == "ember-oak.myshopify.com"


@pytest.mark.parametrize("raw", [
    "", "www.emberoak.com", "ember-oak.myshopify.com:8443", "127.0.0.1", "-bad.myshopify.com",
    "evil.com/ember-oak.myshopify.com", "user@ember-oak.myshopify.com", "ember_oak.myshopify.com",
    "ember-oak.myshopify.com.evil.com", "https://admin.shopify.com/settings",
])
def test_anything_else_is_refused_before_a_request_is_made(raw: str) -> None:
    with pytest.raises(ShopifyInvalidShop):
        normalize_shop_domain(raw)


def test_the_callback_hmac_is_shopifys_scheme_and_constant_time() -> None:
    params = _signed({"code": "abc", "shop": "a.myshopify.com", "state": "s", "timestamp": "1700000000",
                      "host": "YWRtaW4uc2hvcGlmeS5jb20vc3RvcmUvYQ"})
    assert verify_callback_hmac(params.items(), SECRET)
    assert not verify_callback_hmac({**params, "code": "tampered"}.items(), SECRET)
    assert not verify_callback_hmac(params.items(), "another-secret")
    assert not verify_callback_hmac({k: v for k, v in params.items() if k != "hmac"}.items(), SECRET)
    assert not verify_callback_hmac({**params, "hmac": "zz"}.items(), SECRET)


def test_a_write_scope_satisfies_its_read_scope() -> None:
    assert missing_scopes(["read_products", "read_orders"], ["write_products"]) == ["read_orders"]
    assert missing_scopes(["write_products"], ["read_products"]) == ["write_products"]


def test_tokens_are_encrypted_at_rest_and_a_key_change_asks_for_a_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    ciphertext = encrypt_token("shpat_secret")
    assert "shpat_secret" not in ciphertext
    assert decrypt_token(ciphertext) == "shpat_secret"
    monkeypatch.setattr(get_settings(), "shopify_token_encryption_key", "a-different-key")
    with pytest.raises(ShopifyReauthorizeRequired):
        decrypt_token(ciphertext)


# --- install --------------------------------------------------------------------------------


def test_connect_sends_the_merchant_to_shopify_with_a_state(shop: str) -> None:
    response = TestClient(main.app).get("/auth/shopify/install", params={"shop": shop}, follow_redirects=False)
    assert response.status_code == 302
    url, query = _location(response)
    assert url == f"https://{shop}/admin/oauth/authorize"
    assert query["client_id"] == "test-client-id"
    assert query["scope"] == "read_products"
    assert query["redirect_uri"] == get_settings().shopify_redirect_uri
    assert len(query["state"]) >= 32


def test_the_app_url_forwards_to_the_shopify_section_and_ignores_its_query() -> None:
    response = TestClient(main.app).get(
        "/auth/shopify/app", params={"shop": "a.myshopify.com", "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:5173/?tab=shopify"


def test_an_invalid_domain_returns_to_the_app_with_a_reason() -> None:
    response = TestClient(main.app).get("/auth/shopify/install", params={"shop": "www.emberoak.com"},
                                        follow_redirects=False)
    url, query = _location(response)
    assert url == "http://localhost:5173/"
    assert query["tab"] == "shopify" and query["shopify"] == "error" and query["reason"] == "invalid_shop"


def test_an_unconfigured_app_says_so_instead_of_redirecting_to_shopify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "shopify_client_secret", "")
    response = TestClient(main.app).get("/auth/shopify/install", params={"shop": "a.myshopify.com"},
                                        follow_redirects=False)
    assert _location(response)[1]["reason"] == "not_configured"
    status = TestClient(main.app).get("/api/shopify/status").json()
    assert status["configured"] is False and "SHOPIFY_CLIENT_SECRET" in status["configuration_error"]


# --- the whole flow through HTTP ---------------------------------------------------------------


@respx.mock
def test_install_callback_status_products_disconnect(shop: str) -> None:
    respx.post(f"https://{shop}/admin/oauth/access_token").mock(return_value=httpx.Response(200, json=_token_payload()))
    seen_tokens: list[str] = []

    def graphql(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["X-Shopify-Access-Token"])
        body = json.loads(request.content)
        if "ShopInfo" in body["query"]:
            return httpx.Response(200, json=_shop_data())
        assert body["variables"] == {"first": 10, "after": None, "query": "title:tee"}
        return httpx.Response(200, json={"data": {"products": {
            "edges": [{"node": {
                "id": "gid://shopify/Product/1", "title": "Linen Tee", "handle": "linen-tee", "status": "ACTIVE",
                "vendor": "Ember & Oak", "productType": "Shirts", "updatedAt": "2026-10-01T10:00:00Z",
                "onlineStoreUrl": f"https://{shop}/products/linen-tee", "variantsCount": {"count": 3},
                "priceRangeV2": {"minVariantPrice": {"amount": "18.0", "currencyCode": "EUR"}},
                "featuredMedia": {"preview": {"image": {"url": "https://cdn/x.jpg", "altText": None}}},
            }}],
            "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        }}})

    _graphql(shop).mock(side_effect=graphql)
    client = TestClient(main.app)

    install = client.get("/auth/shopify/install", params={"shop": shop}, follow_redirects=False)
    state = _location(install)[1]["state"]
    callback = client.get("/auth/shopify/callback", follow_redirects=False, params=_signed({
        "code": "auth-code", "shop": shop, "state": state, "timestamp": "1700000000", "host": "aG9zdA",
    }))
    url, query = _location(callback)
    assert url == "http://localhost:5173/" and query["shopify"] == "connected" and query["shop"] == shop
    connection_id = query["connection"]

    status = client.get("/api/shopify/status")
    body = status.json()
    [conn] = [c for c in body["connections"] if c["id"] == connection_id]
    assert conn["shop_name"] == "Ember & Oak" and conn["primary_domain"] == "emberoak.com"
    assert conn["status"] == "active" and conn["scopes"] == ["read_products"] and conn["missing_scopes"] == []
    assert "shpat_first" not in status.text and "shprt_first" not in status.text  # never to the browser

    # Already connected: no second trip through OAuth.
    again = client.get("/auth/shopify/install", params={"shop": shop}, follow_redirects=False)
    assert _location(again)[1]["shopify"] == "already_connected"
    assert urlsplit(client.get("/auth/shopify/install", params={"shop": shop, "reconnect": "true"},
                               follow_redirects=False).headers["location"]).netloc == shop

    products = client.get(f"/api/shopify/connections/{connection_id}/products", params={"first": 10, "q": "title:tee"})
    assert products.status_code == 200
    item = products.json()["products"][0]
    assert item["title"] == "Linen Tee" and item["min_price"] == "18.0" and item["variants_count"] == 3
    assert products.json()["has_next_page"] is True and products.json()["end_cursor"] == "c1"
    assert set(seen_tokens) == {"shpat_first"}

    # Another browser session cannot see or use this connection.
    stranger = TestClient(main.app)
    assert stranger.get(f"/api/shopify/connections/{connection_id}/products").status_code == 404
    assert stranger.post(f"/api/shopify/connections/{connection_id}/disconnect").status_code == 404

    gone = client.post(f"/api/shopify/connections/{connection_id}/disconnect")
    assert gone.status_code == 200
    assert connection_id not in [c["id"] for c in gone.json()["connections"]]


# --- the callback's checks ------------------------------------------------------------------------


def _pending(domain: str, **overrides: object) -> dict:
    pending = {"state": "the-state", "shop": domain, "issued_at": time.time(), **overrides}
    return {"user_id": str(uuid4()), "shopify_oauth": pending}


def _callback_params(shop: str, **overrides: str) -> QueryParams:
    params = {"code": "auth-code", "shop": shop, "state": "the-state", "timestamp": "1700000000", **overrides}
    return QueryParams(_signed(params))


@pytest.mark.parametrize(("session_override", "param_override", "reason"), [
    ({}, {"state": "forged"}, "invalid_state"),
    ({"shop": "other.myshopify.com"}, {}, "shop_mismatch"),
    ({"issued_at": 0}, {}, "state_expired"),
])
async def test_a_callback_that_fails_a_check_never_uses_the_code(
    shop: str, session_override: dict, param_override: dict, reason: str
) -> None:
    session = _pending(shop, **session_override)
    with respx.mock:
        exchange = respx.post(f"https://{shop}/admin/oauth/access_token")
        response = await shopify_service.callback(session, _callback_params(shop, **param_override))
        assert not exchange.called
    assert _location(response)[1]["reason"] == reason
    assert "shopify_oauth" not in session  # the state is single use


async def test_a_tampered_callback_fails_the_hmac(shop: str) -> None:
    params = dict(_callback_params(shop))
    params["code"] = "swapped"
    with respx.mock:
        exchange = respx.post(f"https://{shop}/admin/oauth/access_token")
        response = await shopify_service.callback(_pending(shop), QueryParams(params))
        assert not exchange.called
    assert _location(response)[1]["reason"] == "invalid_hmac"


async def test_a_grant_missing_a_scope_is_not_stored(shop: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "shopify_scopes", "read_products,read_orders")
    session = _pending(shop)
    with respx.mock:
        respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(200, json=_token_payload(scope="read_products")))
        response = await shopify_service.callback(session, _callback_params(shop))
    query = _location(response)[1]
    assert query["reason"] == "missing_scopes" and query["missing"] == "read_orders"
    assert await credentials.find_connection(session["user_id"], shop) is None


async def test_a_refused_code_exchange_returns_with_a_reason(shop: str) -> None:
    with respx.mock:
        respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_request"}))
        response = await shopify_service.callback(_pending(shop), _callback_params(shop))
    assert _location(response)[1]["reason"] == "token_exchange_failed"


async def test_tokens_are_stored_encrypted(shop: str) -> None:
    session = _pending(shop)
    with respx.mock:
        respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(200, json=_token_payload()))
        _graphql(shop).mock(return_value=httpx.Response(200, json=_shop_data()))
        await shopify_service.callback(session, _callback_params(shop))
    row = await credentials.find_connection(session["user_id"], shop)
    assert row is not None
    assert "shpat_first" not in row.access_token_enc and "shprt_first" not in (row.refresh_token_enc or "")
    assert credentials.access_token(row) == "shpat_first" and credentials.refresh_token(row) == "shprt_first"
    assert row.access_token_expires_at is not None and row.refresh_token_expires_at is not None


# --- refresh and revocation ---------------------------------------------------------------------


async def test_an_expiring_token_is_refreshed_before_the_request(shop: str) -> None:
    row = await _connect(str(uuid4()), shop, expires_in_s=60)  # inside the 5-minute skew
    with respx.mock:
        refresh = respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(200, json=_token_payload("shpat_second", "shprt_second")))
        api = _graphql(shop).mock(return_value=httpx.Response(200, json=_shop_data()))
        await shopify_service.execute_admin_graphql(row.user_id, row.id, shopify_service.SHOP_QUERY)
    form = parse_qs(refresh.calls.last.request.content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["shprt_first"]
    assert api.calls.last.request.headers["X-Shopify-Access-Token"] == "shpat_second"
    stored = await credentials.load_connection(row.id)
    assert credentials.access_token(stored) == "shpat_second"
    assert credentials.refresh_token(stored) == "shprt_second"  # rotated: the next refresh uses the new one


async def test_concurrent_requests_refresh_once(shop: str) -> None:
    row = await _connect(str(uuid4()), shop, expires_in_s=60)
    with respx.mock:
        refresh = respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(200, json=_token_payload("shpat_second", "shprt_second")))
        _graphql(shop).mock(return_value=httpx.Response(200, json=_shop_data()))
        await asyncio.gather(*(
            shopify_service.execute_admin_graphql(row.user_id, row.id, shopify_service.SHOP_QUERY) for _ in range(4)
        ))
    assert refresh.call_count == 1


async def test_a_401_refreshes_once_and_asks_again(shop: str) -> None:
    row = await _connect(str(uuid4()), shop)
    with respx.mock:
        respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(200, json=_token_payload("shpat_second", "shprt_second")))
        api = _graphql(shop).mock(side_effect=[httpx.Response(401), httpx.Response(200, json=_shop_data())])
        data = await shopify_service.execute_admin_graphql(row.user_id, row.id, shopify_service.SHOP_QUERY)
    assert data["shop"]["name"] == "Ember & Oak"
    assert [c.request.headers["X-Shopify-Access-Token"] for c in api.calls] == ["shpat_first", "shpat_second"]


async def test_a_fresh_token_rejected_too_means_revoked(shop: str) -> None:
    row = await _connect(str(uuid4()), shop)
    with respx.mock:
        respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(200, json=_token_payload("shpat_second", "shprt_second")))
        _graphql(shop).mock(return_value=httpx.Response(401))
        with pytest.raises(ShopifyReauthorizeRequired):
            await shopify_service.execute_admin_graphql(row.user_id, row.id, shopify_service.SHOP_QUERY)
    stored = await credentials.load_connection(row.id)
    assert stored.status == "revoked" and "uninstalled" in stored.status_reason


async def test_a_refused_refresh_token_asks_for_a_reconnect_and_is_never_retried(shop: str) -> None:
    row = await _connect(str(uuid4()), shop, expires_in_s=-10)
    with respx.mock:
        refresh = respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(401, json={"error": "invalid_request"}))
        api = _graphql(shop)
        with pytest.raises(ShopifyReauthorizeRequired):
            await shopify_service.execute_admin_graphql(row.user_id, row.id, shopify_service.SHOP_QUERY)
        assert refresh.call_count == 1 and not api.called
    assert (await credentials.load_connection(row.id)).status == "reauthorize_required"


def test_a_connection_needing_a_reconnect_answers_401_and_check_reports_it(shop: str) -> None:
    client = TestClient(main.app)
    # Bind a connection to this TestClient's session via the real callback.
    with respx.mock:
        respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(200, json=_token_payload()))
        _graphql(shop).mock(return_value=httpx.Response(200, json=_shop_data()))
        state = _location(client.get("/auth/shopify/install", params={"shop": shop}, follow_redirects=False))[1]["state"]
        callback = client.get("/auth/shopify/callback", follow_redirects=False,
                              params=_signed({"code": "c", "shop": shop, "state": state, "timestamp": "1"}))
    connection_id = _location(callback)[1]["connection"]
    asyncio.run(credentials.set_status(connection_id, "revoked", "The app was uninstalled."))

    blocked = client.get(f"/api/shopify/connections/{connection_id}/products")
    assert blocked.status_code == 401 and "uninstalled" in blocked.json()["detail"]

    with respx.mock:
        _graphql(shop).mock(return_value=httpx.Response(401))
        respx.post(f"https://{shop}/admin/oauth/access_token").mock(
            return_value=httpx.Response(401, json={"error": "invalid_request"}))
        checked = client.post(f"/api/shopify/connections/{connection_id}/check")
    assert checked.status_code == 200 and checked.json()["status"] in ("revoked", "reauthorize_required")


# --- the GraphQL client -------------------------------------------------------------------------


async def test_throttled_queries_wait_for_the_bucket_then_succeed() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                 "extensions": {"cost": {"requestedQueryCost": 52, "throttleStatus": {
                     "maximumAvailable": 2000, "currentlyAvailable": 12, "restoreRate": 100}}}}
    with respx.mock:
        respx.post(f"https://a.myshopify.com/admin/api/{VERSION}/graphql.json").mock(
            side_effect=[httpx.Response(200, json=throttled), httpx.Response(429, headers={"Retry-After": "3"}),
                         httpx.Response(200, json=_shop_data())])
        data = await AdminGraphQLClient("a.myshopify.com", "t", api_version=VERSION, sleep=sleep).execute("{ shop { name } }")
    assert data["shop"]["name"] == "Ember & Oak"
    assert waits == [0.5, 3.0]  # (52 - 12) / 100 = 0.4 s, floored at 0.5; then Retry-After


async def test_a_bucket_that_never_refills_gives_up_as_throttled() -> None:
    async def sleep(seconds: float) -> None:
        return None

    with respx.mock:
        respx.post(f"https://a.myshopify.com/admin/api/{VERSION}/graphql.json").mock(return_value=httpx.Response(429))
        with pytest.raises(ShopifyThrottled):
            await AdminGraphQLClient("a.myshopify.com", "t", api_version=VERSION, sleep=sleep).execute("{ shop { name } }")


async def test_a_missing_permission_is_a_permission_error_not_a_retry() -> None:
    denied = {"errors": [{"message": "Access denied for products field. Required access: `read_products`.",
                          "extensions": {"code": "ACCESS_DENIED"}}]}
    with respx.mock:
        route = respx.post(f"https://a.myshopify.com/admin/api/{VERSION}/graphql.json").mock(
            return_value=httpx.Response(200, json=denied))
        with pytest.raises(ShopifyPermissionError, match="read_products"):
            await AdminGraphQLClient("a.myshopify.com", "t", api_version=VERSION).execute("{ products(first: 1) { edges { node { id } } } }")
    assert route.call_count == 1


@pytest.mark.parametrize(("status", "code"), [(402, "payment_required"), (404, "not_found"), (423, "locked")])
async def test_store_level_refusals_are_named(status: int, code: str) -> None:
    with respx.mock:
        respx.post(f"https://a.myshopify.com/admin/api/{VERSION}/graphql.json").mock(return_value=httpx.Response(status))
        with pytest.raises(ShopifyAPIError) as caught:
            await AdminGraphQLClient("a.myshopify.com", "t", api_version=VERSION).execute("{ shop { name } }")
    assert caught.value.code == code
