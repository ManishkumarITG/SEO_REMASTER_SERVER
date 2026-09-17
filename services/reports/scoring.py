"""Weighted scoring + report assembly."""

from __future__ import annotations

from typing import Any

from services.analyzers.base import SEVERITY_ORDER, AnalyzerResult, Severity

_GRADE_BANDS: tuple[tuple[float, str], ...] = ((90.0, "A"), (80.0, "B"), (70.0, "C"), (60.0, "D"))


def grade_for(score: float) -> str:
    for threshold, letter in _GRADE_BANDS:
        if score >= threshold:
            return letter
    return "F"


def weighted_score(results: list[AnalyzerResult], weights: dict[str, float]) -> float | None:
    """Weighted average of analyzer scores. Analyzers with score=None are excluded."""
    scored = [(r.score, weights.get(r.analyzer, 1.0)) for r in results if r.score is not None]
    if not scored:
        return None
    total_weight = sum(weight for _, weight in scored)
    if total_weight <= 0:
        return None
    return round(sum(score * weight for score, weight in scored) / total_weight, 1)


def prioritized_fixes(results: list[AnalyzerResult], limit: int = 10) -> list[dict[str, Any]]:
    """All non-passed findings, most urgent first: severity, then impact desc, then effort asc."""
    fixes: list[dict[str, Any]] = []
    for result in results:
        for finding in result.findings:
            if finding.severity is Severity.PASSED:
                continue
            item = finding.model_dump(mode="json")
            item["analyzer"] = result.analyzer
            item["category"] = result.category
            fixes.append(item)
    fixes.sort(key=lambda f: (SEVERITY_ORDER[Severity(f["severity"])], -f["impact"], f["effort"]))
    return fixes[:limit]


def build_report(results: list[AnalyzerResult], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Full audit report: overall score + grade, per-category scores, prioritized fixes."""
    if weights is None:
        from services.analyzers.registry import get_analyzer_weights

        weights = get_analyzer_weights()

    by_category: dict[str, list[AnalyzerResult]] = {}
    for result in results:
        by_category.setdefault(result.category, []).append(result)

    categories = [
        {
            "name": name,
            "score": weighted_score(group, weights),
            "analyzers": [r.model_dump(mode="json") for r in group],
        }
        for name, group in by_category.items()
    ]

    overall = weighted_score(results, weights)
    return {
        "overall_score": overall,
        "grade": grade_for(overall) if overall is not None else None,
        "categories": categories,
        "prioritized_fixes": prioritized_fixes(results),
    }
