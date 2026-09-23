"""Site-wide crawl: bounded, polite, terminates, and reports why it stopped. No network (respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from config.settings import get_settings
from exceptions.crawler import FetchError
from services.crawler.http_client import crawl_client
from services.crawler.models import CrawlLimits, SiteCrawlResult
from services.crawler.parser import parse_html
from services.crawler.robots import RobotsRules
from services.crawler.site_crawl import (
    CONSECUTIVE_429_ABORT,
    collect_site_crawl,
    extract_page_facts,
    robots_target,
)
from services.crawler.sitemap import is_sitemap_url, looks_like_sitemap

ORIGIN = "https://site.test"
_DEFAULT = object()


def page_html(
    *,
    title: str = "Page",
    links: tuple[str, ...] = (),
    nofollow_links: tuple[str, ...] = (),
    description: str | None = None,
    canonical: str | None = None,
    robots: str | None = None,
    base: str | None = None,
    lang: str = "en",
    h1s: tuple[str, ...] = ("Heading",),
    h2s: int = 0,
    words: int = 0,
    image_alts: tuple[str | None, ...] = (),
) -> str:
    head = [f"<title>{title}</title>"]
    if description is not None:
        head.append(f'<meta name="description" content="{description}">')
    if canonical is not None:
        head.append(f'<link rel="canonical" href="{canonical}">')
    if robots is not None:
        head.append(f'<meta name="robots" content="{robots}">')
    if base is not None:
        head.append(f'<base href="{base}">')
    body = [f"<h1>{h}</h1>" for h in h1s]
    body += [f"<h2>Sub {i}</h2>" for i in range(h2s)]
    body += [f'<a href="{url}">link</a>' for url in links]
    body += [f'<a href="{url}" rel="nofollow">nf</a>' for url in nofollow_links]
    for index, alt in enumerate(image_alts):
        attr = "" if alt is None else f' alt="{alt}"'
        body.append(f'<img src="/i{index}.png"{attr}>')
    if words:
        body.append("<p>" + " ".join(["word"] * words) + "</p>")
    return (
        f'<!doctype html><html lang="{lang}"><head>{"".join(head)}</head>'
        f'<body>{"".join(body)}</body></html>'
    )


def html_response(body: str, *, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    merged = {"content-type": "text/html; charset=utf-8"}
    merged.update(headers or {})
    return httpx.Response(status, text=body, headers=merged)


def mock_site(pages: dict[str, str]) -> None:
    """Register one HTML route per path."""
    for path, body in pages.items():
        respx.get(f"{ORIGIN}{path}").mock(return_value=html_response(body))


async def crawl(*, start: str = f"{ORIGIN}/", robots: object = _DEFAULT, **limit_kw: object) -> SiteCrawlResult:
    rules = RobotsRules.permissive() if robots is _DEFAULT else robots
    limits = CrawlLimits(seed_from_sitemap=False, **limit_kw)  # type: ignore[arg-type]
    async with crawl_client() as client:
        return await collect_site_crawl(client, start, robots=rules, limits=limits)  # type: ignore[arg-type]


def by_url(result: SiteCrawlResult) -> dict[str, object]:
    return {page.url: page for page in result.pages}


# --- walking the site -----------------------------------------------------------------


@respx.mock
async def test_it_follows_internal_links_and_reports_every_page_once() -> None:
    mock_site({
        "/": page_html(links=("/a", "/b")),
        "/a": page_html(links=("/c",)),
        "/b": page_html(),
        "/c": page_html(),
    })
    result = await crawl()
    assert sorted(by_url(result)) == [f"{ORIGIN}/", f"{ORIGIN}/a", f"{ORIGIN}/b", f"{ORIGIN}/c"]
    assert result.stats.fetched == 4
    assert result.stats.ok == 4
    assert all(page.ok for page in result.pages)


@respx.mock
async def test_depth_is_recorded_from_the_seed() -> None:
    mock_site({"/": page_html(links=("/a",)), "/a": page_html(links=("/b",)), "/b": page_html()})
    pages = by_url(await crawl())
    assert pages[f"{ORIGIN}/"].depth == 0
    assert pages[f"{ORIGIN}/a"].depth == 1
    assert pages[f"{ORIGIN}/b"].depth == 2
    # The link that put each page in the frontier is kept, so a report can say
    # how the page is reached.
    assert pages[f"{ORIGIN}/b"].discovered_from == f"{ORIGIN}/a"
    assert pages[f"{ORIGIN}/"].discovered_from == ""


@respx.mock
async def test_a_link_cycle_terminates() -> None:
    """Two pages linking to each other and to themselves must not loop forever."""
    mock_site({"/": page_html(links=("/a", "/")), "/a": page_html(links=("/", "/a"))})
    result = await crawl()
    assert result.stats.fetched == 2


@respx.mock
async def test_urls_differing_only_by_tracking_params_are_fetched_once() -> None:
    route = respx.get(f"{ORIGIN}/a").mock(return_value=html_response(page_html()))
    respx.get(f"{ORIGIN}/").mock(return_value=html_response(
        page_html(links=("/a", "/a?utm_source=newsletter", "/a#reviews"))
    ))
    result = await crawl()
    assert route.call_count == 1
    assert result.stats.fetched == 2


# --- bounds ---------------------------------------------------------------------------


@respx.mock
async def test_max_pages_is_a_hard_ceiling_not_a_target() -> None:
    mock_site({"/": page_html(links=tuple(f"/p{i}" for i in range(10)))})
    for i in range(10):
        respx.get(f"{ORIGIN}/p{i}").mock(return_value=html_response(page_html()))
    result = await crawl(max_pages=4, concurrency=4)
    assert result.stats.fetched == 4
    assert result.stats.hit_page_budget is True
    assert result.stats.aborted_reason == "page budget"
    # The URLs it never got to are counted, not silently forgotten.
    assert result.stats.not_attempted > 0


@respx.mock
async def test_max_depth_stops_the_walk_and_counts_what_is_beyond_it() -> None:
    mock_site({"/": page_html(links=("/a",)), "/a": page_html(links=("/b",)), "/b": page_html()})
    result = await crawl(max_depth=1)
    assert sorted(by_url(result)) == [f"{ORIGIN}/", f"{ORIGIN}/a"]
    assert result.stats.beyond_depth == 1
    assert result.stats.max_depth_seen == 1


@respx.mock
async def test_max_depth_zero_crawls_only_the_seed() -> None:
    mock_site({"/": page_html(links=("/a", "/b"))})
    result = await crawl(max_depth=0)
    assert result.stats.fetched == 1
    assert result.stats.beyond_depth == 2


@respx.mock
async def test_an_expired_deadline_stops_the_crawl_before_any_fetch() -> None:
    mock_site({"/": page_html()})
    result = await crawl(deadline_s=0.0)
    assert result.stats.fetched == 0
    assert result.stats.deadline_hit is True
    assert result.stats.aborted_reason == "deadline"


@respx.mock
async def test_the_per_page_link_cap_limits_what_enters_the_frontier() -> None:
    mock_site({"/": page_html(links=tuple(f"/p{i}" for i in range(6)))})
    for i in range(6):
        respx.get(f"{ORIGIN}/p{i}").mock(return_value=html_response(page_html()))
    result = await crawl(max_links_per_page=2)
    assert result.stats.fetched == 3  # the seed plus two links
    assert by_url(result)[f"{ORIGIN}/"].outlinks_dropped == 4


# --- robots -----------------------------------------------------------------------------


@respx.mock
async def test_robots_disallow_skips_the_path_and_says_so() -> None:
    respx.get(f"{ORIGIN}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    mock_site({"/": page_html(links=("/public", "/private/secret")), "/public": page_html()})
    # robots=None makes the crawl fetch and honour robots.txt itself.
    result = await crawl(robots=None)
    pages = by_url(result)
    assert pages[f"{ORIGIN}/private/secret"].skipped == "robots"
    assert pages[f"{ORIGIN}/private/secret"].status_code is None  # never requested
    assert pages[f"{ORIGIN}/public"].ok
    assert result.stats.skipped_by_robots == 1
    assert result.stats.fetched == 2


@respx.mock
async def test_a_wildcard_disallow_that_only_matches_with_the_query_string() -> None:
    """Shopify ships `Disallow: /*?*oseid=*`; matching the bare path would allow it."""
    respx.get(f"{ORIGIN}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /*?*oseid=*\n")
    )
    mock_site({"/": page_html(links=("/item?oseid=abc", "/item")), "/item": page_html()})
    result = await crawl(robots=None)
    assert by_url(result)[f"{ORIGIN}/item?oseid=abc"].skipped == "robots"
    assert by_url(result)[f"{ORIGIN}/item"].ok


@respx.mock
async def test_a_disallowed_start_url_is_reported_rather_than_crawled() -> None:
    respx.get(f"{ORIGIN}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    result = await crawl(robots=None)
    assert result.stats.fetched == 0
    assert result.stats.skipped_by_robots == 1
    assert "start URL is disallowed" in " ".join(result.stats.notes)


def test_robots_target_includes_the_query_string() -> None:
    assert robots_target("https://site.test/a") == "/a"
    assert robots_target("https://site.test/a?b=1") == "/a?b=1"
    assert robots_target("https://site.test") == "/"


# --- what is not a page ------------------------------------------------------------------


@respx.mock
async def test_asset_links_are_skipped_without_a_request() -> None:
    mock_site({"/": page_html(links=("/logo.png", "/report.pdf", "/a")), "/a": page_html()})
    result = await crawl()
    pages = by_url(result)
    assert pages[f"{ORIGIN}/logo.png"].skipped == "asset"
    assert pages[f"{ORIGIN}/report.pdf"].skipped == "asset"
    assert result.stats.skipped_asset == 2
    assert result.stats.fetched == 2  # only / and /a cost a request


@respx.mock
async def test_a_non_html_content_type_is_fetched_once_then_dropped() -> None:
    respx.get(f"{ORIGIN}/").mock(return_value=html_response(page_html(links=("/download",))))
    respx.get(f"{ORIGIN}/download").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 ...", headers={"content-type": "application/pdf"})
    )
    result = await crawl()
    page = by_url(result)[f"{ORIGIN}/download"]
    assert page.skipped == "non_html"
    assert page.status_code == 200
    assert page.word_count == 0  # never parsed as HTML
    assert result.stats.skipped_non_html == 1


@respx.mock
async def test_external_links_are_counted_but_never_fetched() -> None:
    respx.get(f"{ORIGIN}/").mock(return_value=html_response(
        page_html(links=("/a", "https://other.test/x", "https://other.test/y"))
    ))
    respx.get(f"{ORIGIN}/a").mock(return_value=html_response(page_html()))
    result = await crawl()
    seed = by_url(result)[f"{ORIGIN}/"]
    assert seed.external_links == 2
    assert seed.internal_links == 1
    assert sorted(by_url(result)) == [f"{ORIGIN}/", f"{ORIGIN}/a"]


@respx.mock
async def test_a_redirect_off_the_site_is_recorded_but_not_followed_further() -> None:
    respx.get(f"{ORIGIN}/").mock(return_value=html_response(page_html(links=("/away",))))
    respx.get(f"{ORIGIN}/away").mock(
        return_value=httpx.Response(302, headers={"location": "https://other.test/landing"})
    )
    respx.get("https://other.test/landing").mock(
        return_value=html_response(page_html(links=("https://other.test/deeper",)))
    )
    result = await crawl()
    page = by_url(result)[f"{ORIGIN}/away"]
    assert page.skipped == "off_site"
    assert page.final_url == "https://other.test/landing"
    assert page.redirect_chain == [f"{ORIGIN}/away"]
    assert page.outlinks == []
    assert result.stats.skipped_off_site == 1


# --- failure handling ---------------------------------------------------------------------


@respx.mock
async def test_one_failing_page_does_not_stop_the_others() -> None:
    respx.get(f"{ORIGIN}/").mock(return_value=html_response(page_html(links=("/bad", "/good"))))
    respx.get(f"{ORIGIN}/bad").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{ORIGIN}/good").mock(return_value=html_response(page_html()))
    result = await crawl()
    pages = by_url(result)
    assert pages[f"{ORIGIN}/good"].ok
    assert pages[f"{ORIGIN}/bad"].error
    assert not pages[f"{ORIGIN}/bad"].ok
    assert result.stats.transport_errors == 1
    assert result.stats.retries == 1  # a transport failure is retried exactly once


@respx.mock
async def test_status_codes_are_bucketed_for_the_report() -> None:
    respx.get(f"{ORIGIN}/").mock(return_value=html_response(page_html(links=("/gone", "/broken"))))
    respx.get(f"{ORIGIN}/gone").mock(return_value=httpx.Response(404, text="nope"))
    respx.get(f"{ORIGIN}/broken").mock(return_value=httpx.Response(500, text="boom"))
    result = await crawl()
    assert by_url(result)[f"{ORIGIN}/gone"].status_code == 404
    assert by_url(result)[f"{ORIGIN}/broken"].status_code == 500
    assert result.stats.client_errors == 1
    assert result.stats.server_errors == 1
    assert result.stats.ok == 1


@respx.mock
async def test_repeated_429s_abort_the_crawl() -> None:
    limit = CONSECUTIVE_429_ABORT
    mock_site({"/": page_html(links=tuple(f"/p{i}" for i in range(limit)))})
    for i in range(limit):
        respx.get(f"{ORIGIN}/p{i}").mock(return_value=httpx.Response(429, text="slow down"))
    result = await crawl(concurrency=limit)
    assert result.stats.aborted_reason == f"{limit} consecutive 429s"


@respx.mock
async def test_a_seed_that_is_not_an_http_url_is_a_caller_error() -> None:
    with pytest.raises(FetchError):
        await crawl(start="mailto:hi@site.test")


# --- page facts ----------------------------------------------------------------------------


@respx.mock
async def test_the_seo_facts_of_a_page_are_extracted() -> None:
    mock_site({"/": page_html(
        title="Linen Shirts",
        description="Soft linen shirts, made to last.",
        canonical=f"{ORIGIN}/",
        h1s=("Linen Shirts",),
        h2s=3,
        words=42,
        image_alts=("a model wearing the shirt", "", None),
    )})
    page = by_url(await crawl())[f"{ORIGIN}/"]
    assert page.title == "Linen Shirts"
    assert page.meta_description == "Soft linen shirts, made to last."
    assert page.canonical_url == f"{ORIGIN}/"
    assert page.lang == "en"
    assert page.h1 == ["Linen Shirts"] and page.h1_count == 1
    assert page.h2_count == 3
    assert page.word_count >= 42
    assert page.images == 3
    assert page.images_missing_alt == 2  # a blank alt and a missing one
    assert page.indexable is True


@respx.mock
async def test_a_noindex_meta_tag_makes_a_page_non_indexable() -> None:
    mock_site({"/": page_html(robots="noindex, follow")})
    page = by_url(await crawl())[f"{ORIGIN}/"]
    assert page.noindex is True
    assert page.nofollow is False
    assert page.ok is True
    assert page.indexable is False


@respx.mock
async def test_an_x_robots_tag_header_is_honoured_like_the_meta_tag() -> None:
    """The header is invisible in the markup, so a crawl that ignored it would lie."""
    respx.get(f"{ORIGIN}/").mock(
        return_value=html_response(page_html(), headers={"x-robots-tag": "noindex"})
    )
    page = by_url(await crawl())[f"{ORIGIN}/"]
    assert page.noindex is True
    assert page.indexable is False


@respx.mock
async def test_a_nofollow_page_records_its_links_without_walking_them() -> None:
    mock_site({"/": page_html(robots="nofollow", links=("/a",)), "/a": page_html()})
    result = await crawl()
    seed = by_url(result)[f"{ORIGIN}/"]
    assert seed.nofollow is True
    assert seed.outlinks == [f"{ORIGIN}/a"]  # the graph edge is still reported
    assert result.stats.fetched == 1  # but /a was never requested


@respx.mock
async def test_nofollow_links_are_counted_and_not_followed() -> None:
    mock_site({"/": page_html(links=("/a",), nofollow_links=("/sponsored",)), "/a": page_html()})
    result = await crawl()
    seed = by_url(result)[f"{ORIGIN}/"]
    assert seed.internal_nofollow_links == 1
    assert seed.internal_links == 1
    assert f"{ORIGIN}/sponsored" not in by_url(result)


def test_extract_page_facts_caps_the_h1_list_but_not_the_count() -> None:
    dom = parse_html("<html><body>" + "".join(f"<h1>H{i}</h1>" for i in range(12)) + "</body></html>")
    facts = extract_page_facts(dom)
    assert facts["h1_count"] == 12
    assert len(facts["h1"]) == 5  # MAX_H1_KEPT - the finding is "more than one"


def test_none_in_a_robots_directive_means_both_noindex_and_nofollow() -> None:
    dom = parse_html('<html><head><meta name="robots" content="none"></head><body></body></html>')
    facts = extract_page_facts(dom)
    assert facts["noindex"] is True and facts["nofollow"] is True


# --- sitemap seeding and orphans ------------------------------------------------------------


SITEMAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{ORIGIN}/</loc></url>
  <url><loc>{ORIGIN}/a</loc></url>
  <url><loc>{ORIGIN}/orphan</loc></url>
  <url><loc>https://other.test/evil</loc></url>
</urlset>"""


@respx.mock
async def test_sitemap_seeding_finds_a_page_nothing_links_to() -> None:
    respx.get(f"{ORIGIN}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP_XML, headers={"content-type": "application/xml"})
    )
    mock_site({"/": page_html(links=("/a",)), "/a": page_html(), "/orphan": page_html()})
    limits = CrawlLimits(seed_from_sitemap=True)
    async with crawl_client() as client:
        result = await collect_site_crawl(client, f"{ORIGIN}/", robots=RobotsRules.permissive(), limits=limits)
    assert result.stats.seeded_from_sitemap == 2  # /a and /orphan; the seed was already queued
    assert f"{ORIGIN}/orphan" in by_url(result)
    # The off-site <loc> is never crawled: a document the host controls must not
    # be able to aim the crawler at a new origin.
    assert "https://other.test/evil" not in by_url(result)
    assert result.orphans() == [f"{ORIGIN}/orphan"]


@respx.mock
async def test_a_sitemap_that_is_really_an_html_404_page_seeds_nothing() -> None:
    respx.get(f"{ORIGIN}/sitemap.xml").mock(return_value=html_response("<html><body>Not found</body></html>"))
    mock_site({"/": page_html()})
    limits = CrawlLimits(seed_from_sitemap=True)
    async with crawl_client() as client:
        result = await collect_site_crawl(client, f"{ORIGIN}/", robots=RobotsRules.permissive(), limits=limits)
    assert result.stats.seeded_from_sitemap == 0
    assert result.stats.fetched == 1


@respx.mock
async def test_indexable_pages_excludes_noindex_and_errors() -> None:
    mock_site({
        "/": page_html(links=("/a", "/hidden", "/gone")),
        "/a": page_html(),
        "/hidden": page_html(robots="noindex"),
    })
    respx.get(f"{ORIGIN}/gone").mock(return_value=httpx.Response(404, text="nope"))
    result = await crawl()
    assert sorted(page.url for page in result.indexable_pages) == [f"{ORIGIN}/", f"{ORIGIN}/a"]


# --- sitemap helpers --------------------------------------------------------------------


def test_looks_like_sitemap_tells_a_sitemap_from_a_themed_404_page() -> None:
    assert looks_like_sitemap('<?xml version="1.0"?><urlset><url><loc>https://s/a</loc></url></urlset>')
    assert looks_like_sitemap('<?xml version="1.0"?><sitemapindex><sitemap><loc>https://s/s.xml</loc></sitemap></sitemapindex>')
    # Status 200 with an HTML body is what python.org actually serves at
    # /sitemap.xml, so this distinction is load-bearing, not theoretical.
    assert not looks_like_sitemap("<!doctype html><html><body><h1>Page not found</h1></body></html>")


def test_is_sitemap_url_flags_nested_documents_not_pages() -> None:
    assert is_sitemap_url("https://s/sitemap_products_1.xml")
    assert is_sitemap_url("https://s/sitemap.xml.gz")
    assert not is_sitemap_url("https://s/products/linen-shirt")


# --- limits from settings ----------------------------------------------------------------


def test_limits_are_built_from_the_site_crawl_settings() -> None:
    """The adapter lives on the crawler side so `config` never imports `services`."""
    limits = CrawlLimits.from_settings(get_settings())
    settings = get_settings()
    assert limits.max_pages == settings.site_crawl_max_pages
    assert limits.max_depth == settings.site_crawl_max_depth
    assert limits.concurrency == settings.site_crawl_concurrency
    assert limits.deadline_s == float(settings.site_crawl_deadline_s)


def test_an_override_beats_the_configured_value() -> None:
    limits = CrawlLimits.from_settings(get_settings(), max_pages=7)
    assert limits.max_pages == 7


def test_from_settings_falls_back_to_defaults_for_an_object_with_no_site_crawl_keys() -> None:
    assert CrawlLimits.from_settings(object()) == CrawlLimits()


def test_nonsense_bounds_are_clamped_rather_than_trusted() -> None:
    """0 pages is the "all pages" option now, so only the others are floored."""
    limits = CrawlLimits(max_pages=-3, max_depth=-3, concurrency=0)
    assert (limits.max_pages, limits.max_depth, limits.concurrency) == (0, 0, 1)
    assert CrawlLimits(max_pages=0).page_budget is None


# --- page-type filter ---------------------------------------------------------------------


def _catalog_site() -> dict[str, str]:
    """A store shaped like Shopify: home -> collections -> products, plus a blog."""
    return {
        "/": page_html(links=("/collections/summer", "/collections/winter", "/blogs/news/hello", "/pages/about")),
        "/collections/summer": page_html(links=("/products/linen-shirt", "/products/wool-scarf")),
        "/collections/winter": page_html(links=("/products/parka",)),
        "/products/linen-shirt": page_html(title="Linen Shirt"),
        "/products/wool-scarf": page_html(title="Wool Scarf"),
        "/products/parka": page_html(title="Parka"),
        "/blogs/news/hello": page_html(title="Hello"),
        "/pages/about": page_html(title="About"),
    }


def _targets(result: SiteCrawlResult) -> list[str]:
    return sorted(p.url for p in result.pages if p.is_target and p.fetched)


@respx.mock
async def test_products_only_returns_product_pages_and_nothing_else() -> None:
    mock_site(_catalog_site())
    result = await crawl(page_types=("product",))
    assert _targets(result) == [
        f"{ORIGIN}/products/linen-shirt", f"{ORIGIN}/products/parka", f"{ORIGIN}/products/wool-scarf",
    ]
    assert result.stats.targets == 3


@respx.mock
async def test_collections_only_returns_collection_pages() -> None:
    mock_site(_catalog_site())
    result = await crawl(page_types=("collection",))
    assert _targets(result) == [f"{ORIGIN}/collections/summer", f"{ORIGIN}/collections/winter"]
    assert result.stats.targets == 2


@respx.mock
async def test_both_types_returns_products_and_collections_together() -> None:
    mock_site(_catalog_site())
    result = await crawl(page_types=("product", "collection"))
    assert _targets(result) == [
        f"{ORIGIN}/collections/summer", f"{ORIGIN}/collections/winter",
        f"{ORIGIN}/products/linen-shirt", f"{ORIGIN}/products/parka", f"{ORIGIN}/products/wool-scarf",
    ]
    assert result.stats.targets == 5


@respx.mock
async def test_no_filter_still_crawls_everything() -> None:
    """The default must be exactly the old behaviour."""
    mock_site(_catalog_site())
    result = await crawl()
    assert len(_targets(result)) == 8
    assert result.stats.navigation_pages == 0


@respx.mock
async def test_the_pages_needed_to_reach_the_products_are_fetched_but_not_counted() -> None:
    """The home page and the collection listings are the map, not the answer."""
    mock_site(_catalog_site())
    result = await crawl(page_types=("product",))
    navigation = sorted(p.url for p in result.pages if not p.is_target and p.fetched)
    assert f"{ORIGIN}/" in navigation
    assert f"{ORIGIN}/collections/summer" in navigation
    assert result.stats.navigation_pages == len(navigation)
    # Navigation never eats the page budget, which is reserved for the answer.
    assert result.stats.targets == 3


@respx.mock
async def test_the_page_budget_applies_to_targets_only() -> None:
    mock_site(_catalog_site())
    result = await crawl(page_types=("product",), max_pages=2)
    assert result.stats.targets == 2
    assert result.stats.hit_page_budget is True


@respx.mock
async def test_a_navigation_budget_of_zero_refuses_every_other_page() -> None:
    """With the detour switched off, only URLs already known to match are fetched."""
    mock_site(_catalog_site())
    result = await crawl(page_types=("product",), max_navigation_pages=0)
    assert result.stats.fetched == 0  # the seed itself is not a product page
    assert result.stats.skipped_page_type >= 1
    assert by_url(result)[f"{ORIGIN}/"].skipped == "page_type"


@respx.mock
async def test_running_out_of_navigation_budget_does_not_stop_the_crawl() -> None:
    mock_site(_catalog_site())
    result = await crawl(page_types=("product",), max_navigation_pages=1, concurrency=1)
    assert result.stats.hit_navigation_budget is True
    assert result.stats.aborted_reason == ""  # a detour ended, not the crawl


@respx.mock
async def test_the_page_type_is_recorded_on_every_page() -> None:
    mock_site(_catalog_site())
    pages = by_url(await crawl())
    assert pages[f"{ORIGIN}/products/parka"].page_type == "product"
    assert pages[f"{ORIGIN}/collections/summer"].page_type == "collection"
    assert pages[f"{ORIGIN}/pages/about"].page_type == ""


@respx.mock
async def test_only_target_pages_are_handed_to_the_analyzer_hook() -> None:
    """Scoring a page nobody asked for would spend CPU and skew the average."""
    mock_site(_catalog_site())
    scored: list[str] = []

    async def hook(page, page_data):
        scored.append(page.url)
        page.score = 90.0

    limits = CrawlLimits(seed_from_sitemap=False, page_types=("product",))
    async with crawl_client() as client:
        result = await collect_site_crawl(
            client, f"{ORIGIN}/", robots=RobotsRules.permissive(), limits=limits, page_hook=hook
        )
    assert sorted(scored) == [
        f"{ORIGIN}/products/linen-shirt", f"{ORIGIN}/products/parka", f"{ORIGIN}/products/wool-scarf",
    ]
    assert all(p.score is None for p in result.pages if not p.is_target)


def test_virtual_shopify_collections_are_not_collection_pages() -> None:
    """/collections/all is a view over the whole catalog, not a collection."""
    from services.crawler.urls import classify_catalog_url

    assert classify_catalog_url("https://s/collections/all") is None
    assert classify_catalog_url("https://s/collections/summer") == "collection"
    assert classify_catalog_url("https://s/products/linen-shirt") == "product"
    assert classify_catalog_url("https://s/collections/summer/products/x") is None


def test_wants_treats_an_empty_filter_as_everything() -> None:
    assert CrawlLimits().wants("product") and CrawlLimits().wants(None)
    only_products = CrawlLimits(page_types=("product",))
    assert only_products.wants("product")
    assert not only_products.wants("collection") and not only_products.wants(None)


@respx.mock
async def test_a_blocked_page_outside_the_filter_stays_out_of_the_answer() -> None:
    """Regression: a robots-blocked /account page showed up in a products-only crawl."""
    respx.get(f"{ORIGIN}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *" + chr(10) + "Disallow: /account" + chr(10))
    )
    mock_site({
        "/": page_html(links=("/account", "/products/linen-shirt")),
        "/products/linen-shirt": page_html(title="Linen Shirt"),
    })
    result = await crawl(robots=None, page_types=("product",))
    account = by_url(result)[f"{ORIGIN}/account"]
    assert account.skipped == "robots"
    assert account.is_target is False  # it was never a product page
    assert account.page_type == ""


@respx.mock
async def test_a_blocked_product_page_stays_in_the_answer() -> None:
    """The other half: we asked for it and could not have it, so it is a finding."""
    respx.get(f"{ORIGIN}/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *" + chr(10) + "Disallow: /products/secret" + chr(10)
        )
    )
    mock_site({
        "/": page_html(links=("/products/secret", "/products/linen-shirt")),
        "/products/linen-shirt": page_html(title="Linen Shirt"),
    })
    result = await crawl(robots=None, page_types=("product",))
    secret = by_url(result)[f"{ORIGIN}/products/secret"]
    assert secret.skipped == "robots"
    assert secret.is_target is True
    assert secret.page_type == "product"


@respx.mock
async def test_skipped_and_asset_records_are_classified_without_a_filter() -> None:
    mock_site({"/": page_html(links=("/logo.png",))})
    page = by_url(await crawl())[f"{ORIGIN}/logo.png"]
    assert page.skipped == "asset"
    assert page.is_target is True  # no filter means everything was asked for


# --- no page limit -------------------------------------------------------------------------


@respx.mock
async def test_max_pages_zero_crawls_every_page_it_can_reach() -> None:
    """0 is the "all pages" option: the deadline and the depth are the bounds."""
    mock_site({
        "/": page_html(links=tuple(f"/p{i}" for i in range(12))),
        **{f"/p{i}": page_html() for i in range(12)},
    })
    result = await crawl(max_pages=0)
    assert result.stats.fetched == 13  # the seed plus every link
    assert result.stats.hit_page_budget is False
    assert result.stats.aborted_reason == ""


@respx.mock
async def test_without_a_page_limit_the_depth_limit_still_binds() -> None:
    mock_site({"/": page_html(links=("/a",)), "/a": page_html(links=("/b",)), "/b": page_html()})
    result = await crawl(max_pages=0, max_depth=1)
    assert result.stats.fetched == 2
    assert result.stats.beyond_depth == 1


@respx.mock
async def test_without_a_page_limit_the_deadline_still_binds() -> None:
    mock_site({"/": page_html()})
    result = await crawl(max_pages=0, deadline_s=0.0)
    assert result.stats.fetched == 0
    assert result.stats.deadline_hit is True


def test_page_budget_reads_zero_as_no_limit() -> None:
    assert CrawlLimits(max_pages=0).page_budget is None
    assert CrawlLimits(max_pages=250).page_budget == 250
