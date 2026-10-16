"""The Search Console half of a full analysis: performance, indexation, rich results.

Everything here is BEST-EFFORT and runs in a background worker, not a request.
`execute_google_call` raises HTTPException (it was written for routes); this module
catches everything and returns a result object with an `error` string instead, so
a GSC outage degrades the report rather than failing the whole pipeline.

Two hard limits shape it. The Search Console API has no bulk index-coverage or
schema endpoint, so indexation is confirmed one URL at a time via the URL
Inspection API - which is quota-limited to ~2000/day. So inspection runs on a
small SAMPLE of the crawled URLs, chosen to span templates, never the whole site.
Search Analytics, by contrast, is one call and covers every page with traffic.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from integrations.google.oauth import execute_google_call, get_search_console_service

logger = logging.getLogger("seo.site_analysis.gsc")

#: URLs inspected per analysis. Each is one API call against a ~2000/day quota,
#: shared across every analysis a user runs, so this stays small.
MAX_INSPECTIONS = 20
#: Inspections in flight. The API is per-user rate limited; a couple at a time is
#: courteous and still finishes 20 URLs quickly.
INSPECT_CONCURRENCY = 3
#: Rows pulled from Search Analytics for the performance section.
ANALYTICS_ROW_LIMIT = 100


@dataclass(slots=True)
class GscResult:
    """What the GSC phase learned. Any field may be empty; `error` says why."""

    connected: bool = False
    site_url: str = ""
    #: Top pages and queries from Search Analytics (last 28 days).
    top_pages: list[dict[str, Any]] = field(default_factory=list)
    top_queries: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)
    #: One record per inspected URL: coverage verdict, indexing state, rich results.
    inspections: list[dict[str, Any]] = field(default_factory=list)
    indexed_count: int = 0
    not_indexed_count: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "site_url": self.site_url,
            "top_pages": self.top_pages,
            "top_queries": self.top_queries,
            "totals": self.totals,
            "inspections": self.inspections,
            "indexed_count": self.indexed_count,
            "not_indexed_count": self.not_indexed_count,
            "inspected_count": len(self.inspections),
            "error": self.error,
        }


def choose_property(sites: list[dict[str, Any]], host: str) -> str | None:
    """The verified GSC property that best matches the crawl's host.

    A domain property `sc-domain:example.com` covers every subdomain and scheme,
    so it is preferred; otherwise the first URL-prefix property whose host matches.
    Returns None when the account has no property for this site - the pipeline then
    runs without GSC rather than guessing at someone else's property.
    """
    host = host.lower().removeprefix("www.")
    domain = f"sc-domain:{host}"
    for site in sites:
        if (site.get("siteUrl") or "").lower() == domain:
            return site["siteUrl"]
    for site in sites:
        url = site.get("siteUrl") or ""
        if url.startswith("sc-domain:"):
            if url.lower().removeprefix("sc-domain:").removeprefix("www.") == host:
                return url
        else:
            from urllib.parse import urlsplit

            site_host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            if site_host == host:
                return url
    return None


def _date_range(days: int = 28, lag: int = 2) -> tuple[str, str]:
    from datetime import date, timedelta

    end = date.today() - timedelta(days=lag)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


async def _analytics(user_id: str, site_url: str, dimension: str) -> list[dict[str, Any]]:
    service, creds = await get_search_console_service(user_id)
    start, end = _date_range()
    body = {"startDate": start, "endDate": end, "dimensions": [dimension], "rowLimit": ANALYTICS_ROW_LIMIT}
    result = await execute_google_call(
        user_id, creds, service.searchanalytics().query(siteUrl=site_url, body=body)
    )
    rows = result.get("rows", []) or []
    return [
        {
            "key": (row.get("keys") or [""])[0],
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
            "ctr": round(row.get("ctr", 0.0), 4),
            "position": round(row.get("position", 0.0), 1),
        }
        for row in rows
    ]


async def _inspect(user_id: str, site_url: str, page_url: str) -> dict[str, Any]:
    service, creds = await get_search_console_service(user_id)
    result = await execute_google_call(
        user_id,
        creds,
        service.urlInspection().index().inspect(
            body={"inspectionUrl": page_url, "siteUrl": site_url}
        ),
    )
    inspection = (result.get("inspectionResult") or {})
    index = inspection.get("indexStatusResult") or {}
    rich = inspection.get("richResultsResult") or {}
    rich_issues = []
    for item in rich.get("detectedItems", []) or []:
        for issue in item.get("items", []) or []:
            if issue.get("issueMessage"):
                rich_issues.append(issue["issueMessage"])
    return {
        "url": page_url,
        "coverage_state": index.get("coverageState", ""),
        "verdict": index.get("verdict", ""),
        "indexing_state": index.get("indexingState", ""),
        "last_crawl": index.get("lastCrawlTime", ""),
        "robots_txt_state": index.get("robotsTxtState", ""),
        "rich_result_verdict": rich.get("verdict", ""),
        "rich_result_issues": rich_issues[:10],
    }


def _sample_urls(pages: list[dict[str, Any]]) -> list[str]:
    """Up to MAX_INSPECTIONS crawled URLs, spread across templates.

    Round-robins over templates so a store with 4,000 products and 3 blog posts
    still spends some inspections on the blog, rather than 20 near-identical
    product pages.
    """
    by_template: dict[str, list[str]] = {}
    for page in pages:
        if page.get("status_code") and not page.get("skipped") and not page.get("error"):
            by_template.setdefault(page.get("template") or "other", []).append(page["url"])
    chosen: list[str] = []
    while len(chosen) < MAX_INSPECTIONS and any(by_template.values()):
        for urls in by_template.values():
            if urls:
                chosen.append(urls.pop(0))
                if len(chosen) >= MAX_INSPECTIONS:
                    break
    return chosen


async def collect_gsc(user_id: str | None, host: str, crawled_pages: list[dict[str, Any]]) -> GscResult:
    """Performance + a sampled indexation/rich-results read for `host`.

    `crawled_pages` are the pages the crawl already found, so inspection targets
    real URLs. Returns a GscResult that is safe to serialise even when GSC is
    absent - the pipeline always continues.
    """
    result = GscResult()
    if not user_id:
        result.error = "no signed-in session"
        return result

    try:
        from services.search_console_service import list_sites

        sites = (await list_sites(user_id)).sites
    except Exception as exc:  # noqa: BLE001 - GSC is optional; never fail the pipeline
        result.error = f"Search Console not connected ({type(exc).__name__})"
        return result

    site_url = choose_property([s.model_dump() for s in sites], host)
    if site_url is None:
        result.error = f"no verified Search Console property matches {host}"
        return result
    result.connected = True
    result.site_url = site_url

    try:
        result.top_pages = await _analytics(user_id, site_url, "page")
        result.top_queries = await _analytics(user_id, site_url, "query")
        result.totals = {
            "clicks": sum(p["clicks"] for p in result.top_pages),
            "impressions": sum(p["impressions"] for p in result.top_pages),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("GSC analytics failed for %s: %s", site_url, exc)
        result.error = f"Search Analytics unavailable ({type(exc).__name__})"

    sample = _sample_urls(crawled_pages)
    if sample:
        semaphore = asyncio.Semaphore(INSPECT_CONCURRENCY)

        async def run(url: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    return await _inspect(user_id, site_url, url)
                except Exception as exc:  # noqa: BLE001 - one bad inspection is not fatal
                    logger.info("URL inspection failed for %s: %s", url, exc)
                    return None

        for record in await asyncio.gather(*(run(u) for u in sample)):
            if record is None:
                continue
            result.inspections.append(record)
            if record["verdict"] == "PASS" or "indexed" in record["coverage_state"].lower():
                result.indexed_count += 1
            else:
                result.not_indexed_count += 1

    return result
