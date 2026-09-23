"""URL normalisation and link extraction: the dedupe rules that make a crawl terminate. No network."""

from __future__ import annotations

from services.crawler.parser import parse_html
from services.crawler.urls import (
    MAX_LINKS_PER_PAGE,
    extract_links,
    host_of,
    is_probably_asset,
    normalize_url,
    same_site,
)

HOST = "site.test"
BASE = "https://site.test/blog/post"


def test_fragment_and_tracking_params_are_dropped() -> None:
    assert normalize_url("https://site.test/a#anywhere") == "https://site.test/a"
    assert normalize_url("https://site.test/a?utm_source=x&utm_medium=y") == "https://site.test/a"
    assert normalize_url("https://site.test/a?gclid=1&fbclid=2&msclkid=3") == "https://site.test/a"


def test_meaningful_query_params_survive_and_are_sorted() -> None:
    """A query string can select content, so it is kept - but in one stable order."""
    assert normalize_url("https://site.test/s?page=2") == "https://site.test/s?page=2"
    assert normalize_url("https://site.test/s?b=2&a=1") == normalize_url("https://site.test/s?a=1&b=2")
    # Tracking is stripped without taking the real parameter with it.
    assert normalize_url("https://site.test/s?page=2&utm_source=x") == "https://site.test/s?page=2"


def test_host_case_and_default_ports_are_normalised() -> None:
    assert normalize_url("HTTPS://Site.TEST/a") == "https://site.test/a"
    assert normalize_url("https://site.test:443/a") == "https://site.test/a"
    assert normalize_url("http://site.test:80/a") == "http://site.test/a"
    # A non-default port is part of the origin and must not be discarded.
    assert normalize_url("http://site.test:8080/a") == "http://site.test:8080/a"


def test_an_empty_path_becomes_root() -> None:
    assert normalize_url("https://site.test") == "https://site.test/"


def test_a_trailing_slash_is_preserved_because_it_can_be_a_different_document() -> None:
    assert normalize_url("https://site.test/a") != normalize_url("https://site.test/a/")


def test_non_http_schemes_and_junk_are_rejected() -> None:
    for href in ("mailto:hi@site.test", "tel:+100", "javascript:void(0)", "data:text/html,x",
                 "ftp://site.test/a", "#", "", "   "):
        assert normalize_url(href) is None, href


def test_protocol_relative_hrefs_inherit_the_scheme_of_the_base() -> None:
    """`//host/path` is common in older templates and must not be read as a path."""
    assert normalize_url("//site.test/a", BASE) == "https://site.test/a"
    assert normalize_url("//other.test/a", BASE) == "https://other.test/a"
    # Without a base there is no scheme to inherit, so it is not a fetchable URL.
    assert normalize_url("//site.test/a") is None


def test_a_protocol_relative_link_is_internal_when_the_host_matches() -> None:
    dom = _dom('<a href="//site.test/a">a</a><a href="//other.test/b">b</a>')
    scan = extract_links(dom, BASE, HOST)
    assert scan.internal == ["https://site.test/a"]
    assert scan.external == 1


def test_whitespace_around_an_href_is_ignored() -> None:
    assert normalize_url("  /about  ", BASE) == "https://site.test/about"


def test_relative_hrefs_resolve_against_the_base() -> None:
    assert normalize_url("../about", BASE) == "https://site.test/about"
    assert normalize_url("/about", BASE) == "https://site.test/about"
    assert normalize_url("next", BASE) == "https://site.test/blog/next"
    assert normalize_url("?page=2", BASE) == "https://site.test/blog/post?page=2"


def test_same_site_requires_an_exact_host_unless_subdomains_are_opted_in() -> None:
    assert same_site("https://site.test/a", HOST)
    assert not same_site("https://other.test/a", HOST)
    assert not same_site("https://blog.site.test/a", HOST)
    assert same_site("https://blog.site.test/a", HOST, include_subdomains=True)
    # A lookalike host must never pass the subdomain rule.
    assert not same_site("https://evilsite.test/a", HOST, include_subdomains=True)


def test_asset_paths_are_recognised_without_a_request() -> None:
    assert is_probably_asset("https://site.test/logo.png")
    assert is_probably_asset("https://site.test/doc.PDF")
    assert is_probably_asset("https://site.test/app.js?v=3")
    assert not is_probably_asset("https://site.test/about")
    # A dot in a directory name is not an extension.
    assert not is_probably_asset("https://site.test/v1.2/guide")


def test_host_of_is_lower_cased_and_safe_on_junk() -> None:
    assert host_of("https://Site.TEST/a") == "site.test"
    assert host_of("not a url") == ""


# --- link extraction -----------------------------------------------------------------


def _dom(body: str):
    return parse_html(f"<!doctype html><html><head><title>T</title></head><body>{body}</body></html>")


def test_links_are_split_into_internal_external_and_nofollow() -> None:
    dom = _dom(
        '<a href="/a">a</a>'
        '<a href="https://site.test/b">b</a>'
        '<a href="/c" rel="nofollow">c</a>'
        '<a href="https://other.test/x">x</a>'
        '<a href="https://other.test/y" rel="nofollow ugc">y</a>'
        '<a href="mailto:hi@site.test">mail</a>'
    )
    scan = extract_links(dom, "https://site.test/", HOST)
    assert scan.internal == ["https://site.test/a", "https://site.test/b"]
    assert scan.internal_nofollow == 1
    assert scan.external == 1
    assert scan.external_nofollow == 1
    assert scan.total == 5  # the mailto: is not a link to anywhere we could go


def test_urls_that_normalise_together_are_one_internal_link() -> None:
    """The whole point: three hrefs, one page to fetch."""
    dom = _dom('<a href="/a">1</a><a href="/a?utm_source=n">2</a><a href="/a#top">3</a>')
    scan = extract_links(dom, "https://site.test/", HOST)
    assert scan.internal == ["https://site.test/a"]


def test_follow_nofollow_promotes_nofollow_links_to_followable() -> None:
    dom = _dom('<a href="/a" rel="nofollow">a</a>')
    assert extract_links(dom, "https://site.test/", HOST).internal == []
    scan = extract_links(dom, "https://site.test/", HOST, follow_nofollow=True)
    assert scan.internal == ["https://site.test/a"]
    assert scan.internal_nofollow == 0


def test_a_base_href_changes_what_relative_links_resolve_to() -> None:
    dom = parse_html(
        '<html><head><base href="https://site.test/shop/"></head>'
        '<body><a href="item">i</a></body></html>'
    )
    scan = extract_links(dom, "https://site.test/other/page", HOST)
    assert scan.internal == ["https://site.test/shop/item"]


def test_the_per_page_link_cap_is_enforced_and_reported() -> None:
    dom = _dom("".join(f'<a href="/p{i}">{i}</a>' for i in range(MAX_LINKS_PER_PAGE + 25)))
    scan = extract_links(dom, "https://site.test/", HOST)
    assert len(scan.internal) == MAX_LINKS_PER_PAGE
    assert scan.dropped == 25


def test_subdomain_links_follow_the_include_subdomains_setting() -> None:
    dom = _dom('<a href="https://blog.site.test/post">p</a>')
    assert extract_links(dom, "https://site.test/", HOST).external == 1
    scan = extract_links(dom, "https://site.test/", HOST, include_subdomains=True)
    assert scan.internal == ["https://blog.site.test/post"]
