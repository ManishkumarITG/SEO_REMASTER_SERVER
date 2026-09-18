"""Build a factual profile of a store page, for the Reddit post generator to draw on.

Reuses the existing crawler (services/crawler/fetcher.fetch_page + parser) for the
HTML page - there is deliberately no second HTML fetcher here.

`/products.json` needs its own small JSON GET: fetch_page() hard-rejects any
content type that is not HTML, which is the right behaviour for an SEO audit but
means it cannot carry a JSON endpoint. That helper reuses the crawler's own
User-Agent and timeout so the store still sees one consistent client.

Profiles are cached for an hour, keyed by the normalised URL.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field
from selectolax.parser import HTMLParser

from services.crawler.fetcher import (
    TIMEOUT_SECONDS,
    USER_AGENT,
    FetchError,
    fetch_page,
    read_capped,
)
from services.crawler.parser import PageData, extract_main_text
from utils.cache import get_cache

logger = logging.getLogger("seo.backlinks.store")

_PROFILE_CACHE_TTL = 3600
_PRODUCT_LIMIT = 12
_DESCRIPTION_CHARS = 1500
_PRODUCT_BLURB_CHARS = 200
#: /products.json is a third-party endpoint on a URL the user supplied, so its
#: size is not ours to trust - the same rule the HTML fetcher applies.
_PRODUCTS_MAX_BYTES = 2 * 1024 * 1024
#: Markers that identify a Shopify storefront in the served HTML.
_SHOPIFY_MARKERS = ("cdn.shopify.com", "Shopify.theme", "/cdn/shop/")


class StoreProduct(BaseModel):
    title: str
    product_type: str = ""
    vendor: str = ""
    blurb: str = ""
    price: str | None = None
    url: str | None = None


class StoreProfile(BaseModel):
    store_url: str
    store_name: str = ""
    tagline: str = ""
    description_excerpt: str = ""
    is_shopify: bool = False
    products: list[StoreProduct] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    products_source: str = "none"
    fetched_at: datetime | None = None


def normalize_store_url(raw: str) -> str:
    """Add https:// when missing, drop query/fragment, keep host + path."""
    url = raw.strip()
    if not url:
        raise ValueError("Store URL must not be empty")
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Only http(s) store URLs are supported")
    if not parts.hostname or "." not in parts.hostname:
        raise ValueError("Store URL must include a valid host")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", "", ""))


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _text_from_html(html: str, limit: int) -> str:
    """Strip tags out of a Shopify body_html fragment."""
    if not html:
        return ""
    text = " ".join(HTMLParser(html).text(separator=" ").split())
    return text[:limit].rstrip()


def _first(dom: HTMLParser, selector: str, attribute: str | None = None) -> str:
    node = dom.css_first(selector)
    if node is None:
        return ""
    value = node.attributes.get(attribute, "") if attribute else node.text()
    return " ".join((value or "").split())


async def _fetch_products_json(origin: str) -> tuple[list[StoreProduct], list[str], str]:
    """Read up to 12 products from /products.json. Any failure is non-fatal."""
    url = f"{origin}/products.json"
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            async with client.stream("GET", url, params={"limit": _PRODUCT_LIMIT}) as response:
                if response.status_code >= 400:
                    return [], [], "none"
                body, truncated = await read_capped(response, _PRODUCTS_MAX_BYTES)
        if truncated:
            # A truncated JSON document will not parse, and guessing where it
            # should end would invent data. No products is the honest answer.
            logger.info("products.json at %s exceeded %d bytes - ignored", url, _PRODUCTS_MAX_BYTES)
            return [], [], "none"
        payload = json.loads(body)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("No usable products.json at %s: %s", url, exc)
        return [], [], "none"

    raw_products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(raw_products, list) or not raw_products:
        return [], [], "none"

    products: list[StoreProduct] = []
    categories: list[str] = []
    for item in raw_products[:_PRODUCT_LIMIT]:
        if not isinstance(item, dict):
            continue
        variants = item.get("variants")
        price = None
        if isinstance(variants, list) and variants and isinstance(variants[0], dict):
            raw_price = variants[0].get("price")
            price = str(raw_price) if raw_price is not None else None
        handle = item.get("handle")
        products.append(
            StoreProduct(
                title=str(item.get("title") or "").strip(),
                product_type=str(item.get("product_type") or "").strip(),
                vendor=str(item.get("vendor") or "").strip(),
                blurb=_text_from_html(str(item.get("body_html") or ""), _PRODUCT_BLURB_CHARS),
                price=price,
                url=f"{origin}/products/{handle}" if handle else None,
            )
        )
        for value in (item.get("product_type"), item.get("vendor")):
            text = str(value or "").strip()
            if text and text not in categories:
                categories.append(text)

    return products, categories, "products_json"


def _profile_from_page(store_url: str, page: PageData) -> StoreProfile:
    dom = page.dom
    title = _first(dom, "title")
    meta_description = _first(dom, 'meta[name="description"]', "content")
    h1 = _first(dom, "h1")
    site_name = _first(dom, 'meta[property="og:site_name"]', "content")
    # "Store — tagline" / "Store | tagline": the part before the separator is the name.
    store_name = site_name or next((part.strip() for part in title.split("|") if part.strip()), "")
    if not store_name:
        store_name = urlsplit(store_url).hostname or store_url
    return StoreProfile(
        store_url=store_url,
        store_name=store_name,
        tagline=h1 or meta_description,
        description_excerpt=extract_main_text(dom, max_chars=_DESCRIPTION_CHARS),
        is_shopify=any(marker in page.html for marker in _SHOPIFY_MARKERS),
        fetched_at=page.fetched_at,
    )


async def build_store_profile(store_url: str, *, use_cache: bool = True) -> StoreProfile:
    """Fetch and profile a store: homepage facts plus up to 12 products.

    Raises FetchError when the page itself cannot be fetched; a missing or
    non-JSON /products.json is not an error and simply yields no products.
    """
    normalized = normalize_store_url(store_url)
    cache = get_cache()
    cache_key = f"store_profile:{normalized}"
    if use_cache:
        cached = await cache.get(cache_key)
        if cached is not None:
            return StoreProfile.model_validate(cached)

    page = await fetch_page(normalized)
    profile = _profile_from_page(normalized, page)
    products, categories, source = await _fetch_products_json(_origin_of(page.final_url))
    profile.products = products
    profile.categories = categories
    profile.products_source = source
    if profile.fetched_at is None:
        profile.fetched_at = datetime.now(UTC)

    await cache.set(cache_key, profile.model_dump(mode="json"), _PROFILE_CACHE_TTL)
    return profile


__all__ = [
    "FetchError",
    "StoreProduct",
    "StoreProfile",
    "build_store_profile",
    "normalize_store_url",
]
