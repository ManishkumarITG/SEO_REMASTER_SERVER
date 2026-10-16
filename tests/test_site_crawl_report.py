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


# --- Gemini transient-error retry ------------------------------------------------------


async def test_generate_text_retries_a_transient_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 "high demand" is temporary, so the text path retries rather than failing."""
    from google.genai import errors as genai_errors

    import integrations.google.gemini as gem

    calls = {"n": 0}

    class FakeModels:
        async def generate_content(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise genai_errors.ServerError(503, {"error": {"message": "high demand"}})
            class R:
                text = "# Report"
            return R()

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    monkeypatch.setattr(gem, "_TEXT_RETRY_BACKOFF_S", 0.0)  # no real waiting in tests
    svc = gem.GeminiService(api_key="x")
    monkeypatch.setattr(svc, "_get_client", lambda: FakeClient())
    out = await svc.generate_text(prompt="p", system_instruction="s")
    assert out == "# Report"
    assert calls["n"] == 3  # two failures, then success


async def test_generate_text_gives_up_after_repeated_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.genai import errors as genai_errors

    import integrations.google.gemini as gem
    from exceptions.gemini import GeminiError

    class FakeModels:
        async def generate_content(self, **kwargs):
            raise genai_errors.ServerError(503, {"error": {"message": "high demand"}})

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    monkeypatch.setattr(gem, "_TEXT_RETRY_BACKOFF_S", 0.0)
    svc = gem.GeminiService(api_key="x")
    monkeypatch.setattr(svc, "_get_client", lambda: FakeClient())
    with pytest.raises(GeminiError):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_generate_text_does_not_retry_a_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx is our fault; retrying wastes time, so it fails immediately."""
    from google.genai import errors as genai_errors

    import integrations.google.gemini as gem
    from exceptions.gemini import GeminiError

    calls = {"n": 0}

    class FakeModels:
        async def generate_content(self, **kwargs):
            calls["n"] += 1
            raise genai_errors.ClientError(400, {"error": {"message": "bad request"}})

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    svc = gem.GeminiService(api_key="x")
    monkeypatch.setattr(svc, "_get_client", lambda: FakeClient())
    with pytest.raises(GeminiError):
        await svc.generate_text(prompt="p", system_instruction="s")
    assert calls["n"] == 1  # not retried


# --- Gemini model fallback chain -------------------------------------------------------


async def test_generate_text_falls_back_when_the_primary_model_is_overloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saturated primary must not fail the report when another pool is free."""
    from google.genai import errors as genai_errors

    import integrations.google.gemini as gem

    seen: list[str] = []

    class FakeModels:
        async def generate_content(self, **kwargs):
            model = kwargs["model"]
            seen.append(model)
            if model == "primary":
                raise genai_errors.ServerError(503, {"error": {"message": "high demand"}})
            class R:
                text = "# Fallback report"
            return R()

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    monkeypatch.setattr(gem, "_TEXT_RETRY_BACKOFF_S", 0.0)
    svc = gem.GeminiService(api_key="x", model="primary")
    svc._fallback_models = ["fallback-a", "fallback-b"]
    monkeypatch.setattr(svc, "_get_client", lambda: FakeClient())
    out = await svc.generate_text(prompt="p", system_instruction="s")
    assert out == "# Fallback report"
    assert seen.count("primary") == 3  # primary spent its full retry budget first
    assert "fallback-a" in seen
    assert "fallback-b" not in seen  # stopped as soon as a model answered


async def test_generate_text_skips_a_fallback_model_that_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx on a fallback (e.g. an unknown model name) skips to the next candidate."""
    from google.genai import errors as genai_errors

    import integrations.google.gemini as gem

    seen: list[str] = []

    class FakeModels:
        async def generate_content(self, **kwargs):
            model = kwargs["model"]
            seen.append(model)
            if model == "primary":
                raise genai_errors.ServerError(503, {"error": {"message": "high demand"}})
            if model == "bad-fallback":
                raise genai_errors.ClientError(404, {"error": {"message": "model not found"}})
            class R:
                text = "# Second fallback"
            return R()

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    monkeypatch.setattr(gem, "_TEXT_RETRY_BACKOFF_S", 0.0)
    svc = gem.GeminiService(api_key="x", model="primary")
    svc._fallback_models = ["bad-fallback", "good-fallback"]
    monkeypatch.setattr(svc, "_get_client", lambda: FakeClient())
    out = await svc.generate_text(prompt="p", system_instruction="s")
    assert out == "# Second fallback"
    assert seen.count("bad-fallback") == 1  # a 4xx on a fallback is not retried
    assert "good-fallback" in seen


async def test_generate_text_does_not_fall_back_on_an_empty_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty reply is a content decision, not capacity: don't burn the chain."""
    import integrations.google.gemini as gem
    from exceptions.gemini import GeminiResponseError

    seen: list[str] = []

    class FakeModels:
        async def generate_content(self, **kwargs):
            seen.append(kwargs["model"])
            class R:
                text = ""
            return R()

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    svc = gem.GeminiService(api_key="x", model="primary")
    svc._fallback_models = ["fallback-a"]
    monkeypatch.setattr(svc, "_get_client", lambda: FakeClient())
    with pytest.raises(GeminiResponseError):
        await svc.generate_text(prompt="p", system_instruction="s")
    assert seen == ["primary"]  # never touched the fallback


# --- engine choice: the local Ollama model ---------------------------------------------


async def test_the_ollama_engine_writes_the_report_and_is_credited(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from integrations.ollama.client import OllamaService

    async def fake_ollama(self, *, prompt, system_instruction, **kwargs):
        # The same audit brief goes to the local model, reference URL included.
        assert report.REFERENCE_URL in prompt and "meta_title_distribution" in prompt
        return "# SEO Audit Report\n\nLocal body."

    async def never(self, **kwargs):
        raise AssertionError("Gemini must not be called when engine=ollama")

    monkeypatch.setattr(OllamaService, "generate_text", fake_ollama)
    monkeypatch.setattr(GeminiService, "generate_text", never)
    html_bytes, filename = await report.generate_report_html(_row(), engine="ollama")
    text = html_bytes.decode("utf-8")
    assert "Local body." in text
    assert "by Ollama (" in text and "by Gemini" not in text
    # A different filename from the Gemini one, so both downloads can coexist.
    assert filename == "sitecrawl-shop.test-20260924-ollama-report.html"


async def test_an_ollama_failure_propagates_for_the_route_to_map(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exceptions.ollama import OllamaError
    from integrations.ollama.client import OllamaService

    async def boom(self, **kwargs):
        raise OllamaError("not reachable")

    monkeypatch.setattr(OllamaService, "generate_text", boom)
    with pytest.raises(OllamaError):
        await report.generate_report_html(_row(), engine="ollama")


# --- engine choice: Claude through the Claude Code CLI -----------------------------------


async def test_the_claude_engine_writes_the_report_and_is_credited(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from integrations.claude_cli.client import ClaudeCliService

    async def fake_claude(self, *, prompt, system_instruction, **kwargs):
        assert report.REFERENCE_URL in prompt and "meta_title_distribution" in prompt
        return "# SEO Audit Report\n\nClaude body."

    async def never(self, **kwargs):
        raise AssertionError("Gemini must not be called when engine=claude")

    monkeypatch.setattr(ClaudeCliService, "generate_text", fake_claude)
    monkeypatch.setattr(GeminiService, "generate_text", never)
    html_bytes, filename = await report.generate_report_html(_row(), engine="claude")
    text = html_bytes.decode("utf-8")
    assert "Claude body." in text
    assert "by Claude (" in text and "by Gemini" not in text
    assert filename == "sitecrawl-shop.test-20260924-claude-report.html"


async def test_a_claude_cli_failure_propagates_for_the_route_to_map(
    _stub_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exceptions.claude_cli import ClaudeCliError
    from integrations.claude_cli.client import ClaudeCliService

    async def boom(self, **kwargs):
        raise ClaudeCliError("usage limit")

    monkeypatch.setattr(ClaudeCliService, "generate_text", boom)
    with pytest.raises(ClaudeCliError):
        await report.generate_report_html(_row(), engine="claude")
