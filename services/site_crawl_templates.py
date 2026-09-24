"""One Lighthouse report per template, not per page.

A store with 300 products has 300 product pages and ONE product template. They
share their markup, their scripts and their layout, so they share their
performance characteristics too - and Lighthouse costs 10-30 seconds a run. Per
page that is hours and 300 API calls; per template it is a couple of minutes and
about five.

That is the whole reason `pagespeed` is refused by `site_crawl_analysis.BULK_SAFE`
while this module is allowed to call it: the unit of work is different.

The representative page for a group is chosen, not taken at random - see
`pick_representative`. Everything here degrades to "no report" rather than
failing: PageSpeed is an external service with a quota, and a crawl that finished
is worth more than one that died reaching for a nice-to-have.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from integrations.google.pagespeed import FULL_REPORT, fetch_pagespeed
from services.crawler.models import CrawledPage

logger = logging.getLogger("seo.site_crawl.templates")

#: Templates tested in one crawl. A store has a handful; anything past this is a
#: theme doing something unusual, and each one costs a Lighthouse run.
MAX_TEMPLATES_TESTED = 8
#: Lighthouse runs in flight. PSI is rate limited and slow, and the crawl is
#: already finished by the time this runs, so there is nothing to race.
TEMPLATE_CONCURRENCY = 2
#: A whole template phase that cannot finish in this long is abandoned; the crawl
#: keeps every page result it already has.
TEMPLATE_PHASE_TIMEOUT_S = 8 * 60

#: Which template gets tested first when the cap bites. Ordered by how much of a
#: store's traffic and revenue usually sits behind them.
_PRIORITY: dict[str, int] = {
    "product": 0,
    "collection": 1,
    "home": 2,
    "article": 3,
    "page": 4,
    "blog": 5,
    "search": 6,
    "policy": 7,
    "cart": 8,
    "account": 9,
    "other": 10,
}


@dataclass(slots=True)
class TemplateGroup:
    """Every crawled page that one template rendered."""

    template: str
    urls: list[str] = field(default_factory=list)
    representative: str = ""
    #: Median word count across the group, kept because it is the cheapest signal
    #: that a "template" is really two different layouts wearing one name.
    median_words: int = 0

    @property
    def page_count(self) -> int:
        return len(self.urls)


def pick_representative(pages: Sequence[CrawledPage]) -> CrawledPage | None:
    """The page that best stands in for its group.

    The one whose word count is the group median, not the first one crawled. The
    first is usually the home page's nearest neighbour or whatever the sitemap
    happened to list first, and an outlier - an empty product, a 900-word essay -
    would misreport the whole template. The median is the ordinary case, which is
    what a per-template report is claiming to describe.
    """
    usable = [p for p in pages if p.ok and not p.skipped and not p.error]
    if not usable:
        return None
    ordered = sorted(usable, key=lambda p: (p.word_count, p.url))
    return ordered[len(ordered) // 2]


def group_by_template(pages: Sequence[CrawledPage]) -> list[TemplateGroup]:
    """Fetched pages grouped by template, most-worth-testing first."""
    buckets: dict[str, list[CrawledPage]] = {}
    for page in pages:
        if not page.fetched or page.skipped or page.error:
            continue
        buckets.setdefault(page.template or "other", []).append(page)

    groups: list[TemplateGroup] = []
    for template, members in buckets.items():
        representative = pick_representative(members)
        words = sorted(p.word_count for p in members)
        groups.append(TemplateGroup(
            template=template,
            urls=[p.url for p in members],
            representative=representative.final_url or representative.url if representative else "",
            median_words=words[len(words) // 2] if words else 0,
        ))
    # Biggest groups first within a priority band: a template covering 200 pages
    # is worth more than one covering 2.
    groups.sort(key=lambda g: (_PRIORITY.get(g.template, 99), -g.page_count))
    return groups


async def _report_for(group: TemplateGroup, strategy: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "template": group.template,
        "page_count": group.page_count,
        "representative_url": group.representative,
        "median_words": group.median_words,
        "sample_urls": group.urls[:20],
        "lighthouse": None,
        "error": "",
    }
    if not group.representative:
        record["error"] = "no usable page to test"
        return record
    metrics = await fetch_pagespeed(group.representative, strategy=strategy, categories=FULL_REPORT)
    if metrics is None:
        record["error"] = "PageSpeed Insights returned no usable result"
        return record
    record["lighthouse"] = metrics
    return record


async def build_template_reports(
    pages: Sequence[CrawledPage],
    *,
    strategy: str = "mobile",
    max_templates: int = MAX_TEMPLATES_TESTED,
    concurrency: int = TEMPLATE_CONCURRENCY,
) -> list[dict[str, Any]]:
    """One record per template, with a Lighthouse report on its representative page.

    Groups beyond `max_templates` are still RETURNED - a report that omits a
    template entirely would read as "this store has no blog" - they just carry no
    Lighthouse result and say why.
    """
    groups = group_by_template(pages)
    if not groups:
        return []

    tested, skipped = groups[:max_templates], groups[max_templates:]
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(group: TemplateGroup) -> dict[str, Any]:
        async with semaphore:
            try:
                return await _report_for(group, strategy)
            except Exception as exc:  # noqa: BLE001 - a nice-to-have must not raise
                logger.warning("template report for %s failed: %s: %s",
                               group.template, type(exc).__name__, exc)
                return {
                    "template": group.template,
                    "page_count": group.page_count,
                    "representative_url": group.representative,
                    "median_words": group.median_words,
                    "sample_urls": group.urls[:20],
                    "lighthouse": None,
                    "error": f"{type(exc).__name__}",
                }

    records = list(await asyncio.gather(*(run(group) for group in tested)))
    records.extend({
        "template": group.template,
        "page_count": group.page_count,
        "representative_url": group.representative,
        "median_words": group.median_words,
        "sample_urls": group.urls[:20],
        "lighthouse": None,
        "error": f"not tested - only the first {max_templates} templates are measured",
    } for group in skipped)
    return records
