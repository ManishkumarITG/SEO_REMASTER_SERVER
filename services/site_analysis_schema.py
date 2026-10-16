"""Detect structured-data (JSON-LD) issues on a bounded sample of pages.

The crawler deliberately does not keep page HTML, so schema is checked here, on a
small sample - the representative page of each template plus a few more per
category. That mirrors how the pipeline samples PageSpeed (one run per template):
a report says "we checked N product pages, X were missing aggregateRating", which
is honest and cheap, rather than parsing schema on every page of a 5,000-page
store.

Scope is e-commerce: Product and its Offer, plus the presence of BreadcrumbList
and Organization. The recommended-field lists are Google's for merchant listings
and product rich results, which is what the reference report's schema sections
are about.

Parsing is defensive throughout: the JSON-LD comes from a third party and is
frequently malformed, so a bad block is skipped, never fatal.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from selectolax.parser import HTMLParser

logger = logging.getLogger("seo.site_analysis.schema")

#: Fields Google recommends on a Product for rich results / merchant listings.
#: `offers` carries price; the merchant-listing extras (shipping, returns) live
#: inside the Offer and are checked separately.
PRODUCT_RECOMMENDED = ("name", "image", "description", "offers", "brand", "sku", "aggregateRating", "review")
#: Merchant-listing fields, checked on the Offer object.
OFFER_MERCHANT_FIELDS = ("price", "priceCurrency", "availability", "shippingDetails", "hasMerchantReturnPolicy")


def _iter_ldjson(dom: HTMLParser) -> list[Any]:
    """Every parseable JSON-LD document on the page, @graph flattened one level."""
    objects: list[Any] = []
    for node in dom.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Malformed JSON-LD is extremely common; a broken block is not a
            # crawl failure, it is one more finding ("invalid JSON-LD").
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and "@graph" in item and isinstance(item["@graph"], list):
                objects.extend(g for g in item["@graph"] if isinstance(g, dict))
            elif isinstance(item, dict):
                objects.append(item)
    return objects


def _types_of(obj: dict[str, Any]) -> list[str]:
    """The @type value(s) of one JSON-LD object, always as a list of strings."""
    raw = obj.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def _first_offer(product: dict[str, Any]) -> dict[str, Any] | None:
    offers = product.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                return offer
    return None


def summarize_schema(html: str) -> dict[str, Any]:
    """What structured data one page carries, and what a Product is missing.

    Returns a compact dict: the @types present, whether a Product/BreadcrumbList/
    Organization exists, and for the first Product the recommended fields it lacks.
    `invalid_blocks` counts JSON-LD scripts that failed to parse at all.
    """
    dom = HTMLParser(html or "")
    scripts = dom.css('script[type="application/ld+json"]')
    objects = _iter_ldjson(dom)

    all_types: set[str] = set()
    product: dict[str, Any] | None = None
    for obj in objects:
        types = _types_of(obj)
        all_types.update(types)
        if product is None and "Product" in types:
            product = obj

    parsed_ok = 0
    for node in scripts:
        raw = (node.text() or "").strip()
        if not raw:
            continue
        try:
            json.loads(raw)
            parsed_ok += 1
        except (ValueError, TypeError):
            pass
    invalid_blocks = len([n for n in scripts if (n.text() or "").strip()]) - parsed_ok

    product_missing: list[str] = []
    offer_missing: list[str] = []
    if product is not None:
        product_missing = [f for f in PRODUCT_RECOMMENDED if not product.get(f)]
        offer = _first_offer(product)
        if offer is None:
            offer_missing = list(OFFER_MERCHANT_FIELDS)
        else:
            offer_missing = [f for f in OFFER_MERCHANT_FIELDS if not offer.get(f)]

    return {
        "types": sorted(all_types),
        "has_product": product is not None,
        "has_breadcrumb": "BreadcrumbList" in all_types,
        "has_organization": bool({"Organization", "OnlineStore", "Store"} & all_types),
        "product_missing_fields": product_missing,
        "offer_missing_fields": offer_missing,
        "invalid_blocks": max(0, invalid_blocks),
        "jsonld_blocks": len(scripts),
    }


def aggregate_schema(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll several page summaries into the figures a report cites.

    Each sample is `{"url", "template", "summary"}` where summary is the dict from
    `summarize_schema`. Counts are across the SAMPLED pages, and the sample size
    is returned so the report never implies store-wide totals it did not measure.
    """
    if not samples:
        return {"sampled": 0}

    product_samples = [s for s in samples if s["template"] == "product"]
    with_product = sum(1 for s in product_samples if s["summary"]["has_product"])
    field_gaps: dict[str, int] = {}
    offer_gaps: dict[str, int] = {}
    for s in product_samples:
        for field in s["summary"]["product_missing_fields"]:
            field_gaps[field] = field_gaps.get(field, 0) + 1
        for field in s["summary"]["offer_missing_fields"]:
            offer_gaps[field] = offer_gaps.get(field, 0) + 1

    return {
        "sampled": len(samples),
        "product_pages_sampled": len(product_samples),
        "product_pages_with_product_schema": with_product,
        "product_pages_missing_product_schema": len(product_samples) - with_product,
        "product_field_gaps": field_gaps,
        "merchant_offer_gaps": offer_gaps,
        "pages_with_breadcrumb": sum(1 for s in samples if s["summary"]["has_breadcrumb"]),
        "pages_with_organization": sum(1 for s in samples if s["summary"]["has_organization"]),
        "pages_with_invalid_jsonld": sum(1 for s in samples if s["summary"]["invalid_blocks"] > 0),
        "types_seen": sorted({t for s in samples for t in s["summary"]["types"]}),
    }


#: Pages fetched for the schema check, per template and overall. Small and
#: template-spread for the same reason PageSpeed samples: the check is
#: representative, not exhaustive, and every fetch is a request to a third party.
SCHEMA_PER_TEMPLATE = 3
SCHEMA_MAX_PAGES = 12
#: Only these templates carry the structured data a store report cares about.
SCHEMA_TEMPLATES = ("product", "collection", "home", "article")


def choose_schema_sample(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Up to SCHEMA_MAX_PAGES fetched pages, at most SCHEMA_PER_TEMPLATE per template."""
    by_template: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        if not page.get("status_code") or page.get("skipped") or page.get("error"):
            continue
        template = page.get("template") or "other"
        if template not in SCHEMA_TEMPLATES:
            continue
        bucket = by_template.setdefault(template, [])
        if len(bucket) < SCHEMA_PER_TEMPLATE:
            bucket.append(page)
    chosen: list[dict[str, Any]] = []
    for bucket in by_template.values():
        chosen.extend(bucket)
    return chosen[:SCHEMA_MAX_PAGES]


async def sample_schema(pages: list[dict[str, Any]], *, user_agent: str) -> dict[str, Any]:
    """Fetch a small template-spread sample and report its structured-data state.

    Re-fetches because the crawler does not keep HTML. Bounded, best-effort, and
    never raises: a schema check that could not run must not fail the analysis.
    """
    from services.crawler.http_client import MAX_HEAD_BYTES, crawl_client, get_capped

    sample = choose_schema_sample(pages)
    if not sample:
        return {"sampled": 0}

    summaries: list[dict[str, Any]] = []
    try:
        async with crawl_client(user_agent=user_agent) as client:
            for page in sample:
                url = page.get("final_url") or page["url"]
                # The <head> plus early body holds the JSON-LD; a full read is not
                # needed. MAX_HEAD_BYTES (256 KiB) covers it on every real theme.
                result = await get_capped(
                    client, url, max_bytes=MAX_HEAD_BYTES * 2, accept="text/html,*/*;q=0.1"
                )
                if result.error or not result.text:
                    continue
                summaries.append({
                    "url": url,
                    "template": page.get("template") or "other",
                    "summary": summarize_schema(result.text),
                })
    except Exception as exc:  # noqa: BLE001 - schema is a nice-to-have; never fatal
        logger.warning("schema sampling failed: %s: %s", type(exc).__name__, exc)

    aggregate = aggregate_schema(summaries)
    aggregate["examples"] = [
        {"url": s["url"], "template": s["template"],
         "missing_product_fields": s["summary"]["product_missing_fields"],
         "missing_offer_fields": s["summary"]["offer_missing_fields"]}
        for s in summaries if s["template"] == "product"
    ][:5]
    return aggregate
