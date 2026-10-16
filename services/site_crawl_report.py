"""Generate an AI SEO audit report for a finished crawl, via Gemini or local Ollama.

The flow is: reduce the crawl to a compact JSON of real figures, hand that plus a
fixed audit prompt to the chosen engine, take back Markdown, and render it to a
standalone styled HTML file the browser downloads. The engine only changes who
writes the Markdown; the payload, prompt and page are the same for both.

Two rules shape the payload. First, only REAL numbers go in - the prompt tells
Gemini not to fabricate, so the payload must carry the exact distributions and
metrics rather than raw pages for it to eyeball. Second, it must be small: a
5,000-page crawl cannot be pasted into a prompt, so pages are reduced to
distributions plus a handful of named examples, and the per-template Lighthouse
reports (already one-per-template) go in whole.

Anything the crawl does not measure - Search Console indexation, Merchant-listing
schema - is simply absent from the payload. The prompt is instructed to work with
what is present and note gaps, never to invent the rest.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import UTC, datetime
from typing import Any

from db import site_pages
from models.db import SiteCrawl
from schemas import ReportEngine
from services.markdown_html import render_markdown
from services.report_engines import generator_for, label_for
from services.site_crawl_export import crawl_summary, export_filename

logger = logging.getLogger("seo.site_crawl.report")

#: Title length bands, from the audit prompt.
TITLE_OPTIMAL = (50, 60)
#: Description length bands, from the audit prompt.
DESC_OPTIMAL = (120, 160)
#: Named example pages handed to the model for the "original vs optimized" rewrite
#: and for concrete callouts. A few is enough; the model does not need the corpus.
MAX_EXAMPLES = 8

#: The reference report whose structure and style the output must mirror. Carried
#: in every prompt, as the user asked.
REFERENCE_URL = "https://zaiyan-blue.vercel.app/SEO-Audit.html"

SYSTEM_INSTRUCTION = (
    "You are an expert SEO Analyst and Technical Auditor. You produce a "
    "comprehensive SEO Audit Report in clean Markdown, mirroring the structure, "
    "depth and professional tone of the reference report. You use ONLY the figures "
    "present in the provided JSON and never fabricate numbers. When the JSON lacks "
    "data for a section (for example Search Console indexation or Merchant-listing "
    "schema), you say the data was not collected rather than inventing it."
)

#: The audit brief. This is the report template the output must follow. Kept here
#: verbatim so the report shape is versioned with the code that sends it.
AUDIT_PROMPT = """\
Generate a comprehensive SEO Audit Report from the JSON data below, mirroring the
structure, formatting, data depth and narrative style of the reference report at
{reference_url} (always treat that URL as the style reference).

Follow this exact 6-section structure, using Markdown headings, bold, lists and
GitHub pipe tables so the output parses cleanly into HTML:

# SEO Audit Report

## 01 Executive Summary
- Main heading highlighting the core strength and the main gap.
- A high-level narrative of the technical state and the growth levers.
- Top 3 focus initiatives (performance, meta/on-page gaps, structured-data or
  content gaps), each with the relevant figure and its impact.
- A snapshot metrics block: performance scores, page counts, Lighthouse SEO/
  accessibility/best-practices where available, and the meta title/description
  distribution.

## 02 Overall Website Health
- Scorecards: average page score, and per-template Lighthouse performance,
  accessibility, best-practices and SEO where measured, each with a status label.
- An overview of what was crawled: pages fetched, OK, and any that were broken,
  noindex, orphaned or blocked.

## 03 Technical SEO Findings
Dedicated subsections for each measured area:
### 3.1 Performance by template (Lighthouse)
Table of template -> performance / accessibility / best-practices / SEO plus the
Core Web Vitals (LCP, CLS, TBT, Speed Index). Explain the ranking and conversion
impact and give actionable recommendations.
### 3.2 Broken and redirected pages
Table of the relevant counts, the opportunity, and how to fix them.
### 3.3 Indexation and crawlability
Use noindex, orphan, blocked-by-robots and depth figures. Explain the traffic
opportunity and the fixes. If Search Console indexation data is absent, say so.
### 3.4 Structured data
If the crawl carries no schema/merchant-listing data, state that it was not
collected and recommend collecting it, rather than inventing findings.

## 04 On-Page SEO Findings
- Meta Title Optimization: the distribution (too long >60, too short <50, optimal
  50-60, missing), the impact, and the fix.
- Meta Description Optimization: the distribution (too long >160, too short <120,
  optimal 120-160, missing), the impact, and a real "Original vs Optimized"
  rewrite using one of the example pages in the JSON, with character counts.
- Heading structure: use the heading-skip figure to comment on outline quality.

## 05 Priority Matrix
Rank every finding into P0 (critical / high impact), P1 (high), P2 (medium) and
P3 (low / housekeeping), as a table of Priority | Finding | Why.

## 06 Conclusion & Next Steps
A short closing statement connecting the technical foundation to organic revenue
growth, and a clear next-steps call to action.

Rules: use exact figures from the JSON, keep the tone concise and audit-focused,
and output ONLY the Markdown report with no preamble or code fences.

JSON DATA:
{payload}
"""


def _band(length: int, low: int, high: int) -> str:
    if length == 0:
        return "missing"
    if length < low:
        return "too_short"
    if length > high:
        return "too_long"
    return "optimal"


async def build_payload(row: SiteCrawl) -> dict[str, Any]:
    """The compact, factual JSON handed to Gemini. Distributions, not raw pages."""
    title_dist = {"optimal": 0, "too_short": 0, "too_long": 0, "missing": 0}
    desc_dist = {"optimal": 0, "too_short": 0, "too_long": 0, "missing": 0}
    heading_issue_pages = 0
    score_buckets = {"good_80_plus": 0, "medium_50_79": 0, "poor_under_50": 0, "unscored": 0}
    examples: list[dict[str, Any]] = []
    scanned = 0

    async for batch in site_pages.iter_pages(row.id):
        for doc in batch:
            if not doc.get("status_code") or doc.get("skipped") or doc.get("error"):
                continue
            scanned += 1
            title = (doc.get("title") or "").strip()
            desc = (doc.get("meta_description") or "").strip()
            title_dist[_band(len(title), *TITLE_OPTIMAL)] += 1
            desc_dist[_band(len(desc), *DESC_OPTIMAL)] += 1
            if (doc.get("heading_skips") or 0) > 0:
                heading_issue_pages += 1
            score = doc.get("score")
            if score is None:
                score_buckets["unscored"] += 1
            elif score >= 80:
                score_buckets["good_80_plus"] += 1
            elif score >= 50:
                score_buckets["medium_50_79"] += 1
            else:
                score_buckets["poor_under_50"] += 1
            # Collect examples worth rewriting: a real page with a title or
            # description outside the optimal band.
            if len(examples) < MAX_EXAMPLES and (
                _band(len(title), *TITLE_OPTIMAL) != "optimal"
                or _band(len(desc), *DESC_OPTIMAL) != "optimal"
            ):
                examples.append({
                    "url": doc.get("url"),
                    "title": title,
                    "title_length": len(title),
                    "meta_description": desc,
                    "meta_description_length": len(desc),
                })

    summary = crawl_summary(row)
    try:
        issues = await site_pages.issue_counts(row.id)
    except Exception:  # noqa: BLE001 - a report must not fail over a nice-to-have
        logger.warning("Report %s: issue counts unavailable", row.id)
        issues = {}
    templates = await site_pages.load_template_reports(row.id)

    return {
        "store": {
            "host": row.host,
            "site_name": row.site_name,
            "start_url": row.start_url,
        },
        "crawl": summary,
        "pages_scanned_for_meta": scanned,
        "meta_title_distribution": title_dist,
        "meta_description_distribution": desc_dist,
        "score_distribution": score_buckets,
        "heading_issue_pages": heading_issue_pages,
        "issue_counts": issues,
        "lighthouse_by_template": templates,
        "example_pages": examples,
        "note": (
            "Lighthouse is measured once per TEMPLATE on that template's median "
            "page. Search Console indexation and Merchant-listing / product schema "
            "are not collected by this crawler."
        ),
    }


def _document(row: SiteCrawl, body_html: str, *, engine_label: str = "Gemini") -> str:
    """Wrap the rendered report in a standalone, self-contained HTML page.

    Inline CSS only, no external requests: the file is downloaded and opened from
    disk, so it must render with no network and no local assets. `engine_label`
    credits whichever model wrote it, so a reader knows what to expect of it.
    """
    site = html.escape(row.site_name or row.host or "site")
    by = html.escape(engine_label)
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEO Audit Report - {site}</title>
<style>
  :root {{
    --ink: #10202b; --muted: #5c6b75; --line: #dbe4e6; --accent: #0e7c66;
    --accent-soft: #e3f1ec; --paper: #f4f7f7; --surface: #ffffff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--paper); color: var(--ink);
    font: 16px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  main {{ max-width: 900px; margin: 0 auto; padding: 48px 24px 96px; }}
  .masthead {{ border-bottom: 3px solid var(--accent); padding-bottom: 16px; margin-bottom: 32px; }}
  .masthead p {{ margin: 4px 0 0; color: var(--muted); font-size: 13px; }}
  h1 {{ font-size: 34px; line-height: 1.15; margin: 0 0 8px; }}
  h2 {{
    font-size: 24px; margin: 40px 0 12px; padding-top: 20px;
    border-top: 1px solid var(--line);
  }}
  h3 {{ font-size: 19px; margin: 28px 0 10px; color: var(--accent); }}
  h4 {{ font-size: 16px; margin: 20px 0 8px; }}
  p {{ margin: 12px 0; }}
  ul, ol {{ margin: 12px 0; padding-left: 24px; }}
  li {{ margin: 4px 0; }}
  a {{ color: var(--accent); }}
  code {{
    background: var(--accent-soft); padding: 1px 6px; border-radius: 4px;
    font: 13px/1.4 "SF Mono", "Consolas", monospace;
  }}
  hr {{ border: 0; border-top: 1px solid var(--line); margin: 32px 0; }}
  table {{
    width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px;
    background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
    overflow: hidden;
  }}
  th, td {{ text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); }}
  th {{ background: var(--accent-soft); color: var(--accent); font-weight: 600; }}
  tr:last-child td {{ border-bottom: 0; }}
  .footer {{ margin-top: 48px; color: var(--muted); font-size: 12px; text-align: center; }}
</style>
</head>
<body>
<main>
  <div class="masthead">
    <p>SEO AUDIT</p>
    <p>{site} &middot; generated {generated} by {by}</p>
  </div>
  {body_html}
  <p class="footer">Generated by {by} from crawl {html.escape(row.id)}. Figures are from the
  crawl only; sections marked as not collected require Search Console or rich-results data.</p>
</main>
</body>
</html>
"""


def report_suffix(engine: ReportEngine) -> str:
    """`-report.html` for the default engine, `-ollama-report.html` otherwise, so
    the two downloads of one crawl do not land on the same filename."""
    return "-report.html" if engine == "gemini" else f"-{engine}-report.html"


async def generate_report_html(row: SiteCrawl, *, engine: ReportEngine = "gemini") -> tuple[bytes, str]:
    """-> (HTML bytes, filename). Raises the engine's Error / NotConfigured upward."""
    payload = await build_payload(row)
    prompt = AUDIT_PROMPT.format(
        reference_url=REFERENCE_URL,
        payload=json.dumps(payload, default=str, indent=2),
    )
    service = generator_for(engine)
    markdown = await service.generate_text(prompt=prompt, system_instruction=SYSTEM_INSTRUCTION)
    document = _document(row, render_markdown(markdown), engine_label=label_for(engine))
    # `sitecrawl-<host>-<date>-report.html`, reusing the export naming but with a
    # clean single dot before the extension.
    filename = export_filename(row, "html").replace(".html", report_suffix(engine))
    return document.encode("utf-8"), filename
