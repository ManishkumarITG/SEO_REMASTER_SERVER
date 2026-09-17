"""build_store_profile: Shopify detection, products.json parsing, graceful degradation.

No network: httpx is mocked with respx, so both the homepage fetch (through the
shared crawler) and the products.json GET are served from fixtures.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from services.backlinks.store_profile import (
    build_store_profile,
    normalize_store_url,
)

STORE = "https://shop.example.com"

SHOPIFY_HTML = """<!doctype html>
<html lang="en">
<head>
  <title>Ember &amp; Oak | Hand-poured candles</title>
  <meta name="description" content="Small-batch soy candles poured in Leeds.">
  <meta property="og:site_name" content="Ember &amp; Oak">
  <script src="https://cdn.shopify.com/s/files/1/theme.js"></script>
</head>
<body>
  <h1>Hand-poured candles, made in small batches</h1>
  <p>We started in a kitchen in 2019 and now pour every candle by hand.</p>
</body>
</html>"""

PLAIN_HTML = """<!doctype html>
<html><head><title>Just A Blog</title></head>
<body><h1>Hello</h1><p>Nothing commercial here.</p></body></html>"""

PRODUCTS_JSON = {
    "products": [
        {
            "title": "Cedar & Smoke Candle",
            "product_type": "Candle",
            "vendor": "Ember & Oak",
            "handle": "cedar-smoke",
            "body_html": "<p>A <strong>woody</strong> candle with cedar and vetiver.</p>",
            "variants": [{"price": "24.00"}],
        },
        {
            "title": "Sea Salt Candle",
            "product_type": "Candle",
            "vendor": "Ember & Oak",
            "handle": "sea-salt",
            "body_html": "<p>Bright and mineral.</p>",
            "variants": [{"price": "22.00"}],
        },
        {
            "title": "Brass Wick Trimmer",
            "product_type": "Accessory",
            "vendor": "Toolworks",
            "handle": "wick-trimmer",
            "body_html": "",
            "variants": [{"price": "12.50"}],
        },
    ]
}


def _html_route(html: str) -> httpx.Response:
    return httpx.Response(200, html=html)


def test_normalize_store_url_adds_scheme_and_strips_query() -> None:
    assert normalize_store_url("shop.example.com") == "https://shop.example.com/"
    assert normalize_store_url("https://shop.example.com/collections/all?page=2") == (
        "https://shop.example.com/collections/all"
    )


def test_normalize_store_url_rejects_junk() -> None:
    for bad in ("", "   ", "ftp://shop.example.com", "notahost"):
        with pytest.raises(ValueError):
            normalize_store_url(bad)


@respx.mock
async def test_shopify_store_profile_includes_products() -> None:
    respx.get("https://shop.example.com/").mock(return_value=_html_route(SHOPIFY_HTML))
    respx.get("https://shop.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example.com/products.json").mock(
        return_value=httpx.Response(200, json=PRODUCTS_JSON)
    )

    profile = await build_store_profile(STORE, use_cache=False)

    assert profile.is_shopify is True
    assert profile.store_name == "Ember & Oak"
    assert profile.tagline.startswith("Hand-poured candles")
    assert "kitchen in 2019" in profile.description_excerpt
    assert profile.products_source == "products_json"
    assert [product.title for product in profile.products] == [
        "Cedar & Smoke Candle",
        "Sea Salt Candle",
        "Brass Wick Trimmer",
    ]
    first = profile.products[0]
    assert first.price == "24.00"
    assert first.url == "https://shop.example.com/products/cedar-smoke"
    # body_html is reduced to plain text, tags stripped.
    assert first.blurb == "A woody candle with cedar and vetiver."
    # Distinct product_type / vendor values become the category list.
    assert profile.categories == ["Candle", "Ember & Oak", "Accessory", "Toolworks"]


@respx.mock
async def test_non_shopify_page_is_flagged() -> None:
    respx.get("https://shop.example.com/").mock(return_value=_html_route(PLAIN_HTML))
    respx.get("https://shop.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example.com/products.json").mock(return_value=httpx.Response(404))

    profile = await build_store_profile(STORE, use_cache=False)

    assert profile.is_shopify is False
    assert profile.products == []
    assert profile.products_source == "none"


@respx.mock
async def test_missing_products_json_is_not_an_error() -> None:
    respx.get("https://shop.example.com/").mock(return_value=_html_route(SHOPIFY_HTML))
    respx.get("https://shop.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example.com/products.json").mock(return_value=httpx.Response(403))

    profile = await build_store_profile(STORE, use_cache=False)

    assert profile.is_shopify is True  # the page still profiled fine
    assert profile.products == []
    assert profile.products_source == "none"


@respx.mock
async def test_products_json_returning_html_is_ignored() -> None:
    """A theme that 200s an HTML page at /products.json must not break the profile."""
    respx.get("https://shop.example.com/").mock(return_value=_html_route(SHOPIFY_HTML))
    respx.get("https://shop.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://shop.example.com/products.json").mock(
        return_value=_html_route("<html><body>404</body></html>")
    )

    profile = await build_store_profile(STORE, use_cache=False)

    assert profile.products == []
    assert profile.products_source == "none"
