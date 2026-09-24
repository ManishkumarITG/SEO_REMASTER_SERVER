"""The Gemini SEO report: payload shape, HTML assembly, error mapping. Gemini stubbed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import db.site_pages as site_pages
import services.site_crawl_report as report
from exceptions.gemini import GeminiError, GeminiNotConfigured
from integrations.google.gemini import GeminiService
from models.db import SiteCrawl

HOST = "shop.test"


def _row(**kwargs: object) -> SiteCrawl:
    defaults: dict[str, object] = {
        "id": "crawl-1", "start_url": f"https://{HOST}/", "host": HOST, "status": "complete",
        "site_name": "Shop Test", "max_pages": 100, "max_depth": 3,
        "pages_fetched": 3, "pages_ok": 3, "avg_score": 82.0,
        "created_at": datetime(2026, 9, 24, tzinfo=UTC),
        "completed_at": datetime(2026, 9, 24, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return SiteCrawl(**defaults)  # type: ignore[arg-type]


PAGES = [
    # Optimal title, description too long.
    {"url": f"https://{HOST}/a", "status_code": 200, "score": 88.0,
     "title": "A" * 55, "meta_description": "D" * 200, "heading_skips": 0},
    # Title too short, description missing.
    {"url": f"https://{HOST}/b", "status_code": 200, "score": 40.0,
     "title": "Short", "meta_description": "", "heading_skips": 2},
    # Title too long, description optimal.
    {"url": f"https://{HOST}/c", "status_code": 200, "score": None,
     "title": "T" * 80, "meta_description": "D" * 140, "heading_skips": 0},
    # Not fetched: excluded from the distributions entirely.
    {"url": f"https://{HOST}/skip", "status_code": None, "skipped": "robots"},
]


@pytest.fixture
def _stub_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def iter_pages(crawl_id, *, include_navigation=False, batch_size=200):
        yield PAGES

    async def issue_counts(crawl_id):
        return {"broken": 1, "thin": 2}

    async def load_template_reports(crawl_id):
        return [{"template": "product", "page_count": 200,
                 "lighthouse": {"performance_score": 71, "seo_score": 92}}]

    monkeypatch.setattr(site_pages, "iter_pages", iter_pages)
    monkeypatch.setattr(site_pages, "issue_counts", issue_counts)
    monkeypatch.setattr(site_pages, "load_template_reports", load_template_reports)


# --- payload ---------------------------------------------------------------------------


async def test_the_payload_carries_real_meta_distributions(_stub_storage: None) -> None:
    payload = await report.build_payload(_row())
    assert payload["pages_scanned_for_meta"] == 3  # the skipped page is not counted
    assert payload["meta_title_distribution"] == {
        "optimal": 1, "too_short": 1, "too_long": 1, "missing": 0,
    }
    assert payload["meta_description_distribution"] == {
        "optimal": 1, "too_short": 0, "too_long": 1, "missing": 1,
    }


async def test_the_payload_carries_score_and_heading_signals(_stub_storage: None) -> None:
    payload = await report.build_payload(_row())
    assert payload["score_distribution"] == {
        "good_80_plus": 1, "medium_50_79": 0, "poor_under_50": 1, "unscored": 1,
    }
    assert payload["heading_issue_pages"] == 1


async def test_the_payload_includes_examples_templates_and_issues(_stub_storage: None) -> None:
    payload = await report.build_payload(_row())
    assert payload["issue_counts"]["broken"] == 1
    assert payload["lighthouse_by_template"][0]["lighthouse"]["performance_score"] == 71
    # Only pages outside the optimal band are worth a rewrite example.
    example_urls = {e["url"] for e in payload["example_pages"]}
    assert f"https://{HOST}/b" in example_urls
    assert all("title_length" in e and "meta_description_length" in e for e in payload["example_pages"])


async def test_issue_counts_failing_does_not_break_the_payload(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(crawl_id):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(site_pages, "issue_counts", boom)
    payload = await report.build_payload(_row())
    assert payload["issue_counts"] == {}
    assert payload["pages_scanned_for_meta"] == 3


# --- HTML generation -------------------------------------------------------------------


async def test_the_report_is_a_standalone_html_document(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_text(self, *, prompt, system_instruction, **kwargs):
        # The payload really reached the model: the prompt must carry the figures.
        assert "meta_title_distribution" in prompt
        assert report.REFERENCE_URL in prompt
        return "# SEO Audit Report\n\n## 01 Executive Summary\n\n**Strong** base.\n\n- Item\n"

    monkeypatch.setattr(GeminiService, "generate_text", fake_text)
    html_bytes, filename = await report.generate_report_html(_row())
    text = html_bytes.decode("utf-8")
    assert text.startswith("<!doctype html>")
    assert "<title>SEO Audit Report - Shop Test</title>" in text
    assert "<h1>SEO Audit Report</h1>" in text
    assert "<strong>Strong</strong>" in text
    assert "generated" in text and "by Gemini" in text
    assert filename == "sitecrawl-shop.test-20260924-report.html"


async def test_model_authored_html_in_the_report_is_escaped(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's output is untrusted text landing in a browser."""
    async def evil(self, *, prompt, system_instruction, **kwargs):
        return "## <script>alert(1)</script>\n\nText <img src=x onerror=y>."

    monkeypatch.setattr(GeminiService, "generate_text", evil)
    text = (await report.generate_report_html(_row()))[0].decode("utf-8")
    # The one <script> in the document is nothing from the model.
    assert "alert(1)" not in text or "<script>alert" not in text
    assert "&lt;script&gt;" in text
    assert "<img src=x" not in text


async def test_a_gemini_failure_propagates_for_the_route_to_map(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self, **kwargs):
        raise GeminiError("upstream 500")

    monkeypatch.setattr(GeminiService, "generate_text", boom)
    with pytest.raises(GeminiError):
        await report.generate_report_html(_row())


async def test_an_unconfigured_gemini_propagates(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self, **kwargs):
        raise GeminiNotConfigured("no key")

    monkeypatch.setattr(GeminiService, "generate_text", boom)
    with pytest.raises(GeminiNotConfigured):
        await report.generate_report_html(_row())
