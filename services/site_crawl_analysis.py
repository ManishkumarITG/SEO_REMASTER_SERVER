"""Score every crawled page with the existing analyzers.

A single-page audit runs FIVE analyzers. A site crawl cannot: three of them make
their own outbound call per page, so running them `max_pages` times would turn a
500-page crawl into 500 Lighthouse runs, 500 LLM completions and 500 Search
Console queries. Only the two that read the already-fetched DOM are safe in bulk:

    on_page    titles, descriptions, headings, images, links   ~1 ms
    technical  robots, canonical, viewport, status, timing     ~1 ms

The other three stay available for the single-URL audit, where one page justifies
the cost. `BULK_SAFE` is the guard: a name configured outside it is refused with a
log line rather than quietly running up somebody's API bill.

Analysis happens INSIDE the crawl, through the `page_hook` seam, because the DOM
exists only while a page is being reduced to facts. Scoring afterwards would mean
fetching the whole site twice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from services.analyzers.base import AnalyzerResult, BaseAnalyzer, Severity
from services.analyzers.registry import get_analyzer_weights, get_analyzers
from services.crawler.models import CrawledPage, CrawlStats
from services.crawler.parser import PageData
from services.crawler.site_crawl import PageHook
from services.reports.scoring import grade_for, prioritized_fixes, weighted_score

logger = logging.getLogger("seo.site_crawl.analysis")

#: Analyzers that only read the PageData they are handed. Everything outside this
#: set performs network I/O per page and must never run in a bulk crawl.
BULK_SAFE: frozenset[str] = frozenset({"on_page", "technical"})

#: Default set, and the value SITE_CRAWL_ANALYZERS overrides.
DEFAULT_ANALYZERS: tuple[str, ...] = ("on_page", "technical")

#: Findings kept per page. Non-passed only, most urgent first. A passed check is
#: not worth 500 copies in the database - the score already carries that news.
MAX_FINDINGS_PER_PAGE = 25


def parse_analyzer_names(configured: str | None) -> list[str]:
    """Split the SITE_CRAWL_ANALYZERS setting. Empty means "no analysis"."""
    if configured is None:
        return list(DEFAULT_ANALYZERS)
    return [name.strip() for name in configured.split(",") if name.strip()]


def bulk_analyzers(names: Sequence[str] | None = None) -> list[BaseAnalyzer]:
    """Instantiate the named analyzers, refusing any that is not bulk-safe.

    Returns an empty list when nothing is requested, which is the caller cue to
    skip analysis entirely rather than run a hook that does nothing.
    """
    wanted = list(DEFAULT_ANALYZERS) if names is None else list(names)
    if not wanted:
        return []
    available = {analyzer.name: analyzer for analyzer in get_analyzers()}
    selected: list[BaseAnalyzer] = []
    for name in wanted:
        if name not in BULK_SAFE:
            logger.warning(
                "Analyzer %r is not safe to run once per crawled page (it makes its own "
                "outbound call); skipping it. Bulk-safe analyzers: %s",
                name, ", ".join(sorted(BULK_SAFE)),
            )
            continue
        analyzer = available.get(name)
        if analyzer is None:
            logger.warning("Analyzer %r is not registered; skipping it", name)
            continue
        selected.append(analyzer)
    return selected


def apply_results(page: CrawledPage, results: list[AnalyzerResult]) -> None:
    """Fold analyzer output onto the page. Pure, so it can be tested without a crawl."""
    weights = get_analyzer_weights()
    page.analyzer_scores = {result.analyzer: result.score for result in results}
    page.score = weighted_score(results, weights)
    page.grade = grade_for(page.score) if page.score is not None else None
    findings = prioritized_fixes(results, limit=MAX_FINDINGS_PER_PAGE)
    page.findings = findings
    page.critical_count = sum(1 for f in findings if f["severity"] == Severity.CRITICAL)
    page.warning_count = sum(1 for f in findings if f["severity"] == Severity.WARNING)


async def analyze_page(page: CrawledPage, page_data: PageData, analyzers: Sequence[BaseAnalyzer]) -> None:
    """Run every analyzer over one page and record the outcome on it.

    `BaseAnalyzer.analyze` already swallows its own exceptions into a result with
    `error` set, so gather never has to tolerate a raise here.
    """
    if not analyzers:
        return
    results = list(await asyncio.gather(*(analyzer.analyze(page_data) for analyzer in analyzers)))
    apply_results(page, results)


def make_page_hook(analyzers: Sequence[BaseAnalyzer], stats: CrawlStats | None = None) -> PageHook:
    """A `crawl_site(page_hook=...)` callable that scores each page as it is parsed.

    Rolls the site-level totals up here rather than in the worker loop so a
    caller that streams pages straight to disk still gets the same numbers.
    """

    async def hook(page: CrawledPage, page_data: PageData) -> None:
        await analyze_page(page, page_data, analyzers)
        if stats is not None and page.score is not None:
            stats.analyzed += 1
            stats.score_total += page.score
            stats.critical_findings += page.critical_count
            stats.warning_findings += page.warning_count

    return hook


def worst_pages(pages: Sequence[CrawledPage], limit: int = 10) -> list[CrawledPage]:
    """The lowest-scoring analyzed pages, worst first. `url` makes the order total."""
    scored = [page for page in pages if page.score is not None]
    scored.sort(key=lambda page: (page.score, page.url))  # type: ignore[arg-type,return-value]
    return scored[:limit]
