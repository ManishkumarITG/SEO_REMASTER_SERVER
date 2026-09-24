"""Export a finished crawl as JSON or CSV.

Built on request, never in advance: an export is a file somebody asked for once,
and pre-building one for every crawl would store megabytes nobody downloads.

Both formats STREAM. A 5,000-page crawl carrying its text and link graph is tens
of megabytes, and building the whole document in memory to hand it to a response
would hold two copies of it. `db.site_pages.iter_pages` yields batches; each one
is serialised, emitted and dropped.

The two formats answer different questions and are shaped differently on purpose:

- **JSON** is the complete record - every field of every page, plus the crawl
  summary, the issue counts and the per-template Lighthouse reports. It is for
  feeding another program.
- **CSV** is one flat row per page, for a spreadsheet. Nested values are
  flattened rather than dropped, and the template's Lighthouse scores are
  repeated onto every page that template rendered, so the file pivots.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from db import site_pages
from models.db import SiteCrawl

logger = logging.getLogger("seo.site_crawl.export")

#: Excel on Windows reads a BOM-less UTF-8 CSV as the system codepage, which
#: turns every non-ASCII title into mojibake. The BOM is what makes a
#: double-click open correctly.
CSV_ENCODING = "utf-8-sig"

#: Long free text is kept but flattened: a cell with newlines in it is legal CSV
#: and miserable to read.
_WHITESPACE = re.compile(r"\s+")
MAX_OUTLINE_CHARS = 500
MAX_FINDINGS_CHARS = 500

#: Column order is the reading order: what the page IS, then how it scored, then
#: its content, then its links, then the long text last so it never pushes the
#: useful columns off the screen.
CSV_COLUMNS: tuple[str, ...] = (
    "url",
    "template",
    "page_type",
    "depth",
    "status_code",
    "score",
    "grade",
    "critical_count",
    "warning_count",
    "title",
    "title_length",
    "meta_description",
    "meta_description_length",
    "canonical_url",
    "lang",
    "noindex",
    "nofollow",
    "orphan",
    "h1_count",
    "h2_count",
    "heading_count",
    "heading_skips",
    "heading_outline",
    "word_count",
    "images",
    "images_missing_alt",
    "internal_links",
    "internal_nofollow_links",
    "external_links",
    "discovered_from",
    "final_url",
    "redirect_count",
    "elapsed_ms",
    "truncated",
    "skipped",
    "error",
    "template_performance",
    "template_accessibility",
    "template_best_practices",
    "template_seo",
    "top_findings",
    "text_content",
)


def _flat(value: Any) -> str:
    """One cell: no newlines, no runs of whitespace, never the word None."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _WHITESPACE.sub(" ", str(value)).strip()


def _outline(headings: list[dict[str, Any]] | None) -> str:
    """The heading hierarchy as one readable cell: `h1: Title | h2: Sub`."""
    if not headings:
        return ""
    parts = [f"h{h.get('level', '?')}: {_flat(h.get('text'))}" for h in headings]
    joined = " | ".join(parts)
    return joined[:MAX_OUTLINE_CHARS]


def _findings(findings: list[dict[str, Any]] | None) -> str:
    """Finding titles, most urgent first, as one cell."""
    if not findings:
        return ""
    titles = [_flat(f.get("title")) for f in findings if f.get("title")]
    return "; ".join(titles)[:MAX_FINDINGS_CHARS]


def export_filename(row: SiteCrawl, extension: str) -> str:
    """A filename that says what it is without being opened."""
    host = re.sub(r"[^a-z0-9.-]+", "-", (row.host or "site").lower()).strip("-") or "site"
    stamp = (row.completed_at or row.created_at or datetime.now(UTC)).strftime("%Y%m%d")
    return f"sitecrawl-{host}-{stamp}.{extension}"


def crawl_summary(row: SiteCrawl) -> dict[str, Any]:
    """The crawl itself, as both exports describe it.

    Every bound is included, not just the counters, because a number like "150
    pages" is unreadable months later without "out of a 150-page budget".
    """
    return {
        "crawl_id": row.id,
        "host": row.host,
        "site_name": row.site_name,
        "start_url": row.start_url,
        "status": row.status,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "elapsed_s": row.elapsed_s,
        "limits": {
            "max_pages": row.max_pages,
            "max_depth": row.max_depth,
            "page_types": [t for t in (row.page_types or "").split(",") if t],
        },
        "totals": {
            "pages_discovered": row.pages_discovered,
            "pages_fetched": row.pages_fetched,
            "pages_ok": row.pages_ok,
            "target_pages": row.target_pages or 0,
            "navigation_pages": row.navigation_pages or 0,
            "client_errors": row.client_errors,
            "server_errors": row.server_errors,
            "transport_errors": row.transport_errors,
            "skipped_by_robots": row.skipped_by_robots,
            "skipped_asset": row.skipped_asset,
            "skipped_non_html": row.skipped_non_html,
            "skipped_off_site": row.skipped_off_site,
            "beyond_depth": row.beyond_depth,
            "not_attempted": row.not_attempted,
            "seeded_from_sitemap": row.seeded_from_sitemap,
            "max_depth_seen": row.max_depth_seen,
            "throttled": row.throttled or 0,
            "gave_up_throttled": row.gave_up_throttled or 0,
        },
        "findings": {
            "pages_analyzed": row.pages_analyzed or 0,
            "avg_score": row.avg_score,
            "critical_findings": row.critical_findings or 0,
            "warning_findings": row.warning_findings or 0,
            "orphan_count": row.orphan_count,
            "noindex_count": row.noindex_count,
            "missing_title_count": row.missing_title_count,
            "missing_description_count": row.missing_description_count,
            "broken_page_count": row.broken_page_count,
        },
        "stopped_because": row.aborted_reason,
        "notes": list(row.notes or []),
        "exported_at": datetime.now(UTC),
    }


def _json_default(value: Any) -> str:
    """datetimes and anything else exotic become strings rather than an error."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def stream_json(row: SiteCrawl, *, include_navigation: bool = False) -> AsyncIterator[bytes]:
    """The complete crawl as one JSON document, emitted incrementally.

    Hand-assembled rather than `json.dumps(everything)`: the pages array is the
    large part, and this way only one batch of it is in memory at a time.
    """
    templates = await site_pages.load_template_reports(row.id)
    try:
        issues = await site_pages.issue_counts(row.id)
    except Exception:  # noqa: BLE001 - an export must not fail over a nice-to-have
        logger.warning("Export %s: issue counts unavailable", row.id)
        issues = {}

    head = {
        "crawl": crawl_summary(row),
        "issue_counts": issues,
        "templates": templates,
    }
    prefix = json.dumps(head, default=_json_default, indent=2)[:-2].rstrip()
    yield (prefix + ',\n  "pages": [\n').encode("utf-8")

    first = True
    async for batch in site_pages.iter_pages(row.id, include_navigation=include_navigation):
        chunk = io.StringIO()
        for doc in batch:
            chunk.write("" if first else ",\n")
            chunk.write("    " + json.dumps(doc, default=_json_default))
            first = False
        yield chunk.getvalue().encode("utf-8")
    yield b"\n  ]\n}\n"


async def stream_csv(row: SiteCrawl, *, include_navigation: bool = False) -> AsyncIterator[bytes]:
    """One row per page, flattened for a spreadsheet."""
    templates = await site_pages.load_template_reports(row.id)
    # Repeated onto every page of a template so the file pivots: "average score
    # of pages whose template scores under 50" is then one spreadsheet formula.
    scores: dict[str, dict[str, Any]] = {
        record["template"]: (record.get("lighthouse") or {}) for record in templates
    }

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")

    def drain() -> bytes:
        data = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return data.encode("utf-8")

    # The BOM goes out once, before anything else, or Excel mis-reads the file.
    yield "﻿".encode()
    writer.writeheader()
    yield drain()

    async for batch in site_pages.iter_pages(row.id, include_navigation=include_navigation):
        for doc in batch:
            writer.writerow(_csv_row(doc, scores))
        yield drain()


def _csv_row(doc: dict[str, Any], scores: dict[str, dict[str, Any]]) -> dict[str, str]:
    """One page as flat cells."""
    template = doc.get("template") or ""
    lighthouse = scores.get(template) or {}
    title = doc.get("title") or ""
    description = doc.get("meta_description") or ""
    return {
        "url": _flat(doc.get("url")),
        "template": _flat(template),
        "page_type": _flat(doc.get("page_type")),
        "depth": _flat(doc.get("depth")),
        "status_code": _flat(doc.get("status_code")),
        "score": _flat(doc.get("score")),
        "grade": _flat(doc.get("grade")),
        "critical_count": _flat(doc.get("critical_count")),
        "warning_count": _flat(doc.get("warning_count")),
        "title": _flat(title),
        # Length matters more than the text for a title, and a spreadsheet cannot
        # compute it once the value is quoted.
        "title_length": str(len(title)),
        "meta_description": _flat(description),
        "meta_description_length": str(len(description)),
        "canonical_url": _flat(doc.get("canonical_url")),
        "lang": _flat(doc.get("lang")),
        "noindex": _flat(doc.get("noindex")),
        "nofollow": _flat(doc.get("nofollow")),
        "orphan": _flat(doc.get("orphan")),
        "h1_count": _flat(doc.get("h1_count")),
        "h2_count": _flat(doc.get("h2_count")),
        "heading_count": _flat(doc.get("heading_count")),
        "heading_skips": _flat(doc.get("heading_skips")),
        "heading_outline": _outline(doc.get("headings")),
        "word_count": _flat(doc.get("word_count")),
        "images": _flat(doc.get("images")),
        "images_missing_alt": _flat(doc.get("images_missing_alt")),
        "internal_links": _flat(doc.get("internal_links")),
        "internal_nofollow_links": _flat(doc.get("internal_nofollow_links")),
        "external_links": _flat(doc.get("external_links")),
        "discovered_from": _flat(doc.get("discovered_from")),
        "final_url": _flat(doc.get("final_url")),
        "redirect_count": str(len(doc.get("redirect_chain") or [])),
        "elapsed_ms": _flat(round(doc["elapsed_ms"]) if doc.get("elapsed_ms") else 0),
        "truncated": _flat(doc.get("truncated")),
        "skipped": _flat(doc.get("skipped")),
        "error": _flat(doc.get("error")),
        "template_performance": _flat(lighthouse.get("performance_score")),
        "template_accessibility": _flat(lighthouse.get("accessibility_score")),
        "template_best_practices": _flat(lighthouse.get("best_practices_score")),
        "template_seo": _flat(lighthouse.get("seo_score")),
        "top_findings": _findings(doc.get("findings")),
        "text_content": _flat(doc.get("text_content")),
    }
