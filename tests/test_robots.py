"""robots.txt parsing and matching. Pure functions, no network."""

from __future__ import annotations

from services.crawler.robots import RobotsRules, parse_robots, path_allowed

ALLBIRDS_SHAPED = """
User-agent: *
Disallow: /collections/*sort_by*
Disallow: /*?*oseid=*
Disallow: /cart

User-agent: Nutch
Disallow: /

User-agent: AhrefsBot
Crawl-delay: 10
Disallow: /collections/*+*

User-agent: *
Disallow: /checkout
Sitemap: https://shop.test/sitemap.xml
"""


def _info() -> dict:
    return {"disallow": [], "allow": [], "sitemaps": [], "crawl_delay": None}


def test_star_group_rules_accumulate_across_repeated_star_blocks() -> None:
    info = _info()
    parse_robots(ALLBIRDS_SHAPED, info)
    assert info["disallow"] == ["/collections/*sort_by*", "/*?*oseid=*", "/cart", "/checkout"]
    assert info["sitemaps"] == ["https://shop.test/sitemap.xml"]


def test_a_named_seo_bots_crawl_delay_does_not_leak_to_us() -> None:
    info = _info()
    parse_robots(ALLBIRDS_SHAPED, info)
    assert info["crawl_delay"] is None


def test_a_blanket_disallow_aimed_at_another_bot_does_not_block_us() -> None:
    info = _info()
    parse_robots(ALLBIRDS_SHAPED, info)
    assert RobotsRules.from_info(info).blanket_disallow is False


def test_a_group_naming_our_bot_wins_over_star() -> None:
    info = _info()
    parse_robots("User-agent: *\nDisallow: /\nUser-agent: SEOAnalyzerBot\nCrawl-delay: 2\nDisallow: /private", info)
    assert info["disallow"] == ["/private"]
    assert info["crawl_delay"] == 2.0


def test_wildcard_rules_match_shopify_faceted_urls() -> None:
    disallow = ["/collections/*sort_by*", "/*?*oseid=*"]
    assert not path_allowed("/collections/all?sort_by=price", disallow, [])
    assert not path_allowed("/products/x?oseid=abc", disallow, [])
    assert path_allowed("/products/linen-shirt", disallow, [])
    assert path_allowed("/collections/summer", disallow, [])


def test_longest_match_wins_and_allow_beats_disallow_on_a_tie() -> None:
    assert path_allowed("/products/special", ["/products"], ["/products/special"])
    assert not path_allowed("/products/other", ["/products"], ["/products/special"])
    assert path_allowed("/x", ["/x"], ["/x"])


def test_dollar_anchors_the_end_of_the_path() -> None:
    assert not path_allowed("/file.pdf", ["/*.pdf$"], [])
    assert path_allowed("/file.pdfx", ["/*.pdf$"], [])


def test_a_bare_disallow_line_allows_everything() -> None:
    info = _info()
    parse_robots("User-agent: *\nDisallow:", info)
    assert info["disallow"] == []
    assert RobotsRules.from_info(info).allows("/anything")


def test_unreadable_robots_is_permissive() -> None:
    assert RobotsRules.permissive().allows("/products/x")
    assert RobotsRules.permissive().crawl_delay is None
