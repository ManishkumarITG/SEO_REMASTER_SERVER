"""search_console analyzer: property matching, skip paths, finding construction.

No network: property matching and report building are pure, and the skip paths
return before any Google call.
"""

from __future__ import annotations

from uuid import uuid4

from db.session import init_db
from services.analyzers.base import Severity
from services.analyzers.search_console import (
    SearchConsoleAnalyzer,
    default_range,
    match_property,
)
from tests.factories import GOOD_HTML, make_page

PROPS = [
    "sc-domain:example.com",
    "https://example.com/",
    "https://example.com/blog/",
    "https://other.com/",
]


def test_matches_most_specific_url_prefix_property() -> None:
    assert match_property(PROPS, "https://example.com/blog/post") == "https://example.com/blog/"
    assert match_property(PROPS, "https://example.com/pricing") == "https://example.com/"


def test_falls_back_to_domain_property() -> None:
    # Only a domain property covers this subdomain.
    assert match_property(PROPS, "https://shop.example.com/x") == "sc-domain:example.com"


def test_www_is_normalized() -> None:
    assert match_property(["https://example.com/"], "https://www.example.com/page") == "https://example.com/"
    assert match_property(["https://www.example.com/"], "https://example.com/page") == "https://www.example.com/"


def test_unrelated_host_matches_nothing() -> None:
    assert match_property(PROPS, "https://unrelated.dev/page") is None
    assert match_property([], "https://example.com/") is None


def test_default_range_is_28_days_ending_two_days_ago() -> None:
    from datetime import date

    start, end = default_range()
    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 27
    assert (date.today() - date.fromisoformat(end)).days == 2


async def test_skips_without_user_session() -> None:
    result = await SearchConsoleAnalyzer().analyze(make_page(GOOD_HTML))
    assert result.error is None
    assert result.score is None  # excluded from the weighted overall score
    assert result.findings[0].id == "search_console.no_session"
    assert result.findings[0].severity == Severity.INFO


async def test_skips_when_google_not_connected() -> None:
    await init_db()
    page = make_page(GOOD_HTML)
    page.user_id = str(uuid4())  # a session with no stored Google credential
    result = await SearchConsoleAnalyzer().analyze(page)
    assert result.error is None
    assert result.score is None
    assert result.findings[0].id == "search_console.not_connected"


def _build(totals, queries):
    return SearchConsoleAnalyzer()._build_result(
        "https://example.com/", "https://example.com/page", "2026-08-04", "2026-08-31", totals, queries
    )


def test_no_data_for_url_is_flagged() -> None:
    result = _build([], [])
    assert result.findings[0].id == "search_console.no_data"
    assert result.findings[0].severity == Severity.WARNING
    assert result.score is not None and result.score < 100


def test_summary_and_top_queries_are_reported() -> None:
    totals = [{"keys": ["https://example.com/page"], "clicks": 120, "impressions": 4000, "ctr": 0.03, "position": 9.4}]
    queries = [
        {"keys": ["https://example.com/page", "blue widgets"], "clicks": 90, "impressions": 1000, "ctr": 0.09, "position": 3.1},
        {"keys": ["https://example.com/page", "red widgets"], "clicks": 30, "impressions": 900, "ctr": 0.033, "position": 6.0},
    ]
    by_id = {f.id: f for f in _build(totals, queries).findings}
    summary = by_id["search_console.summary"]
    assert summary.severity == Severity.INFO
    assert summary.evidence is not None
    assert summary.evidence["clicks"] == 120
    assert summary.evidence["matched_property"] == "https://example.com/"
    assert [q["query"] for q in by_id["search_console.top_queries"].evidence["queries"]] == [
        "blue widgets",
        "red widgets",
    ]


def test_low_ctr_queries_are_flagged() -> None:
    totals = [{"keys": ["p"], "clicks": 5, "impressions": 3000, "ctr": 0.001, "position": 8.0}]
    queries = [
        # ranks well, plenty of impressions, almost no clicks -> CTR problem
        {"keys": ["p", "widget shop"], "clicks": 2, "impressions": 2000, "ctr": 0.001, "position": 7.0},
        # too few impressions to judge
        {"keys": ["p", "rare term"], "clicks": 0, "impressions": 12, "ctr": 0.0, "position": 5.0},
    ]
    by_id = {f.id: f for f in _build(totals, queries).findings}
    low = by_id["search_console.low_ctr"]
    assert low.severity == Severity.WARNING
    assert [q["query"] for q in low.evidence["queries"]] == ["widget shop"]


def test_striking_distance_queries_are_flagged() -> None:
    totals = [{"keys": ["p"], "clicks": 10, "impressions": 900, "ctr": 0.011, "position": 14.0}]
    queries = [
        {"keys": ["p", "page two term"], "clicks": 1, "impressions": 400, "ctr": 0.0025, "position": 13.2},
        {"keys": ["p", "already ranking"], "clicks": 9, "impressions": 300, "ctr": 0.03, "position": 2.0},
    ]
    by_id = {f.id: f for f in _build(totals, queries).findings}
    striking = by_id["search_console.striking_distance"]
    assert striking.severity == Severity.WARNING
    assert [q["query"] for q in striking.evidence["queries"]] == ["page two term"]
