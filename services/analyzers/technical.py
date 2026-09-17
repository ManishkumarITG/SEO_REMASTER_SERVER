"""Technical SEO checks: HTTP behavior, indexability, and rendering hints."""

from __future__ import annotations

from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from services.analyzers.base import (
    AnalyzerResult,
    BaseAnalyzer,
    Finding,
    Severity,
    score_from_findings,
)
from services.analyzers.registry import register_analyzer
from services.crawler.parser import PageData

FAST_MS = 800.0
SLOW_MS = 2500.0


def _meta_by_name(dom: HTMLParser, name: str) -> str | None:
    for node in dom.css("meta"):
        if (node.attributes.get("name") or "").strip().lower() == name:
            return node.attributes.get("content")
    return None


@register_analyzer
class TechnicalAnalyzer(BaseAnalyzer):
    name = "technical"
    category = "technical"
    weight = 1.0

    async def _analyze(self, page: PageData) -> AnalyzerResult:
        dom = page.dom
        findings: list[Finding] = []
        add = findings.append

        # --- Status code -----------------------------------------------
        status = page.status_code
        if 200 <= status < 300:
            add(Finding(
                id="technical.status_code", severity=Severity.PASSED,
                title=f"HTTP {status}",
                description="The page resolves to a successful status code.",
                recommendation="No action needed.",
                impact=1, effort=1))
        elif status >= 400:
            add(Finding(
                id="technical.status_code", severity=Severity.CRITICAL,
                title=f"HTTP error status {status}",
                description="The page returns an error status; search engines will drop or never index it.",
                recommendation="Fix the server response so the page returns HTTP 200.",
                impact=5, effort=3, evidence={"status_code": status}))
        else:
            add(Finding(
                id="technical.status_code", severity=Severity.WARNING,
                title=f"Unexpected status {status}",
                description="The final response is not a 2xx success.",
                recommendation="Ensure the canonical URL responds with HTTP 200.",
                impact=3, effort=2, evidence={"status_code": status}))

        # --- Redirect chain ----------------------------------------------
        chain_len = len(page.redirect_chain)
        if chain_len == 0:
            add(Finding(
                id="technical.redirects", severity=Severity.PASSED,
                title="No redirects",
                description="The URL resolves directly without redirects.",
                recommendation="No action needed.",
                impact=1, effort=1))
        elif chain_len == 1:
            add(Finding(
                id="technical.redirects", severity=Severity.INFO,
                title="One redirect before the final URL",
                description="A single redirect (e.g. http to https) is normal, but internal links should point at the final URL.",
                recommendation="Update internal links and sitemaps to reference the final URL directly.",
                impact=1, effort=1, evidence={"chain": page.redirect_chain}))
        else:
            add(Finding(
                id="technical.redirects", severity=Severity.WARNING,
                title=f"Redirect chain of {chain_len} hops",
                description="Each redirect hop wastes crawl budget, slows users down, and leaks link equity.",
                recommendation="Collapse the chain so every entry point 301s at most once to the final URL.",
                impact=3, effort=2, evidence={"chain": page.redirect_chain}))

        # --- HTTPS ----------------------------------------------------------
        scheme = urlsplit(page.final_url).scheme
        if scheme == "https":
            add(Finding(
                id="technical.https", severity=Severity.PASSED,
                title="Served over HTTPS",
                description="The final URL uses HTTPS.",
                recommendation="No action needed.",
                impact=1, effort=1))
        else:
            add(Finding(
                id="technical.https", severity=Severity.CRITICAL,
                title="Not served over HTTPS",
                description="HTTPS is a ranking signal and browsers mark plain HTTP pages as not secure.",
                recommendation="Install a TLS certificate and 301-redirect all HTTP traffic to HTTPS.",
                impact=5, effort=3, evidence={"final_url": page.final_url}))

        # --- Response time -----------------------------------------------------
        ms = page.load_time_ms
        if ms < FAST_MS:
            add(Finding(
                id="technical.response_time", severity=Severity.PASSED,
                title=f"Fast server response ({ms:.0f} ms)",
                description="Initial HTML arrived quickly.",
                recommendation="No action needed.",
                impact=1, effort=1, evidence={"load_time_ms": ms}))
        elif ms < SLOW_MS:
            add(Finding(
                id="technical.response_time", severity=Severity.WARNING,
                title=f"Sluggish server response ({ms:.0f} ms)",
                description="The HTML response took noticeably long; server latency drags every other metric down.",
                recommendation="Add caching / a CDN, or profile the backend to bring HTML delivery under 800 ms.",
                impact=3, effort=3, evidence={"load_time_ms": ms}))
        else:
            add(Finding(
                id="technical.response_time", severity=Severity.CRITICAL,
                title=f"Very slow server response ({ms:.0f} ms)",
                description="HTML delivery above 2.5 s severely hurts both crawling and user experience.",
                recommendation="Investigate server performance urgently: caching, database queries, hosting tier.",
                impact=5, effort=4, evidence={"load_time_ms": ms}))

        # --- robots.txt ------------------------------------------------------------
        robots_status = page.robots_txt.get("status_code")
        if robots_status == 200:
            add(Finding(
                id="technical.robots_txt", severity=Severity.PASSED,
                title="robots.txt reachable",
                description="robots.txt responded with HTTP 200.",
                recommendation="No action needed.",
                impact=1, effort=1,
                evidence={"url": page.robots_txt.get("url"), "sitemaps": page.robots_txt.get("sitemaps", [])}))
        else:
            add(Finding(
                id="technical.robots_txt", severity=Severity.WARNING,
                title="robots.txt not reachable",
                description=(
                    f"robots.txt returned status {robots_status!r}. Crawlers fall back to defaults, "
                    "and you lose the sitemap pointer."
                ),
                recommendation="Serve a robots.txt with HTTP 200, even a permissive default, and reference your sitemap in it.",
                impact=2, effort=1,
                evidence={"url": page.robots_txt.get("url"), "status_code": robots_status}))

        # --- noindex (meta robots AND X-Robots-Tag header) ---------------------------
        meta_robots = (_meta_by_name(dom, "robots") or "").lower()
        header_robots = page.headers.get("x-robots-tag", "").lower()
        noindex_sources: list[str] = []
        if "noindex" in meta_robots:
            noindex_sources.append("meta robots")
        if "noindex" in header_robots:
            noindex_sources.append("X-Robots-Tag header")
        if noindex_sources:
            add(Finding(
                id="technical.noindex", severity=Severity.CRITICAL,
                title=f"Page is blocked from indexing ({' + '.join(noindex_sources)})",
                description="A noindex directive tells search engines to drop this page from results entirely.",
                recommendation="Remove the noindex directive if this page should rank.",
                impact=5, effort=1,
                evidence={"meta_robots": meta_robots or None, "x_robots_tag": header_robots or None}))
        else:
            add(Finding(
                id="technical.noindex", severity=Severity.PASSED,
                title="Page is indexable",
                description="No noindex directive found in meta robots or the X-Robots-Tag header.",
                recommendation="No action needed.",
                impact=1, effort=1))

        # --- Viewport -------------------------------------------------------------------
        if (_meta_by_name(dom, "viewport") or "").strip():
            add(Finding(
                id="technical.viewport", severity=Severity.PASSED,
                title="Viewport meta present",
                description="The page declares a responsive viewport.",
                recommendation="No action needed.",
                impact=1, effort=1))
        else:
            add(Finding(
                id="technical.viewport", severity=Severity.WARNING,
                title="Missing viewport meta tag",
                description="Without a viewport meta tag the page renders desktop-sized on phones; mobile-first indexing punishes that.",
                recommendation='Add <meta name="viewport" content="width=device-width, initial-scale=1">.',
                impact=4, effort=1))

        # --- Charset ----------------------------------------------------------------------
        has_meta_charset = dom.css_first("meta[charset]") is not None
        header_ct = page.headers.get("content-type", "").lower()
        has_header_charset = "charset=" in header_ct
        has_http_equiv = any(
            "charset=" in (node.attributes.get("content") or "").lower()
            for node in dom.css("meta")
            if (node.attributes.get("http-equiv") or "").lower() == "content-type"
        )
        if has_meta_charset or has_header_charset or has_http_equiv:
            add(Finding(
                id="technical.charset", severity=Severity.PASSED,
                title="Character encoding declared",
                description="The page declares its charset via meta tag or Content-Type header.",
                recommendation="No action needed.",
                impact=1, effort=1))
        else:
            add(Finding(
                id="technical.charset", severity=Severity.WARNING,
                title="No character encoding declared",
                description="Browsers must guess the encoding, which can garble text.",
                recommendation='Add <meta charset="utf-8"> as the first element inside <head>.',
                impact=2, effort=1))

        # --- Favicon ------------------------------------------------------------------------
        has_favicon = any(
            "icon" in (node.attributes.get("rel") or "").lower()
            for node in dom.css("link[rel]")
        )
        if has_favicon:
            add(Finding(
                id="technical.favicon", severity=Severity.PASSED,
                title="Favicon declared",
                description="A favicon link tag is present.",
                recommendation="No action needed.",
                impact=1, effort=1))
        else:
            add(Finding(
                id="technical.favicon", severity=Severity.INFO,
                title="No favicon link tag",
                description="Browsers will fall back to requesting /favicon.ico; search results may show a generic icon.",
                recommendation='Add <link rel="icon" href="/favicon.ico"> (plus modern sizes).',
                impact=1, effort=1))

        return AnalyzerResult(
            analyzer=self.name,
            category=self.category,
            score=score_from_findings(findings),
            findings=findings,
        )
