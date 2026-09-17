"""Registry auto-discovery + content_ai graceful skip (no network, no API key)."""

from __future__ import annotations

import pytest

import services.integrations.llm as llm_module
from services.analyzers.base import Severity
from services.analyzers.content_ai import ContentAIAnalyzer
from services.analyzers.registry import get_analyzers
from tests.factories import GOOD_HTML, make_page


def test_registry_discovers_builtin_analyzers() -> None:
    names = {analyzer.name for analyzer in get_analyzers()}
    assert {"on_page", "technical", "content_ai", "pagespeed"} <= names


async def test_content_ai_skips_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "_service", llm_module.LLMService(api_key="", model="test-model"))
    result = await ContentAIAnalyzer().analyze(make_page(GOOD_HTML))
    assert result.error is None
    assert result.score is None  # excluded from weighting
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity == Severity.INFO
    assert "no API key" in finding.title
