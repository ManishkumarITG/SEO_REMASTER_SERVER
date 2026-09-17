"""Scoring math: weighted averages, grades, prioritized fix ordering."""

from __future__ import annotations

from services.analyzers.base import AnalyzerResult, Finding, Severity
from services.reports.scoring import build_report, grade_for


def _result(analyzer: str, category: str, score: float | None, findings=()) -> AnalyzerResult:
    return AnalyzerResult(analyzer=analyzer, category=category, score=score, findings=list(findings))


def _finding(fid: str, severity: Severity, impact: int = 3, effort: int = 3) -> Finding:
    return Finding(
        id=fid, severity=severity, title=fid, description="d", recommendation="r",
        impact=impact, effort=effort,
    )


def test_grade_bands() -> None:
    assert grade_for(95) == "A"
    assert grade_for(90) == "A"
    assert grade_for(89.9) == "B"
    assert grade_for(80) == "B"
    assert grade_for(70) == "C"
    assert grade_for(60) == "D"
    assert grade_for(59.9) == "F"
    assert grade_for(0) == "F"


def test_overall_is_weight_averaged() -> None:
    report = build_report(
        [_result("a", "x", 80), _result("b", "y", 60)],
        weights={"a": 3.0, "b": 1.0},
    )
    assert report["overall_score"] == 75.0  # (80*3 + 60*1) / 4
    assert report["grade"] == "C"


def test_none_scores_excluded_from_weighting() -> None:
    report = build_report(
        [_result("a", "x", 90), _result("b", "y", None)],
        weights={"a": 1.0, "b": 1.0},
    )
    assert report["overall_score"] == 90.0
    assert report["grade"] == "A"


def test_all_scores_none_gives_no_overall() -> None:
    report = build_report([_result("a", "x", None)], weights={"a": 1.0})
    assert report["overall_score"] is None
    assert report["grade"] is None


def test_category_grouping() -> None:
    report = build_report(
        [_result("a", "x", 100), _result("b", "x", 50), _result("c", "y", 70)],
        weights={"a": 1.0, "b": 1.0, "c": 1.0},
    )
    by_name = {c["name"]: c for c in report["categories"]}
    assert by_name["x"]["score"] == 75.0
    assert by_name["y"]["score"] == 70.0
    assert len(by_name["x"]["analyzers"]) == 2


def test_prioritized_fixes_order_and_limit() -> None:
    findings = [
        _finding("warning-strong-hard", Severity.WARNING, impact=5, effort=3),
        _finding("critical-weak", Severity.CRITICAL, impact=2, effort=5),
        _finding("warning-strong-easy", Severity.WARNING, impact=5, effort=1),
        _finding("passed", Severity.PASSED),
        _finding("info", Severity.INFO),
    ]
    report = build_report([_result("a", "x", 50, findings)], weights={"a": 1.0})
    ids = [f["id"] for f in report["prioritized_fixes"]]
    # severity first (critical > warning > info), then impact desc, then effort asc; passed excluded
    assert ids == ["critical-weak", "warning-strong-easy", "warning-strong-hard", "info"]

    many = [_finding(f"f{i}", Severity.WARNING) for i in range(15)]
    report = build_report([_result("a", "x", 50, many)], weights={"a": 1.0})
    assert len(report["prioritized_fixes"]) == 10
    assert all(f["analyzer"] == "a" for f in report["prioritized_fixes"])
