"""Real Google Search Console performance for the audited URL.

When the browser session has a connected Google account, this analyzer finds
the Search Console property that owns the audited URL by itself, pulls that
page's last-28-days performance, and turns it into findings: totals, top
queries, low-CTR queries worth a rewrite, and striking-distance rankings.

Results are cached (quota is limited) and mirrored into MongoDB with the same
shape the /api/search-analytics endpoint uses, so audit pulls become part of
the stored history too.

Skips gracefully - like content_ai and pagespeed - when the audit has no user,
Google is not connected, or no property matches the URL.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from config import get_settings
from services.analyzers.base import (
    AnalyzerResult,
    BaseAnalyzer,
    Finding,
    Severity,
    score_from_findings,
)
from services.analyzers.registry import register_analyzer
from services.cache import get_cache
from services.crawler.parser import PageData
from services.integrations import mongo
from services.integrations.google_oauth import (
    execute_google_call,
    get_search_console_service,
    load_credentials,
)

LOOKBACK_DAYS = 28
DATA_LAG_DAYS = 2  # Search Console data trails ~2 days
TOP_QUERY_LIMIT = 25

#: A query with this many impressions is worth judging by CTR.
_MIN_IMPRESSIONS_FOR_CTR = 100
_LOW_CTR = 0.02
#: Positions 11-20: ranking on page 2, cheapest wins available.
_STRIKING_MIN, _STRIKING_MAX = 10.5, 20.5
_MIN_IMPRESSIONS_STRIKING = 50


def default_range() -> tuple[str, str]:
    end = date.today() - timedelta(days=DATA_LAG_DAYS)
    start = end - timedelta(days=LOOKBACK_DAYS - 1)
    return start.isoformat(), end.isoformat()


def _norm_host(url_or_host: str) -> str:
    host = (urlsplit(url_or_host).hostname or url_or_host).lower()
    return host.removeprefix("www.")


def match_property(site_urls: list[str], page_url: str) -> str | None:
    """Pick the Search Console property that owns `page_url`.

    Prefers the most specific URL-prefix property (longest matching path,
    same scheme) and falls back to a `sc-domain:` property covering the host.
    Pure function - unit-tested without network access.
    """
    page = urlsplit(page_url)
    page_host = _norm_host(page_url)
    page_path = page.path or "/"

    best: tuple[int, str] | None = None
    for site in site_urls:
        specificity: int | None = None
        if site.startswith("sc-domain:"):
            domain = _norm_host(site[len("sc-domain:") :])
            if page_host == domain or page_host.endswith(f".{domain}"):
                specificity = 100  # covers the whole domain: least specific
        else:
            prop = urlsplit(site)
            if _norm_host(site) == page_host:
                prop_path = prop.path or "/"
                if page_path.startswith(prop_path):
                    # Longer prefix wins; same scheme breaks ties.
                    specificity = 1000 + len(prop_path) + (1 if prop.scheme == page.scheme else 0)
        if specificity is not None and (best is None or specificity > best[0]):
            best = (specificity, site)
    return best[1] if best else None


def _page_filter(page_url: str) -> dict[str, Any]:
    return {
        "dimensionFilterGroups": [
            {"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}
        ]
    }


def _skip(reason_id: str, title: str, description: str, recommendation: str) -> AnalyzerResult:
    return AnalyzerResult(
        analyzer="search_console",
        category="search_performance",
        score=None,  # excluded from the weighted overall score
        findings=[
            Finding(
                id=reason_id,
                severity=Severity.INFO,
                title=title,
                description=description,
                recommendation=recommendation,
                impact=1,
                effort=1,
            )
        ],
    )


@register_analyzer
class SearchConsoleAnalyzer(BaseAnalyzer):
    name = "search_console"
    category = "search_performance"
    weight = 1.0

    async def _analyze(self, page: PageData) -> AnalyzerResult:
        user_id = page.user_id
        if not user_id:
            return _skip(
                "search_console.no_session",
                "Search Console data skipped - no user session",
                "This audit was not started from a browser session, so no Google account could be resolved.",
                "Run the audit from the app UI while connected to Google Search Console.",
            )
        if await load_credentials(user_id) is None:
            return _skip(
                "search_console.not_connected",
                "Search Console data skipped - Google not connected",
                "No Google account is connected for this session, so real search performance is unavailable.",
                "Open the Search Console tab and connect your Google account, then re-run the audit.",
            )

        try:
            return await self._analyze_connected(user_id, page)
        except HTTPException as exc:
            # 401 (revoked/expired) or a Google API error: report, never fail the audit.
            return _skip(
                "search_console.unavailable",
                "Search Console data unavailable",
                f"Google Search Console could not be queried: {exc.detail}",
                "Reconnect Google Search Console, then re-run the audit.",
            )

    async def _analyze_connected(self, user_id: str, page: PageData) -> AnalyzerResult:
        cache = get_cache()
        ttl = get_settings().cache_ttl_seconds
        service, creds = await get_search_console_service(user_id)

        sites_key = f"gsc:sites:{user_id}"
        site_urls: list[str] | None = await cache.get(sites_key)
        if site_urls is None:
            result = await execute_google_call(user_id, creds, service.sites().list())
            site_urls = [
                entry["siteUrl"]
                for entry in result.get("siteEntry", []) or []
                if entry.get("siteUrl") and entry.get("permissionLevel") != "siteUnverifiedUser"
            ]
            await cache.set(sites_key, site_urls, ttl)

        if not site_urls:
            return _skip(
                "search_console.no_properties",
                "No verified Search Console properties",
                "The connected Google account has no verified properties.",
                "Verify this site in Search Console, then re-run the audit.",
            )

        site = match_property(site_urls, page.final_url)
        if site is None:
            return AnalyzerResult(
                analyzer=self.name,
                category=self.category,
                score=None,
                findings=[
                    Finding(
                        id="search_console.no_property_match",
                        severity=Severity.INFO,
                        title="No Search Console property matches this URL",
                        description=(
                            f"None of the {len(site_urls)} verified properties cover "
                            f"{page.final_url}, so no search performance could be attached."
                        ),
                        recommendation="Add and verify this site in Search Console to see its search data here.",
                        impact=2,
                        effort=2,
                        evidence={"available_properties": site_urls[:25]},
                    )
                ],
            )

        start_date, end_date = default_range()
        totals_rows = await self._query(
            user_id, creds, service, cache, ttl, site, page.final_url,
            dimensions=["page"], start_date=start_date, end_date=end_date, row_limit=1,
        )
        query_rows = await self._query(
            user_id, creds, service, cache, ttl, site, page.final_url,
            dimensions=["page", "query"], start_date=start_date, end_date=end_date,
            row_limit=TOP_QUERY_LIMIT,
        )

        # Mirror into MongoDB using the same shape as /api/search-analytics.
        # `page` is part of the dimension key, so page-scoped audit pulls never
        # collide with site-wide pulls of the same date range.
        if totals_rows:
            await mongo.save_search_analytics(
                user_id, site, start_date, end_date, ["page"], totals_rows
            )
        if query_rows:
            await mongo.save_search_analytics(
                user_id, site, start_date, end_date, ["page", "query"], query_rows
            )

        return self._build_result(site, page.final_url, start_date, end_date, totals_rows, query_rows)

    async def _query(
        self,
        user_id: str,
        creds: Any,
        service: Any,
        cache: Any,
        ttl: int,
        site: str,
        page_url: str,
        *,
        dimensions: list[str],
        start_date: str,
        end_date: str,
        row_limit: int,
    ) -> list[dict[str, Any]]:
        cache_key = f"gsc:rows:{user_id}:{site}:{page_url}:{','.join(dimensions)}:{start_date}:{end_date}"
        cached: list[dict[str, Any]] | None = await cache.get(cache_key)
        if cached is not None:
            return cached
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions,
            "rowLimit": row_limit,
            **_page_filter(page_url),
        }
        result = await execute_google_call(
            user_id, creds, service.searchanalytics().query(siteUrl=site, body=body)
        )
        rows = result.get("rows", [])
        await cache.set(cache_key, rows, ttl)
        return rows

    def _build_result(
        self,
        site: str,
        page_url: str,
        start_date: str,
        end_date: str,
        totals_rows: list[dict[str, Any]],
        query_rows: list[dict[str, Any]],
    ) -> AnalyzerResult:
        findings: list[Finding] = []
        add = findings.append
        window = {
            "matched_property": site,
            "page": page_url,
            "start_date": start_date,
            "end_date": end_date,
        }

        if not totals_rows:
            add(Finding(
                id="search_console.no_data",
                severity=Severity.WARNING,
                title="No Search Console data for this URL",
                description=(
                    f"Between {start_date} and {end_date} this exact URL recorded no impressions in "
                    f"{site}. Either it is not indexed, it receives no search traffic yet, or the "
                    "indexed URL differs (trailing slash, parameters, or a canonical variant)."
                ),
                recommendation=(
                    "Check the URL in Search Console's URL Inspection tool, confirm it is indexed, "
                    "and make sure internal links point at this exact address."
                ),
                impact=4,
                effort=3,
                evidence=window,
            ))
            return AnalyzerResult(
                analyzer=self.name,
                category=self.category,
                score=score_from_findings(findings),
                findings=findings,
            )

        totals = totals_rows[0]
        clicks = float(totals.get("clicks", 0))
        impressions = float(totals.get("impressions", 0))
        ctr = float(totals.get("ctr", 0))
        position = float(totals.get("position", 0))

        add(Finding(
            id="search_console.summary",
            severity=Severity.INFO,
            title=(
                f"{clicks:,.0f} clicks · {impressions:,.0f} impressions · "
                f"{ctr * 100:.1f}% CTR · avg position {position:.1f}"
            ),
            description=(
                f"Live Search Console performance for this URL from {start_date} to {end_date}, "
                f"via property {site}."
            ),
            recommendation="No action needed - this is the measured baseline for the audit.",
            impact=1,
            effort=1,
            evidence={
                **window,
                "clicks": clicks,
                "impressions": impressions,
                "ctr": ctr,
                "position": position,
            },
        ))

        queries = [
            {
                "query": str(row["keys"][-1]),
                "clicks": float(row.get("clicks", 0)),
                "impressions": float(row.get("impressions", 0)),
                "ctr": float(row.get("ctr", 0)),
                "position": float(row.get("position", 0)),
            }
            for row in query_rows
            if row.get("keys")
        ]

        if queries:
            top = sorted(queries, key=lambda q: q["clicks"], reverse=True)[:10]
            add(Finding(
                id="search_console.top_queries",
                severity=Severity.INFO,
                title=f"Top {len(top)} search queries for this page",
                description="Queries actually sending impressions and clicks to this URL: "
                + ", ".join(q["query"] for q in top[:5])
                + ("…" if len(top) > 5 else ""),
                recommendation="Make sure the page's title, H1 and opening copy match how people phrase these queries.",
                impact=2,
                effort=2,
                evidence={**window, "queries": top},
            ))

            low_ctr = sorted(
                (
                    q
                    for q in queries
                    if q["impressions"] >= _MIN_IMPRESSIONS_FOR_CTR
                    and q["ctr"] < _LOW_CTR
                    and q["position"] <= _STRIKING_MAX
                ),
                key=lambda q: q["impressions"],
                reverse=True,
            )[:10]
            if low_ctr:
                add(Finding(
                    id="search_console.low_ctr",
                    severity=Severity.WARNING,
                    title=f"{len(low_ctr)} queries rank but barely get clicked",
                    description=(
                        "These queries show this page often yet convert under "
                        f"{_LOW_CTR * 100:.0f}% of impressions into clicks: "
                        + ", ".join(f"{q['query']} ({q['ctr'] * 100:.1f}%)" for q in low_ctr[:5])
                    ),
                    recommendation=(
                        "Rewrite the title tag and meta description to speak directly to these queries - "
                        "impressions already exist, so CTR is the cheapest win here."
                    ),
                    impact=4,
                    effort=2,
                    evidence={**window, "queries": low_ctr},
                ))

            striking = sorted(
                (
                    q
                    for q in queries
                    if _STRIKING_MIN <= q["position"] <= _STRIKING_MAX
                    and q["impressions"] >= _MIN_IMPRESSIONS_STRIKING
                ),
                key=lambda q: q["impressions"],
                reverse=True,
            )[:10]
            if striking:
                add(Finding(
                    id="search_console.striking_distance",
                    severity=Severity.WARNING,
                    title=f"{len(striking)} striking-distance queries (page 2)",
                    description=(
                        "These queries rank just off page 1, where small gains move traffic the most: "
                        + ", ".join(f"{q['query']} (pos {q['position']:.1f})" for q in striking[:5])
                    ),
                    recommendation=(
                        "Strengthen this page for these specific queries - deepen the relevant section, "
                        "add internal links with matching anchor text, and improve on-page relevance."
                    ),
                    impact=5,
                    effort=3,
                    evidence={**window, "queries": striking},
                ))

        return AnalyzerResult(
            analyzer=self.name,
            category=self.category,
            score=score_from_findings(findings),
            findings=findings,
        )
