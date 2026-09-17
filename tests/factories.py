"""Shared test fixtures: PageData factory + fixture HTML strings. No network anywhere."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from services.crawler.parser import PageData

_PARAGRAPH = " ".join(
    ["search engines reward pages that answer the reader question clearly and quickly"] * 30
)

GOOD_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Complete Guide to Technical SEO Audits</title>
  <meta name="description" content="Learn how to run a complete technical SEO audit, fix crawl issues, and improve your rankings with this checklist.">
  <link rel="canonical" href="https://example.com/">
  <link rel="icon" href="/favicon.ico">
  <meta property="og:title" content="The Complete Guide to Technical SEO Audits">
  <meta property="og:description" content="Run a complete technical SEO audit with this practical checklist.">
  <meta property="og:image" content="https://example.com/og.png">
</head>
<body>
  <h1>The Complete Guide to Technical SEO Audits</h1>
  <p>{_PARAGRAPH}</p>
  <h2>Crawling and indexing</h2>
  <p>{_PARAGRAPH}</p>
  <h3>Robots directives</h3>
  <p>{_PARAGRAPH}</p>
  <h2>Page experience</h2>
  <img src="/chart.png" alt="Core Web Vitals chart">
  <img src="/team.jpg" alt="Our SEO team at work">
  <p><a href="/guides/crawling">Crawling guide</a> <a href="/guides/indexing">Indexing guide</a>
     <a href="https://developers.google.com/search">Google Search docs</a></p>
</body>
</html>"""

BROKEN_HTML = """<!doctype html>
<html>
<head>
</head>
<body>
  <h2>Welcome</h2>
  <h4>Some subsection</h4>
  <img src="/a.png">
  <p>Just a few words here.</p>
</body>
</html>"""

DEFAULT_ROBOTS: dict[str, Any] = {
    "url": "https://example.com/robots.txt",
    "fetched": True,
    "status_code": 200,
    "disallow": [],
    "allow": [],
    "sitemaps": [],
}


def make_page(
    html: str,
    *,
    requested_url: str = "https://example.com/",
    final_url: str | None = None,
    status_code: int = 200,
    redirect_chain: list[str] | None = None,
    headers: dict[str, str] | None = None,
    load_time_ms: float = 120.0,
    robots_txt: dict[str, Any] | None = None,
) -> PageData:
    merged_headers = {"content-type": "text/html; charset=utf-8"}
    merged_headers.update({k.lower(): v for k, v in (headers or {}).items()})
    return PageData(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        status_code=status_code,
        redirect_chain=redirect_chain or [],
        headers=merged_headers,
        html=html,
        dom=HTMLParser(html),
        load_time_ms=load_time_ms,
        fetched_at=datetime.now(UTC),
        robots_txt=robots_txt if robots_txt is not None else dict(DEFAULT_ROBOTS),
    )
