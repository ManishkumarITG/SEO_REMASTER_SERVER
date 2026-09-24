"""Per-template Lighthouse reports: grouping, representative choice, degradation."""

from __future__ import annotations

import pytest

from services.crawler.models import CrawledPage
from services.site_crawl_templates import (
    MAX_TEMPLATES_TESTED,
    build_template_reports,
    group_by_template,
    pick_representative,
)

ORIGIN = "https://shop.test"


def _page(url: str, template: str, words: int = 500, **kwargs: object) -> CrawledPage:
    return CrawledPage(
        url=url, final_url=url, status_code=kwargs.pop("status_code", 200),
        template=template, word_count=words, **kwargs,  # type: ignore[arg-type]
    )


# --- grouping ------------------------------------------------------------------------


def test_pages_are_grouped_by_template() -> None:
    pages = [
        _page(f"{ORIGIN}/products/a", "product"),
        _page(f"{ORIGIN}/products/b", "product"),
        _page(f"{ORIGIN}/collections/x", "collection"),
        _page(f"{ORIGIN}/", "home"),
    ]
    groups = {g.template: g for g in group_by_template(pages)}
    assert groups["product"].page_count == 2
    assert groups["collection"].page_count == 1
    assert groups["home"].page_count == 1


def test_products_are_measured_before_less_valuable_templates() -> None:
    """The cap should bite on the account page, never on the product template."""
    pages = [_page(f"{ORIGIN}/account", "account"), _page(f"{ORIGIN}/products/a", "product")]
    assert [g.template for g in group_by_template(pages)] == ["product", "account"]


def test_a_bigger_group_outranks_a_smaller_one_of_the_same_kind() -> None:
    pages = [_page(f"{ORIGIN}/pages/a", "page")]
    pages += [_page(f"{ORIGIN}/blogs/n/{i}", "article") for i in range(5)]
    ordered = [g.template for g in group_by_template(pages)]
    assert ordered.index("article") < ordered.index("page")


def test_pages_that_were_never_fetched_are_not_grouped() -> None:
    pages = [
        _page(f"{ORIGIN}/products/a", "product"),
        CrawledPage(url=f"{ORIGIN}/private", skipped="robots", template="other"),
        CrawledPage(url=f"{ORIGIN}/broken", error="timeout", template="other"),
    ]
    groups = group_by_template(pages)
    assert [g.template for g in groups] == ["product"]


def test_a_page_with_no_template_falls_into_other() -> None:
    assert group_by_template([_page(f"{ORIGIN}/x", "")])[0].template == "other"


# --- representative choice -----------------------------------------------------------


def test_the_median_page_represents_the_group_not_the_first() -> None:
    """An empty product or a 5,000-word essay must not describe the template."""
    pages = [
        _page(f"{ORIGIN}/products/empty", "product", words=3),
        _page(f"{ORIGIN}/products/normal", "product", words=500),
        _page(f"{ORIGIN}/products/essay", "product", words=5000),
    ]
    chosen = pick_representative(pages)
    assert chosen is not None and chosen.url == f"{ORIGIN}/products/normal"


def test_a_group_with_nothing_usable_has_no_representative() -> None:
    assert pick_representative([CrawledPage(url=f"{ORIGIN}/x", skipped="robots")]) is None
    assert pick_representative([]) is None


def test_a_failed_page_is_never_the_representative() -> None:
    pages = [
        _page(f"{ORIGIN}/products/gone", "product", status_code=404),
        _page(f"{ORIGIN}/products/ok", "product"),
    ]
    chosen = pick_representative(pages)
    assert chosen is not None and chosen.url == f"{ORIGIN}/products/ok"


# --- building the reports -------------------------------------------------------------


async def test_one_report_per_template_with_one_lighthouse_run_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: 200 product pages cost ONE Lighthouse run."""
    calls: list[str] = []

    async def fake_psi(url, strategy="mobile", categories=()):
        calls.append(url)
        return {"performance_score": 71, "lcp_ms": 3100.0, "seo_score": 92}

    monkeypatch.setattr("services.site_crawl_templates.fetch_pagespeed", fake_psi)
    pages = [_page(f"{ORIGIN}/products/p{i}", "product") for i in range(200)]
    pages += [_page(f"{ORIGIN}/collections/c{i}", "collection") for i in range(20)]

    records = await build_template_reports(pages)
    assert len(calls) == 2  # not 220
    by_template = {r["template"]: r for r in records}
    assert by_template["product"]["page_count"] == 200
    assert by_template["product"]["lighthouse"]["performance_score"] == 71
    assert by_template["collection"]["page_count"] == 20


async def test_a_pagespeed_failure_leaves_the_record_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_psi(url, strategy="mobile", categories=()):
        return None

    monkeypatch.setattr("services.site_crawl_templates.fetch_pagespeed", fake_psi)
    (record,) = await build_template_reports([_page(f"{ORIGIN}/products/a", "product")])
    assert record["lighthouse"] is None
    assert "no usable result" in record["error"]


async def test_a_raising_pagespeed_never_breaks_the_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(url, strategy="mobile", categories=()):
        raise RuntimeError("psi exploded")

    monkeypatch.setattr("services.site_crawl_templates.fetch_pagespeed", boom)
    (record,) = await build_template_reports([_page(f"{ORIGIN}/products/a", "product")])
    assert record["lighthouse"] is None and record["error"] == "RuntimeError"


async def test_templates_beyond_the_cap_are_reported_but_not_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting a template entirely would read as "this store has no blog"."""
    calls: list[str] = []

    async def fake_psi(url, strategy="mobile", categories=()):
        calls.append(url)
        return {"performance_score": 80}

    monkeypatch.setattr("services.site_crawl_templates.fetch_pagespeed", fake_psi)
    templates = ["product", "collection", "home", "article", "page", "blog", "search", "policy",
                 "cart", "account", "other"]
    pages = [_page(f"{ORIGIN}/{t}", t) for t in templates]

    records = await build_template_reports(pages, max_templates=3)
    assert len(calls) == 3
    assert len(records) == len(templates)  # every template still appears
    untested = [r for r in records if r["lighthouse"] is None]
    assert all("only the first 3" in r["error"] for r in untested)


async def test_no_pages_means_no_reports_and_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*args, **kwargs):
        raise AssertionError("PageSpeed must not be called for an empty crawl")

    monkeypatch.setattr("services.site_crawl_templates.fetch_pagespeed", fail)
    assert await build_template_reports([]) == []


def test_the_cap_default_is_small_enough_to_stay_cheap() -> None:
    """Each template costs a 10-30s Lighthouse run, so this must stay a handful."""
    assert MAX_TEMPLATES_TESTED <= 12
