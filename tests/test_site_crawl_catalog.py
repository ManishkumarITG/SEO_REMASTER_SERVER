"""Catalog-first phase of the site crawl: paced Shopify JSON, findings, records.

No network (respx) and no real waiting: the rate limiter runs on a fake clock
whose sleep only advances time, so a 429's back-off costs nothing to test.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import respx

from services.crawler.http_client import crawl_client
from services.crawler.robots import RobotsRules
from services.crawler.site_crawl import SiteProbe
from services.crawler.throttle import AimdParams, LocalRateLimiter
from services.site_crawl_catalog import catalog_findings, collect_site_catalog
from services.stores.discovery import (
    MAX_JSON_THROTTLE_RETRIES,
    _paginate,
    collection_from_json,
    fetch_catalog_json,
    product_from_json,
)
from tests.factories import shopify_collection, shopify_product

ORIGIN = "https://shop.test"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _limiter() -> LocalRateLimiter:
    clock = FakeClock()
    return LocalRateLimiter(AimdParams(r_max=10.0), clock=clock, sleep=clock.sleep)


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _paged(key: str, pages: dict[str, list[dict]]):
    """A respx side effect serving `pages` by ?page=, empty past the end."""

    def serve(request: httpx.Request) -> httpx.Response:
        return _json({key: pages.get(request.url.params.get("page", "1"), [])})

    return serve


def _products(n: int, *, start: int = 0) -> list[dict]:
    return [shopify_product(f"p{i}", pid=i) for i in range(start, start + n)]


# --- the paced walk ---------------------------------------------------------------------


@respx.mock
async def test_a_429_mid_catalog_is_asked_again_instead_of_ending_the_catalog() -> None:
    """The catalog crawl used to stop at the first 429 and report a short store."""
    page1, page2 = _products(250), _products(1, start=250)
    refused_once: list[bool] = []

    def products(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "2" and not refused_once:
            refused_once.append(True)
            return httpx.Response(429)
        return _json({"products": {"1": page1, "2": page2}.get(page, [])})

    respx.get(f"{ORIGIN}/products.json").mock(side_effect=products)
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=_json({"collections": []}))
    limiter = _limiter()
    async with crawl_client() as client:
        result = await fetch_catalog_json(client, ORIGIN, RobotsRules.permissive(), pacer=limiter)

    assert len(result.products) == 251
    assert result.products_complete is True and result.collections_complete is True
    assert result.requests == 4  # page 1, page 2 refused, page 2 again, collections
    assert limiter.state.throttled == 1 and limiter.state.episodes == 1
    assert result.notes == []


@respx.mock
async def test_a_store_that_keeps_refusing_leaves_the_catalog_incomplete_and_says_so() -> None:
    page1 = _products(250)

    def products(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "1":
            return _json({"products": page1})
        return httpx.Response(429)

    respx.get(f"{ORIGIN}/products.json").mock(side_effect=products)
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=_json({"collections": []}))
    async with crawl_client() as client:
        result = await fetch_catalog_json(client, ORIGIN, RobotsRules.permissive(), pacer=_limiter())

    assert len(result.products) == 250
    assert result.products_complete is False  # 250 read is not "the store has 250"
    assert result.requests == 1 + (1 + MAX_JSON_THROTTLE_RETRIES) + 1
    assert any("failed at page 2" in n and "incomplete after 250 items" in n for n in result.notes)


@respx.mock
async def test_a_bot_wall_is_reported_as_blocked_and_the_next_endpoint_is_not_tried() -> None:
    respx.get(f"{ORIGIN}/products.json").mock(return_value=httpx.Response(403))
    collections = respx.get(f"{ORIGIN}/collections.json").mock(return_value=_json({"collections": []}))
    limiter = _limiter()
    async with crawl_client() as client:
        result = await fetch_catalog_json(client, ORIGIN, RobotsRules.permissive(), pacer=limiter)

    assert result.blocked is True
    assert result.products == [] and result.products_complete is False
    assert not collections.called
    assert result.requests == 1  # a refusal is not retried
    assert limiter.state.episodes == 1  # and it slows the page crawl that follows
    assert any("refused" in n for n in result.notes)


@respx.mock
async def test_robots_disallowing_products_json_is_honoured() -> None:
    products = respx.get(f"{ORIGIN}/products.json").mock(return_value=_json({"products": _products(3)}))
    respx.get(f"{ORIGIN}/collections.json").mock(
        side_effect=_paged("collections", {"1": [shopify_collection("shirts", cid=1)]})
    )
    robots = RobotsRules(fetched=True, disallow=["/products.json"])
    async with crawl_client() as client:
        result = await fetch_catalog_json(client, ORIGIN, robots, pacer=_limiter())

    assert not products.called
    assert result.products == [] and result.products_complete is False
    assert [c.handle for c in result.collections] == ["shirts"]
    assert any("robots.txt disallows /products.json" in n for n in result.notes)


@respx.mock
async def test_the_item_cap_stops_the_walk_and_the_catalog_is_not_complete() -> None:
    respx.get(f"{ORIGIN}/products.json").mock(
        side_effect=_paged("products", {"1": _products(250), "2": _products(250, start=250)})
    )
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=_json({"collections": []}))
    async with crawl_client() as client:
        result = await fetch_catalog_json(client, ORIGIN, RobotsRules.permissive(), pacer=_limiter(),
                                          max_products=300)

    assert len(result.products) == 300
    assert result.products_complete is False
    assert any("300-item cap" in n for n in result.notes)


@respx.mock
async def test_stop_while_reading_ends_the_walk_before_the_next_request() -> None:
    route = respx.get(f"{ORIGIN}/products.json").mock(side_effect=_paged("products", {"1": _products(250)}))
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=_json({"collections": []}))
    def should_stop() -> bool:
        return route.call_count >= 1

    async with crawl_client() as client:
        result = await fetch_catalog_json(client, ORIGIN, RobotsRules.permissive(), pacer=_limiter(),
                                          should_stop=should_stop)

    assert route.call_count == 1
    assert len(result.products) == 250 and result.products_complete is False
    assert any("stopped at page 2" in n for n in result.notes)


@respx.mock
async def test_the_unpaced_walk_the_store_crawl_uses_is_unchanged() -> None:
    """No pacer: a 429 ends the walk with what it has, exactly as before."""
    def products(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "1":
            return _json({"products": _products(250)})
        return httpx.Response(429)

    route = respx.get(f"{ORIGIN}/products.json").mock(side_effect=products)
    async with crawl_client() as client:
        rows, pages, notes, hit_cap, truncated = await _paginate(
            client, ORIGIN, "/products.json", "products", limit=250, max_items=10_000, max_pages=40,
        )
    assert len(rows) == 250 and pages == 1
    assert route.call_count == 2  # no retry without a pacer
    assert notes == [] and hit_cap is False and truncated is False


# --- findings -------------------------------------------------------------------------------


def test_findings_are_what_json_can_say_and_never_the_page_crawls_job() -> None:
    items = [
        product_from_json(ORIGIN, shopify_product("tee-a", pid=1, title="Linen Tee", alt="A tee")),
        product_from_json(ORIGIN, shopify_product("tee-b", pid=2, title="linen  tee", alt="Another")),
        product_from_json(ORIGIN, shopify_product("bare", pid=3, body="", alt=None)),
        collection_from_json(ORIGIN, shopify_collection("empty", cid=9, count=0, description="<p>x</p>")),
    ]
    findings = catalog_findings([i for i in items if i])

    assert findings[f"{ORIGIN}/products/tee-a"] == ["duplicate_name"]
    assert findings[f"{ORIGIN}/products/tee-b"] == ["duplicate_name"]  # case and spacing ignored
    assert findings[f"{ORIGIN}/products/bare"] == ["description_missing", "image_alt_missing"]
    assert findings[f"{ORIGIN}/collections/empty"] == ["collection_empty", "image_alt_missing"]
    every = {g for gaps in findings.values() for g in gaps}
    assert not every & {"not_enriched", "page_unreachable", "skipped_by_robots", "meta_title_missing"}


@respx.mock
async def test_collecting_the_catalog_yields_records_seeds_and_counts() -> None:
    respx.get(f"{ORIGIN}/products.json").mock(side_effect=_paged("products", {"1": [
        shopify_product("tee", pid=1, alt="A tee"),
        shopify_product("bare", pid=2, body="", alt=None),
    ]}))
    respx.get(f"{ORIGIN}/collections.json").mock(side_effect=_paged("collections", {"1": [
        shopify_collection("shirts", cid=7, description="<p>All shirts</p>"),
    ]}))
    probe = SiteProbe(start_url=f"{ORIGIN}/", origin=ORIGIN, host="shop.test",
                      robots=RobotsRules.permissive(), is_shopify=True, currency="EUR")
    async with crawl_client() as client:
        catalog = await collect_site_catalog(client, probe, crawl_id="c1", limiter=_limiter())

    assert catalog.status == "complete"
    assert (catalog.products, catalog.collections) == (2, 1)
    assert set(catalog.urls) == {f"{ORIGIN}/products/tee", f"{ORIGIN}/products/bare", f"{ORIGIN}/collections/shirts"}
    assert catalog.gap_counts == {"image_alt_missing": 2, "description_missing": 1}

    by_handle = {r["handle"]: r for r in catalog.records}
    bare = by_handle["bare"]
    assert bare["crawl_id"] == "c1" and bare["host"] == "shop.test" and bare["kind"] == "product"
    assert bare["gaps"] == ["description_missing", "image_alt_missing"] and bare["gap_count"] == 2
    assert bare["currency"] == "EUR" and bare["price"] == "18.00"
    assert isinstance(bare["updated_at"], datetime)  # what an incremental re-crawl compares
    assert by_handle["shirts"]["products_count"] == 4
    assert by_handle["tee"]["description_words"] == 2

    summary = catalog.summary()
    assert summary["status"] == "complete" and summary["requests"] == 2


@respx.mock
async def test_a_store_with_no_json_catalog_is_unavailable_not_an_error() -> None:
    respx.get(f"{ORIGIN}/products.json").mock(return_value=httpx.Response(404))
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=httpx.Response(404))
    probe = SiteProbe(start_url=f"{ORIGIN}/", origin=ORIGIN, host="shop.test",
                      robots=RobotsRules.permissive(), is_shopify=True)
    async with crawl_client() as client:
        catalog = await collect_site_catalog(client, probe, crawl_id="c2", limiter=_limiter())

    assert catalog.status == "unavailable"
    assert catalog.urls == [] and catalog.records == []
    assert any("answered 404" in n for n in catalog.notes)
