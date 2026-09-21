"""Catalog discovery: paginated Shopify JSON and the sitemap fallback. No network (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

import services.stores.discovery as discovery
from exceptions.stores import CatalogUnavailable, RobotsDisallowed
from services.crawler.http_client import crawl_client
from services.stores.discovery import (
    classify_catalog_url,
    collection_from_json,
    discover_catalog,
    parse_sitemap_locs,
    probe_store,
    product_from_json,
)
from tests.factories import (
    BILLION_LAUGHS_XML,
    SITEMAP_COLLECTIONS_XML,
    SITEMAP_INDEX_XML,
    SITEMAP_PRODUCTS_XML,
    shopify_collection,
    shopify_product,
)

ORIGIN = "https://shop.test"
HOME = "<html><head><title>Shop</title><meta property='og:site_name' content='Ember & Oak'></head><body>cdn.shopify.com</body></html>"


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF check resolves DNS; test hosts must not hit the resolver."""
    monkeypatch.setattr(discovery, "is_public_host", lambda host: True)


def _no_other_sitemaps() -> None:
    """Registered LAST (respx matches in order): any sitemap candidate a test did not mock is a 404."""
    respx.get(url__regex=r"https://(www\.)?shop\.test/.*sitemap.*").mock(return_value=httpx.Response(404))


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _mock_home(robots: str = "User-agent: *\nDisallow: /cart") -> None:
    respx.get(f"{ORIGIN}/").mock(return_value=httpx.Response(200, text=HOME, headers={"content-type": "text/html"}))
    respx.get(f"{ORIGIN}/robots.txt").mock(return_value=httpx.Response(200, text=robots))


@respx.mock
async def test_paginated_products_json_is_followed_to_the_last_page() -> None:
    _mock_home()
    page1 = [shopify_product(f"p{i}", pid=i) for i in range(250)]
    page2 = [shopify_product("last", pid=999)]

    def products(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        limit = request.url.params.get("limit")
        if limit == "1":
            return _json({"products": page1[:1]})
        return _json({"products": {"1": page1, "2": page2}.get(page, [])})

    respx.get(f"{ORIGIN}/products.json").mock(side_effect=products)
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=httpx.Response(404))
    respx.get(f"{ORIGIN}/sitemap.xml").mock(return_value=httpx.Response(404))

    _no_other_sitemaps()
    async with crawl_client() as client:
        probe = await probe_store(client, f"{ORIGIN}/")
        result = await discover_catalog(client, probe)

    assert result.source == "products_json"
    assert result.products_total == 251
    assert result.items[0].price == "18.00" and result.items[0].available is True
    assert result.items[0].featured_image_alt == ""  # null alt -> ""


@respx.mock
async def test_discovery_stops_when_a_page_repeats_the_previous_page() -> None:
    """A proxy that strips ?page= would otherwise feed us page 1 forty times."""
    _mock_home()
    same = [shopify_product(f"p{i}", pid=i) for i in range(250)]
    respx.get(f"{ORIGIN}/products.json").mock(return_value=_json({"products": same}))
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=httpx.Response(404))
    respx.get(f"{ORIGIN}/sitemap.xml").mock(return_value=httpx.Response(404))

    _no_other_sitemaps()
    async with crawl_client() as client:
        probe = await probe_store(client, f"{ORIGIN}/")
        result = await discover_catalog(client, probe)

    assert result.products_total == 250
    assert any("ignores the page parameter" in n for n in result.notes)


@respx.mock
async def test_a_403_on_products_json_falls_back_to_the_sitemap() -> None:
    """gymshark.com's real behaviour."""
    _mock_home()
    respx.get(f"{ORIGIN}/products.json").mock(return_value=httpx.Response(403, text="<html>blocked</html>"))
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=httpx.Response(403))
    respx.get(f"{ORIGIN}/sitemap.xml").mock(return_value=httpx.Response(200, text=SITEMAP_INDEX_XML))
    respx.get(url__regex=rf"{ORIGIN}/sitemap_products_1\.xml.*").mock(
        return_value=httpx.Response(200, text=SITEMAP_PRODUCTS_XML))
    respx.get(f"{ORIGIN}/sitemap_collections_1.xml").mock(return_value=httpx.Response(200, text=SITEMAP_COLLECTIONS_XML))
    locale = respx.get(f"{ORIGIN}/es-US/sitemap_products_1.xml").mock(return_value=httpx.Response(200, text=SITEMAP_PRODUCTS_XML))
    pages = respx.get(f"{ORIGIN}/sitemap_pages_1.xml").mock(return_value=httpx.Response(200, text="<urlset/>"))
    evil = respx.get("https://evil.test/sitemap_products_1.xml").mock(return_value=httpx.Response(200, text=SITEMAP_PRODUCTS_XML))

    _no_other_sitemaps()
    async with crawl_client() as client:
        probe = await probe_store(client, f"{ORIGIN}/")
        assert probe.blocked
        result = await discover_catalog(client, probe)

    assert result.source == "sitemap"
    assert sorted(i.handle for i in result.items if i.kind == "product") == ["linen-shirt", "wool-scarf"]
    assert [i.handle for i in result.items if i.kind == "collection"] == ["summer"]  # /collections/all skipped
    assert not locale.called, "locale sub-sitemap must be skipped"
    assert not pages.called, "pages sitemap is not catalog"
    assert not evil.called, "foreign-host sitemaps are never fetched"
    assert result.blocked


@respx.mock
async def test_html_served_at_products_json_is_not_treated_as_a_catalog() -> None:
    _mock_home()
    respx.get(f"{ORIGIN}/products.json").mock(return_value=httpx.Response(200, text="<html>not json</html>",
                                                                          headers={"content-type": "text/html"}))
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=httpx.Response(404))
    respx.get(f"{ORIGIN}/sitemap.xml").mock(return_value=httpx.Response(404))

    async with crawl_client() as client:
        with pytest.raises(CatalogUnavailable) as excinfo:  # sync CM: cannot share the async-with
            await probe_store(client, f"{ORIGIN}/")
    assert "/products.json" in excinfo.value.message and "/sitemap.xml" in excinfo.value.message


@respx.mock
async def test_a_blanket_robots_disallow_for_us_is_refused_before_any_catalog_request() -> None:
    _mock_home(robots="User-agent: *\nDisallow: /")
    products = respx.get(f"{ORIGIN}/products.json").mock(return_value=_json({"products": []}))

    async with crawl_client() as client:
        with pytest.raises(RobotsDisallowed):
            await probe_store(client, f"{ORIGIN}/")
    assert not products.called


@respx.mock
async def test_products_via_json_and_collections_via_sitemap_is_reported_as_mixed() -> None:
    _mock_home()
    respx.get(f"{ORIGIN}/products.json").mock(return_value=_json({"products": [shopify_product("p1", pid=1)]}))
    respx.get(f"{ORIGIN}/collections.json").mock(return_value=httpx.Response(404))
    respx.get(f"{ORIGIN}/sitemap.xml").mock(return_value=httpx.Response(200, text=SITEMAP_INDEX_XML))
    respx.get(url__regex=rf"{ORIGIN}/sitemap_products_1\.xml.*").mock(return_value=httpx.Response(200, text=SITEMAP_PRODUCTS_XML))
    respx.get(f"{ORIGIN}/sitemap_collections_1.xml").mock(return_value=httpx.Response(200, text=SITEMAP_COLLECTIONS_XML))
    respx.get(url__regex=rf"{ORIGIN}/(es-US/)?sitemap_(pages|products)_1\.xml").mock(return_value=httpx.Response(200, text="<urlset/>"))

    _no_other_sitemaps()
    async with crawl_client() as client:
        probe = await probe_store(client, f"{ORIGIN}/")
        result = await discover_catalog(client, probe)

    assert result.source == "mixed"
    assert result.products_total == 1 and result.collections_total == 1


@respx.mock
async def test_the_effective_origin_follows_an_apex_to_www_redirect() -> None:
    """Every built URL must use the post-redirect origin or canonical_mismatch fires everywhere."""
    respx.get("https://shop.test/").mock(return_value=httpx.Response(301, headers={"location": "https://www.shop.test/"}))
    respx.get("https://www.shop.test/").mock(return_value=httpx.Response(200, text=HOME, headers={"content-type": "text/html"}))
    respx.get("https://www.shop.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://www.shop.test/products.json").mock(return_value=_json({"products": [shopify_product("p1", pid=1)]}))
    respx.get("https://www.shop.test/collections.json").mock(return_value=httpx.Response(404))
    respx.get("https://www.shop.test/sitemap.xml").mock(return_value=httpx.Response(404))

    _no_other_sitemaps()
    async with crawl_client() as client:
        probe = await probe_store(client, "https://shop.test/")
        result = await discover_catalog(client, probe)

    assert probe.origin.origin == "https://www.shop.test"
    assert result.items[0].url == "https://www.shop.test/products/p1"


def test_sitemap_loc_extraction_is_inert_against_an_entity_bomb() -> None:
    locs = parse_sitemap_locs(BILLION_LAUGHS_XML)
    assert locs == ["https://shop.test/products/one", "&lol3;"]


def test_sitemap_loc_extraction_unescapes_and_caps() -> None:
    assert parse_sitemap_locs("<loc>https://x/a?b=1&amp;c=2</loc>") == ["https://x/a?b=1&c=2"]
    assert len(parse_sitemap_locs("".join(f"<loc>https://x/{i}</loc>" for i in range(100)), limit=10)) == 10


def test_catalog_urls_are_classified_by_path_shape() -> None:
    assert classify_catalog_url("https://s/products/linen-shirt") == "product"
    assert classify_catalog_url("https://s/collections/summer") == "collection"
    assert classify_catalog_url("https://s/collections/all") is None
    assert classify_catalog_url("https://s/collections/summer/products/linen-shirt") is None
    assert classify_catalog_url("https://s/pages/about") is None
    assert classify_catalog_url("https://s/products/bad handle") is None


def test_product_mapping_keeps_price_as_string_and_computes_a_range() -> None:
    raw = shopify_product("multi", pid=7)
    raw["variants"] = [{"price": "18.00", "available": False}, {"price": "24.50", "available": True}]
    item = product_from_json(ORIGIN, raw)
    assert item is not None
    assert item.price == "18.00" and item.price_min == "18.00" and item.price_max == "24.50"
    assert item.available is True and item.variants_count == 2
    assert item.description == "Soft linen."


def test_products_with_an_invalid_handle_are_dropped_not_quoted() -> None:
    assert product_from_json(ORIGIN, shopify_product("ok", pid=1) | {"handle": "../etc/passwd"}) is None
    assert product_from_json(ORIGIN, shopify_product("ok", pid=1) | {"handle": "a?b"}) is None


def test_collection_image_is_a_single_object_not_a_list() -> None:
    item = collection_from_json(ORIGIN, shopify_collection("summer", cid=3, description="<p>Hot <i>days</i></p>"))
    assert item is not None
    assert item.featured_image_url.endswith("c-summer.jpg") and item.featured_image_alt == ""
    assert item.description == "Hot days" and item.products_count == 4
