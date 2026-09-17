"""on_page analyzer: a well-formed page passes, a broken page gets flagged."""

from __future__ import annotations

from services.analyzers.base import Severity
from services.analyzers.on_page import OnPageAnalyzer
from tests.factories import BROKEN_HTML, GOOD_HTML, make_page


async def test_well_formed_page_scores_high() -> None:
    result = await OnPageAnalyzer().analyze(make_page(GOOD_HTML))
    assert result.error is None
    assert result.analyzer == "on_page"
    assert result.score is not None and result.score >= 90
    problems = [f for f in result.findings if f.severity in (Severity.CRITICAL, Severity.WARNING)]
    assert problems == []


async def test_well_formed_page_specific_checks() -> None:
    result = await OnPageAnalyzer().analyze(make_page(GOOD_HTML))
    by_id = {f.id: f for f in result.findings}
    for check in (
        "on_page.title",
        "on_page.meta_description",
        "on_page.h1",
        "on_page.heading_hierarchy",
        "on_page.image_alt",
        "on_page.canonical",
        "on_page.open_graph",
        "on_page.word_count",
        "on_page.lang",
    ):
        assert by_id[check].severity == Severity.PASSED, check
    assert by_id["on_page.links"].evidence == {"internal": 2, "external": 1}


async def test_broken_page_flags_issues() -> None:
    result = await OnPageAnalyzer().analyze(make_page(BROKEN_HTML))
    assert result.error is None
    assert result.score is not None and result.score < 50
    by_id = {f.id: f for f in result.findings}
    assert by_id["on_page.title"].severity == Severity.CRITICAL
    assert by_id["on_page.h1"].severity == Severity.CRITICAL
    assert by_id["on_page.meta_description"].severity == Severity.WARNING
    assert by_id["on_page.heading_hierarchy"].severity == Severity.WARNING
    assert by_id["on_page.image_alt"].severity == Severity.WARNING
    assert by_id["on_page.word_count"].severity == Severity.WARNING
    assert by_id["on_page.lang"].severity == Severity.WARNING


async def test_analyzer_never_raises_on_empty_html() -> None:
    result = await OnPageAnalyzer().analyze(make_page(""))
    assert result.analyzer == "on_page"  # an empty page is still a result, not an exception
