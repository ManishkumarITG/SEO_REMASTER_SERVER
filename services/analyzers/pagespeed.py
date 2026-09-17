"""Core Web Vitals via Google PageSpeed Insights. Skips gracefully on failure."""

from __future__ import annotations

from services.analyzers.base import AnalyzerResult, BaseAnalyzer, Finding, Severity
from services.analyzers.registry import register_analyzer
from services.crawler.parser import PageData
from services.integrations.pagespeed import fetch_pagespeed


def _rate(value: float, good: float, poor: float) -> Severity:
    if value <= good:
        return Severity.PASSED
    if value <= poor:
        return Severity.WARNING
    return Severity.CRITICAL


@register_analyzer
class PageSpeedAnalyzer(BaseAnalyzer):
    name = "pagespeed"
    category = "performance"
    weight = 1.0

    async def _analyze(self, page: PageData) -> AnalyzerResult:
        metrics = await fetch_pagespeed(page.final_url)
        if not metrics or all(
            metrics.get(key) is None for key in ("performance_score", "lcp_ms", "inp_ms", "cls")
        ):
            return AnalyzerResult(
                analyzer=self.name,
                category=self.category,
                score=None,  # None = excluded from the weighted overall score
                findings=[Finding(
                    id="pagespeed.skipped", severity=Severity.INFO,
                    title="PageSpeed Insights unavailable - skipped",
                    description="The PageSpeed Insights API could not be reached or returned no usable data.",
                    recommendation="Set PAGESPEED_API_KEY (or retry later) to include Core Web Vitals in the audit.",
                    impact=1, effort=1)],
            )

        findings: list[Finding] = []
        perf = metrics.get("performance_score")
        if perf is not None:
            severity = Severity.PASSED if perf >= 90 else Severity.WARNING if perf >= 50 else Severity.CRITICAL
            findings.append(Finding(
                id="pagespeed.performance", severity=severity,
                title=f"Lighthouse performance score: {perf}/100",
                description="Overall Lighthouse performance rating for the mobile lab run.",
                recommendation=(
                    "Work through the LCP / INP / CLS findings; they drive this score."
                    if severity is not Severity.PASSED else "No action needed."
                ),
                impact=4, effort=3, evidence={"performance_score": perf}))

        lcp = metrics.get("lcp_ms")
        if lcp is not None:
            severity = _rate(lcp, 2500, 4000)
            findings.append(Finding(
                id="pagespeed.lcp", severity=severity,
                title=f"Largest Contentful Paint: {lcp / 1000:.1f}s",
                description="LCP measures how fast the main content becomes visible. Good is 2.5s or less.",
                recommendation=(
                    "Optimize the LCP element: compress hero images, preload critical assets, cut server latency."
                    if severity is not Severity.PASSED else "No action needed."
                ),
                impact=5, effort=3, evidence={"lcp_ms": lcp}))

        inp = metrics.get("inp_ms")
        if inp is not None:
            severity = _rate(inp, 200, 500)
            findings.append(Finding(
                id="pagespeed.inp", severity=severity,
                title=f"Interaction to Next Paint: {inp:.0f}ms",
                description="INP measures responsiveness to user input. Good is 200ms or less.",
                recommendation=(
                    "Break up long JavaScript tasks and defer non-critical scripts."
                    if severity is not Severity.PASSED else "No action needed."
                ),
                impact=4, effort=4, evidence={"inp_ms": inp}))
        else:
            findings.append(Finding(
                id="pagespeed.inp", severity=Severity.INFO,
                title="No INP field data",
                description="INP comes from real-user Chrome data; this URL lacks sufficient traffic in CrUX.",
                recommendation="No action possible - the metric appears once the page has enough real-user data.",
                impact=1, effort=1))

        cls = metrics.get("cls")
        if cls is not None:
            severity = _rate(cls, 0.1, 0.25)
            findings.append(Finding(
                id="pagespeed.cls", severity=severity,
                title=f"Cumulative Layout Shift: {cls:.3f}",
                description="CLS measures visual stability. Good is 0.1 or less.",
                recommendation=(
                    "Reserve space for images, ads and embeds with explicit dimensions to stop content jumping."
                    if severity is not Severity.PASSED else "No action needed."
                ),
                impact=3, effort=2, evidence={"cls": cls}))

        return AnalyzerResult(
            analyzer=self.name,
            category=self.category,
            score=float(perf) if perf is not None else None,
            findings=findings,
        )
