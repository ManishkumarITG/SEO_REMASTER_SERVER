"""Schema parsing/sampling and GSC property-matching/sampling. Pure + mocked."""

from __future__ import annotations

from services.site_analysis_gsc import MAX_INSPECTIONS, _sample_urls, choose_property
from services.site_analysis_schema import (
    SCHEMA_PER_TEMPLATE,
    aggregate_schema,
    choose_schema_sample,
    summarize_schema,
)

HOST = "shop.test"


# --- schema parsing --------------------------------------------------------------------


def test_a_product_with_offer_reports_its_missing_fields() -> None:
    html = """<html><head>
      <script type="application/ld+json">
      {"@type":"Product","name":"Linen Shirt","image":"a.jpg",
       "offers":{"@type":"Offer","price":"18","priceCurrency":"USD"}}
      </script></head></html>"""
    s = summarize_schema(html)
    assert s["has_product"] is True
    assert "aggregateRating" in s["product_missing_fields"]
    assert "review" in s["product_missing_fields"]
    # price/currency are present on the offer, so only the extras are missing.
    assert "price" not in s["offer_missing_fields"]
    assert "hasMerchantReturnPolicy" in s["offer_missing_fields"]


def test_types_are_read_from_a_graph_wrapper() -> None:
    html = """<script type="application/ld+json">
      {"@graph":[{"@type":"Organization","name":"S"},{"@type":"BreadcrumbList"}]}
      </script>"""
    s = summarize_schema(f"<html><head>{html}</head></html>")
    assert s["has_organization"] is True and s["has_breadcrumb"] is True


def test_malformed_jsonld_is_counted_not_fatal() -> None:
    s = summarize_schema('<script type="application/ld+json">{bad,,,}</script>')
    assert s["invalid_blocks"] == 1 and s["has_product"] is False


def test_a_page_with_no_jsonld_is_empty_not_an_error() -> None:
    s = summarize_schema("<html><body><p>no schema</p></body></html>")
    assert s["types"] == [] and s["jsonld_blocks"] == 0


def test_aggregate_counts_product_field_gaps_across_the_sample() -> None:
    with_rating = summarize_schema(
        '<script type="application/ld+json">{"@type":"Product","name":"A","image":"i",'
        '"description":"d","brand":"b","sku":"1","aggregateRating":{"@type":"AggregateRating"},'
        '"review":[{}],"offers":{"@type":"Offer","price":"9","priceCurrency":"USD",'
        '"availability":"InStock","shippingDetails":{},"hasMerchantReturnPolicy":{}}}</script>'
    )
    without = summarize_schema('<script type="application/ld+json">{"@type":"Product","name":"B"}</script>')
    agg = aggregate_schema([
        {"url": "/a", "template": "product", "summary": with_rating},
        {"url": "/b", "template": "product", "summary": without},
    ])
    assert agg["product_pages_sampled"] == 2
    assert agg["product_pages_with_product_schema"] == 2
    assert agg["product_field_gaps"]["aggregateRating"] == 1  # only /b lacks it


def test_the_schema_sample_spreads_across_templates_and_caps_each() -> None:
    pages = [{"url": f"https://s/p{i}", "template": "product", "status_code": 200} for i in range(10)]
    pages += [{"url": "https://s/", "template": "home", "status_code": 200}]
    pages += [{"url": "https://s/pages/x", "template": "page", "status_code": 200}]  # not a schema template
    chosen = choose_schema_sample(pages)
    assert sum(1 for c in chosen if c["template"] == "product") == SCHEMA_PER_TEMPLATE
    assert any(c["template"] == "home" for c in chosen)
    assert all(c["template"] != "page" for c in chosen)


# --- GSC property matching + sampling --------------------------------------------------


def test_a_domain_property_is_preferred_over_a_url_prefix() -> None:
    sites = [{"siteUrl": "https://www.shop.test/"}, {"siteUrl": "sc-domain:shop.test"}]
    assert choose_property(sites, "www.shop.test") == "sc-domain:shop.test"


def test_a_url_prefix_property_matches_on_host() -> None:
    assert choose_property([{"siteUrl": "https://shop.test/"}], "shop.test") == "https://shop.test/"


def test_no_matching_property_returns_none() -> None:
    assert choose_property([{"siteUrl": "sc-domain:other.test"}], "shop.test") is None


def test_inspection_sampling_is_capped_and_template_spread() -> None:
    pages = [{"url": f"https://s/p{i}", "template": "product", "status_code": 200} for i in range(50)]
    pages += [{"url": "https://s/blog/a", "template": "article", "status_code": 200}]
    pages += [{"url": "https://s/x", "template": "product", "status_code": None, "skipped": "robots"}]
    sample = _sample_urls(pages)
    assert len(sample) <= MAX_INSPECTIONS
    assert "https://s/blog/a" in sample  # the lone article still gets a slot
    assert "https://s/x" not in sample  # skipped pages are never inspected
