"""technical analyzer: redirects, noindex (meta + header), HTTPS."""

from __future__ import annotations

from services.analyzers.base import Severity
from services.analyzers.technical import TechnicalAnalyzer
from tests.factories import GOOD_HTML, make_page


def _finding(result, finding_id):
    return next(f for f in result.findings if f.id == finding_id)


async def test_clean_page_passes() -> None:
    result = await TechnicalAnalyzer().analyze(make_page(GOOD_HTML))
    assert result.error is None
    assert result.score is not None and result.score >= 90
    assert _finding(result, "technical.https").severity == Severity.PASSED
    assert _finding(result, "technical.noindex").severity == Severity.PASSED
    assert _finding(result, "technical.redirects").severity == Severity.PASSED
    assert _finding(result, "technical.status_code").severity == Severity.PASSED


async def test_redirect_chain_flagged() -> None:
    chain = ["http://example.com/", "https://example.com/", "https://example.com/home"]
    result = await TechnicalAnalyzer().analyze(make_page(GOOD_HTML, redirect_chain=chain))
    finding = _finding(result, "technical.redirects")
    assert finding.severity == Severity.WARNING
    assert finding.evidence == {"chain": chain}


async def test_single_redirect_is_only_informational() -> None:
    result = await TechnicalAnalyzer().analyze(
        make_page(GOOD_HTML, redirect_chain=["http://example.com/"])
    )
    assert _finding(result, "technical.redirects").severity == Severity.INFO


async def test_noindex_meta_detected_case_insensitively() -> None:
    html = GOOD_HTML.replace("<head>", '<head><meta name="robots" content="NOINDEX, nofollow">', 1)
    result = await TechnicalAnalyzer().analyze(make_page(html))
    finding = _finding(result, "technical.noindex")
    assert finding.severity == Severity.CRITICAL
    assert "meta robots" in finding.title


async def test_noindex_header_detected() -> None:
    result = await TechnicalAnalyzer().analyze(
        make_page(GOOD_HTML, headers={"X-Robots-Tag": "noindex"})
    )
    finding = _finding(result, "technical.noindex")
    assert finding.severity == Severity.CRITICAL
    assert "X-Robots-Tag" in finding.title


async def test_plain_http_is_critical() -> None:
    result = await TechnicalAnalyzer().analyze(make_page(GOOD_HTML, requested_url="http://example.com/"))
    assert _finding(result, "technical.https").severity == Severity.CRITICAL


async def test_error_status_is_critical() -> None:
    result = await TechnicalAnalyzer().analyze(make_page(GOOD_HTML, status_code=404))
    assert _finding(result, "technical.status_code").severity == Severity.CRITICAL
