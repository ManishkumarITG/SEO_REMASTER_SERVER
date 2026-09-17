"""Analyzer plugin contract: Severity, Finding, AnalyzerResult, BaseAnalyzer.

Adding a new analyzer to the platform means adding ONE new file to
services/analyzers/ containing a BaseAnalyzer subclass decorated with
@register_analyzer. Nothing else in the app changes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from services.crawler.parser import PageData


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    PASSED = "passed"


#: Sort order for prioritization (lower = more urgent).
SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
    Severity.PASSED: 3,
}

#: Score deduction applied per finding severity by `score_from_findings`.
_DEDUCTIONS: dict[Severity, float] = {
    Severity.CRITICAL: 20.0,
    Severity.WARNING: 8.0,
    Severity.INFO: 0.0,
    Severity.PASSED: 0.0,
}


class Finding(BaseModel):
    id: str
    severity: Severity
    title: str
    description: str
    recommendation: str
    impact: int = Field(ge=1, le=5, description="How much fixing this helps (1-5)")
    effort: int = Field(ge=1, le=5, description="How hard the fix is (1-5)")
    evidence: dict[str, Any] | None = None


class AnalyzerResult(BaseModel):
    analyzer: str
    category: str
    score: float | None = Field(default=None, ge=0, le=100)
    findings: list[Finding] = Field(default_factory=list)
    duration_ms: float = 0.0
    error: str | None = None


def score_from_findings(findings: list[Finding]) -> float:
    """100 minus a deduction per non-passed finding, floored at 0."""
    return max(0.0, 100.0 - sum(_DEDUCTIONS[f.severity] for f in findings))


class BaseAnalyzer(ABC):
    """All analyzers subclass this. The public `analyze` never raises."""

    name: ClassVar[str]
    category: ClassVar[str]
    weight: ClassVar[float] = 1.0

    async def analyze(self, page: PageData) -> AnalyzerResult:
        started = time.perf_counter()
        try:
            result = await self._analyze(page)
        except Exception as exc:  # noqa: BLE001 — one analyzer failing must never fail the audit
            result = AnalyzerResult(
                analyzer=self.name,
                category=self.category,
                score=None,
                findings=[],
                error=f"{type(exc).__name__}: {exc}",
            )
        result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return result

    @abstractmethod
    async def _analyze(self, page: PageData) -> AnalyzerResult:
        """Implement the actual checks. May raise freely; `analyze` wraps it."""
