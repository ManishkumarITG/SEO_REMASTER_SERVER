"""The full-analysis Gemini report: crawl + Search Console + schema, in one audit.

What changed versus the previous version
----------------------------------------
1. `build_derived()` pre-computes the cross-checks that make an audit useful
   (canonical vs. internal links, duplicate URL patterns, identical pages, empty
   pages, broken-link sources, per-template title/description bands, H1 issues,
   title-suffix pattern, worst template per metric). An LLM is unreliable at
   counting across 150 pages; Python is not. Gemini is told to use these numbers
   and never re-count.
2. The `pages` list sent to Gemini is trimmed to the fields the report needs
   (no `text_content`, no `outlinks`), which cuts the prompt to a fraction of
   the raw export.
3. The prompt no longer just fills a template. It asks for an analysis pass,
   findings that explain the mechanism behind each problem, platform-specific
   fixes (Shopify theme sections, metafields, URL redirects...), verified example
   rewrites, per-template breakdowns and a priority matrix with effort.
4. Findings with no problem get a **Good** badge and two lines - the report does
   not invent issues, and data that was not collected is stated in one line
   instead of an empty section.

Still Markdown only: this service renders the HTML page.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import urlsplit

from models.db import SiteAnalysis, SiteCrawl
from schemas import ReportEngine
from services.markdown_html import render_markdown
from services.report_engines import generator_for, label_for
from services.site_crawl_report import _document, build_payload, report_suffix

logger = logging.getLogger("seo.site_analysis.report")

REFERENCE_URL = "https://zaiyan-blue.vercel.app/SEO-Audit.html"

# Length bands used for BOTH the derived counts and the prompt, so the numbers in
# the report never disagree with each other. Change them here only.
THRESHOLDS = {
    "title_min": 30,
    "title_max": 60,
    "description_min": 70,
    "description_max": 160,
    "thin_words": 300,
}

# ---------------------------------------------------------------------------
# Derived data: the cross-checks Gemini must not be asked to count itself
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _norm(text: Any) -> str:
    """Collapse whitespace; accepts a string or a list of strings (the crawler stores H1s as a list)."""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(x) for x in text if x)
    return _WS.sub(" ", str(text or "")).strip()


def _path(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _pattern(path: str) -> str:
    """'/collections/frontpage/products/gloria-7l' -> '/collections/frontpage/products/…'"""
    if "?" in path:
        return path.split("?")[0] + "?…"
    segments = path.rstrip("/").split("/")
    if len(segments) <= 2:
        return path
    return "/".join(segments[:-1]) + "/…"


def _crawler_length(page: dict[str, Any], finding_id: str, field: str) -> int:
    """Length as the crawler measured it (its finding evidence), else our own count.

    Keeps the report's bands identical to the crawler's own issue counts.
    """
    for f in page.get("findings") or []:
        if f.get("id") == finding_id and isinstance((f.get("evidence") or {}).get("length"), int):
            return f["evidence"]["length"]
    return len(_norm(page.get(field)))


def _band(length: int, lo: int, hi: int) -> str:
    if length == 0:
        return "missing"
    if length < lo:
        return "too_short"
    if length > hi:
        return "too_long"
    return "optimal"


def build_derived(export: dict[str, Any], limit: int = 25) -> dict[str, Any]:
    """Cross-checks computed from the crawl export (crawl / issue_counts / templates / pages).

    Every list is capped at `limit` entries to keep the prompt small; counts are
    always complete. Field access is defensive so a partial export still works.
    """
    pages_all: list[dict[str, Any]] = export.get("pages") or []
    site_host = (export.get("crawl") or {}).get("host") or ""
    t = THRESHOLDS
    if not pages_all:
        logger.warning("build_derived: export has no pages; derived cross-checks will be empty")

    def is_html_ok(p: dict[str, Any]) -> bool:
        return (
            p.get("status_code") == 200
            and "html" in (p.get("content_type") or "text/html")
            and not p.get("skipped")
            and not p.get("error")
        )

    pages = [p for p in pages_all if is_html_ok(p)]
    by_url = {p["url"]: p for p in pages_all}

    # --- inbound links (self-links excluded) ------------------------------------
    inlinks: dict[str, set[str]] = defaultdict(set)
    for p in pages:
        for target in p.get("outlinks") or []:
            if target != p["url"]:
                inlinks[target].add(p["url"])

    # --- broken / redirected targets and how many pages link to them -----------
    broken = []
    for p in pages_all:
        status = p.get("status_code")
        if status and status != 200 and not p.get("skipped"):
            broken.append(
                {
                    "url": p["url"],
                    "status": status,
                    "linked_from_pages": len(inlinks.get(p["url"], ())),
                    "site_wide": len(inlinks.get(p["url"], ())) >= max(3, int(0.8 * len(pages))),
                }
            )
    broken.sort(key=lambda b: -b["linked_from_pages"])

    # --- canonicals: pages whose canonical points elsewhere, grouped by pattern -
    canon_other = [p for p in pages if p.get("canonical_url") and p["canonical_url"] != p["url"]]
    canon_groups: dict[str, dict[str, Any]] = {}
    for p in canon_other:
        key = _pattern(_path(p["url"]))
        g = canon_groups.setdefault(key, {"url_pattern": key, "pages": 0, "canonical_pattern": _pattern(_path(p["canonical_url"])), "examples": []})
        g["pages"] += 1
        if len(g["examples"]) < 3:
            g["examples"].append(_path(p["url"]))
    canonical_targets = {p["canonical_url"] for p in canon_other}

    # Canonical pages that receive links only from themselves / their own duplicates.
    starved = []
    for target in sorted(canonical_targets):
        dup_urls = {p["url"] for p in canon_other if p["canonical_url"] == target}
        sources = inlinks.get(target, set()) - dup_urls
        if not sources and target in by_url:
            starved.append(_path(target))

    # --- duplicate titles / identical pages / empty pages ----------------------
    title_groups: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        if _norm(p.get("title")):
            title_groups[_norm(p.get("title")).lower()].append(_path(p["url"]))
    dup_titles = [{"title": k, "urls": v} for k, v in title_groups.items() if len(v) > 1]

    # Identical pages = same body text (or same H1 + word count when text is absent),
    # counting only pages that are NOT canonicalised to one another.
    ident_groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for p in pages:
        text = _norm(p.get("text_content"))
        key: Any = ("text", hash(text.lower())) if text else ("shape", _norm(p.get("h1")).lower(), p.get("word_count"))
        ident_groups[key].append(p)
    identical = []
    for grp in ident_groups.values():
        canon = {q.get("canonical_url") or q["url"] for q in grp}
        if len(grp) > 1 and len(canon) > 1:
            identical.append({"urls": [_path(q["url"]) for q in grp], "titles": [_norm(q.get("title")) for q in grp][:6], "word_count": grp[0].get("word_count"), "self_canonical": len(canon) == len(grp)})
    identical.sort(key=lambda g: -len(g["urls"]))

    word_counts = [p.get("word_count") or 0 for p in pages]
    boilerplate = min(word_counts) if word_counts else 0
    empty = [
        {"path": _path(p["url"]), "words": p.get("word_count"), "inlinks": len(inlinks.get(p["url"], ())), "discovered_from": p.get("discovered_from")}
        for p in pages
        if (p.get("word_count") or 0) <= boilerplate + 10 and _path(p["url"]) != "/"
    ]
    thin = sorted(
        ({"path": _path(p["url"]), "template": p.get("template"), "words": p.get("word_count")} for p in pages if (p.get("word_count") or 0) < t["thin_words"]),
        key=lambda x: x["words"] or 0,
    )
    no_inlinks = [
        {"path": _path(p["url"]), "discovered_from": p.get("discovered_from"), "words": p.get("word_count")}
        for p in pages
        if not inlinks.get(p["url"]) and _path(p["url"]) != "/"
    ]

    # --- titles & descriptions: bands per template ------------------------------
    def bands(field: str, finding_id: str, lo: int, hi: int) -> dict[str, Any]:
        per_template: dict[str, Counter] = defaultdict(Counter)
        overall: Counter = Counter()
        worst: list[dict[str, Any]] = []
        for p in pages:
            flagged = any(f.get("id") == finding_id for f in (p.get("findings") or []))
            length = 0 if not _norm(p.get(field)) else _crawler_length(p, finding_id, field)
            # Trust the crawler: a page it did not flag is in range, whatever our own count says.
            b = _band(length, lo, hi) if (flagged or length == 0) else "optimal"
            overall[b] += 1
            per_template[p.get("template") or "other"][b] += 1
            if b != "optimal":
                worst.append({"path": _path(p["url"]), "template": p.get("template"), "band": b, "length": length, "inlinks": len(inlinks.get(p["url"], ())), "current": _norm(p.get(field))[:120]})
        worst.sort(key=lambda w: (-w["inlinks"], w["path"]))
        return {
            "thresholds": {"min": lo, "max": hi},
            "overall": dict(overall),
            "to_fix": sum(v for k, v in overall.items() if k != "optimal"),
            "per_template": {k: dict(v) for k, v in per_template.items()},
            "most_linked_pages_to_fix": worst[:limit],
        }

    titles = bands("title", "on_page.title", t["title_min"], t["title_max"])
    descriptions = bands("meta_description", "on_page.meta_description", t["description_min"], t["description_max"])

    # Brand suffix pattern, e.g. " – Gloria Keg" on 90% of titles.
    suffix_counter: Counter = Counter()
    for p in pages:
        m = re.search(r"\s[–|\-]\s([^–|\-]{2,40})$", _norm(p.get("title")))
        if m:
            suffix_counter[m.group(0)] += 1
    suffix, suffix_n = (suffix_counter.most_common(1)[0] if suffix_counter else ("", 0))
    # Titles that already start with the brand word and still get the suffix ("Gloria 7L … – Gloria Keg").
    brand_repeated = []
    if suffix:
        brand_words = suffix.strip().lstrip("–|-").split()
        first = brand_words[0].lower() if brand_words and len(brand_words[0]) >= 4 else ""
        for p in pages:
            title = _norm(p.get("title"))
            if first and title.lower().endswith(suffix.strip().lower()) and title.lower()[: -len(suffix.strip())].split()[:1] == [first]:
                brand_repeated.append({"path": _path(p["url"]), "title": title, "length": _crawler_length(p, "on_page.title", "title")})

    # --- headings & images -------------------------------------------------------
    no_h1 = [_path(p["url"]) for p in pages if not p.get("h1_count")]
    empty_h1 = [_path(p["url"]) for p in pages if p.get("h1_count") and not _norm(p.get("h1"))]
    multi_h1 = [{"path": _path(p["url"]), "h1_count": p.get("h1_count")} for p in pages if (p.get("h1_count") or 0) > 1]
    skips = [_path(p["url"]) for p in pages if p.get("heading_skips")]
    img_missing = [{"path": _path(p["url"]), "missing_alt": p.get("images_missing_alt"), "images": p.get("images")} for p in pages if p.get("images_missing_alt")]
    img_total = sum(p.get("images") or 0 for p in pages)

    # --- templates & Lighthouse ---------------------------------------------------
    template_counts = Counter(p.get("template") or "other" for p in pages)
    lh_rows = []
    for tpl in export.get("templates") or []:
        lh = tpl.get("lighthouse") or {}
        if not lh:
            continue
        lh_rows.append(
            {
                "template": tpl.get("template"),
                "pages": tpl.get("page_count"),
                "strategy": lh.get("strategy"),
                "tested_url": lh.get("fetched_url") or tpl.get("representative_url"),
                "performance": lh.get("performance_score"),
                "seo": lh.get("seo_score"),
                "accessibility": lh.get("accessibility_score"),
                "best_practices": lh.get("best_practices_score"),
                "lcp_ms": lh.get("lcp_ms"),
                "fcp_ms": lh.get("fcp_ms"),
                "speed_index_ms": lh.get("speed_index_ms"),
                "tbt_ms": lh.get("tbt_ms"),
                "cls": lh.get("cls"),
                "inp_ms": lh.get("inp_ms"),
                "median_words": tpl.get("median_words"),
            }
        )
    lh_rows.sort(key=lambda r: -(r["pages"] or 0))

    def _avg(key: str, strategy: str) -> float | None:
        vals = [r[key] for r in lh_rows if r.get("strategy") == strategy and r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def _worst(key: str, strategy: str, high_is_bad: bool = True) -> dict[str, Any] | None:
        rows = [r for r in lh_rows if r.get("strategy") == strategy and r.get(key) is not None]
        if not rows:
            return None
        r = max(rows, key=lambda x: x[key]) if high_is_bad else min(rows, key=lambda x: x[key])
        return {"template": r["template"], "value": r[key]}

    lighthouse = {
        "strategies_run": sorted({r["strategy"] for r in lh_rows if r.get("strategy")}),
        "templates_tested": len(lh_rows),
        "mobile_avg": {k: _avg(k, "mobile") for k in ("performance", "seo", "accessibility", "best_practices")},
        "desktop_avg": {k: _avg(k, "desktop") for k in ("performance", "seo", "accessibility", "best_practices")},
        "worst_mobile": {
            "performance": _worst("performance", "mobile", high_is_bad=False),
            "lcp_ms": _worst("lcp_ms", "mobile"),
            "fcp_ms": _worst("fcp_ms", "mobile"),
            "speed_index_ms": _worst("speed_index_ms", "mobile"),
            "tbt_ms": _worst("tbt_ms", "mobile"),
            "cls": _worst("cls", "mobile"),
        },
        "rows": lh_rows,
    }

    scores = [p.get("score") for p in pages if p.get("score") is not None]
    grades = Counter(p.get("grade") for p in pages if p.get("grade"))

    return {
        "thresholds": dict(t),
        "host": site_host,
        "pages_analysed": len(pages),
        "pages_fetched": len(pages_all),
        "templates": dict(template_counts),
        "crawl_health": {
            "avg_page_score": round(sum(scores) / len(scores), 1) if scores else None,
            "grades": dict(grades),
            "all_pages_status_200": all(p.get("status_code") == 200 for p in pages),
            "with_canonical": sum(1 for p in pages if p.get("canonical_url")),
            "self_canonical": sum(1 for p in pages if p.get("canonical_url") == p["url"]),
            "canonical_elsewhere": len(canon_other),
            "noindex": sum(1 for p in pages if p.get("noindex")),
            "median_fetch_ms": sorted(p.get("elapsed_ms") or 0 for p in pages)[len(pages) // 2] if pages else None,
            "lang_declared_pct": round(100 * sum(1 for p in pages if p.get("lang")) / len(pages), 1) if pages else None,
            "sitemap_only_pages": [_path(p["url"]) for p in pages if p.get("discovered_from") == "sitemap" and not inlinks.get(p["url"])][:limit],
        },
        "broken_targets": broken[:limit],
        "canonical_groups": list(canon_groups.values()),
        "canonical_pages_with_no_external_links": {"count": len(starved), "paths": starved[:limit]},
        "duplicate_titles": {"groups": len(dup_titles), "pages": sum(len(g["urls"]) for g in dup_titles), "examples": dup_titles[:limit]},
        "identical_pages": identical[:limit],
        "empty_pages": {"boilerplate_words": boilerplate, "count": len(empty), "pages": empty[:limit]},
        "thin_pages": {"threshold_words": t["thin_words"], "count": len(thin), "pages": thin[:limit]},
        "pages_with_no_inlinks": {"count": len(no_inlinks), "pages": no_inlinks[:limit]},
        "titles": titles,
        "descriptions": descriptions,
        "title_suffix": {"suffix": suffix.strip(), "pages_with_suffix": suffix_n, "brand_word_repeated_pages": len(brand_repeated), "examples": brand_repeated[:limit]},
        "headings": {
            "no_h1": no_h1,
            "empty_h1": empty_h1,
            "multiple_h1": multi_h1,
            "heading_level_skips": {"count": len(skips), "pages": skips[:limit]},
        },
        "images": {"total": img_total, "missing_alt": sum(i["missing_alt"] or 0 for i in img_missing), "pages": img_missing[:limit]},
        "lighthouse": lighthouse,
    }


def compact_pages(export: dict[str, Any]) -> list[dict[str, Any]]:
    """Only the per-page fields the report needs (drops text, outlinks, raw findings)."""
    keep = ("url", "template", "status_code", "title", "meta_description", "canonical_url", "h1", "h1_count", "word_count", "images", "images_missing_alt", "noindex", "score", "grade", "discovered_from")
    rows = []
    for p in export.get("pages") or []:
        row = {k: p.get(k) for k in keep if p.get(k) is not None}
        row["finding_ids"] = sorted({f.get("id") for f in (p.get("findings") or []) if f.get("id")})
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You are a senior technical SEO consultant writing a client-facing audit for an "
    "agency. You work in two passes: first you analyse the data (cross-referencing "
    "canonicals, internal links, duplicate URLs, content depth, templates and "
    "Lighthouse results), then you write. Your reports are specific, evidence-led "
    "and consultative: every finding names exact URL patterns, page groups and "
    "numbers, explains in one sentence WHY the problem costs rankings or clicks, "
    "and gives fixes that name the exact theme file, section, setting, field or "
    "attribute to change on the store's platform. You lead with what is healthy, "
    "say so briefly, and never invent problems to fill space. You use ONLY the "
    "figures in the JSON; conversions (ms to s), sums and percentages of JSON "
    "values are allowed, nothing else. British spelling. Clean GitHub-flavoured "
    "Markdown only: no HTML, no code fences around the report, no preamble, no "
    "closing remarks."
)

ANALYSIS_PROMPT = """\
Write a complete SEO audit report for this store. Match the structure and tone of
the reference report at {reference_url} (cover, six numbered sections, findings
with a status badge, data source, evidence table, "The Opportunity" and
"Recommendations", a priority matrix and a conclusion) and reach the depth of a
hand-written audit: findings that connect the data, per-template numbers, real
example rewrites and platform-specific fixes.

You cannot open the reference URL; the blueprint below IS the reference.

=====================================================================
A. HOW TO USE THE DATA
=====================================================================
1. `derived` is the source of truth for every count (when
   `derived.available` is false, the per-page export was not attached: use
   the payload's own totals and issue counts, skip the findings that need
   page-level data, and say so in one line under the Executive Summary).
   Otherwise it is complete and was computed by code from the full crawl: canonical groups, duplicate titles, identical pages,
   empty and thin pages, pages with no inbound links, broken-link targets,
   title and description bands (overall and per template), heading issues,
   image alt gaps, the brand-suffix pattern and Lighthouse per template.
   NEVER re-count these from `pages`; NEVER give a number that differs from
   `derived`. Use `pages` only to look up an individual page's current title,
   description, H1, word count or template when you need an example.
2. Length bands come from `derived.thresholds`. State them in the report
   exactly as given (for example "Too short (< 30)", "Too long (> 60)").
3. `search_console` (URL-inspection sample, top pages/queries) and
   `structured_data` (sampled schema check) exist only when the analysis
   collected them. If `search_console.connected` is false, or
   `structured_data.sampled` is 0, the matching findings get the badge
   **Not Collected** and ONE line saying what would be needed (for example
   "Connect a Search Console property to add indexation coverage."). Do not
   pad them. Sampled data is always labelled with its sample size.
4. Respect every caveat in `analysis.notes`.
5. Platform: read it from the JSON (crawl notes, template names, URL
   patterns such as /collections/ and /products/ mean Shopify). Give fixes in
   that platform's own vocabulary. For Shopify this means, where relevant:
   the "Search engine listing" title and description fields, `theme.liquid`,
   sections and snippets (product card snippet, Collection banner section),
   the `within: collection` filter that creates /collections/…/products/
   links, the `seo.hidden` metafield, URL redirects (Online Store >
   Navigation > URL redirects), "Create a URL redirect" when renaming a
   handle, `image_url` with `srcset`, `fetchpriority="high"`, `loading="lazy"`,
   app and third-party script audits, `font-display: swap`. For other
   platforms, use their equivalents; never mention Shopify features on a
   non-Shopify site.
6. Units: *_ms values to seconds with one decimal ("7126 ms" -> "7.1 s");
   TBT and INP stay in whole milliseconds; CLS with three decimals; scores as
   "NN/100"; percentages with one decimal. Missing values are "—", never 0.
7. Rating labels (use exactly these):
   - Lighthouse scores: 90-100 Excellent | 50-89 Needs Improvement | 0-49 Poor
   - Core Web Vitals (Good / Poor): LCP <= 2.5 s / > 4.0 s | FCP <= 1.8 s /
     > 3.0 s | Speed Index <= 3.4 s / > 5.8 s | CLS <= 0.1 / > 0.25 |
     TBT <= 200 ms / > 600 ms | INP <= 200 ms / > 500 ms; between = Needs
     Improvement. Judge a metric by the WORST template in `derived.lighthouse`.
   - Finding badges, bold on the line under the finding heading:
     **Critical** (a Poor CWV value, a site-wide broken link, canonical pages
     that receive no links, 50%+ of pages outside a band, rich-result
     ineligibility), **Improve** (Needs Improvement or a partial issue),
     **Good** (within thresholds), **Not Collected**.

=====================================================================
B. WHAT MAKES THIS REPORT GOOD (apply to every finding)
=====================================================================
- Connect the dots. Examples of the analysis the reader expects:
  * Canonical tags correct but internal links point at duplicate URLs
    (`derived.canonical_groups` + `canonical_pages_with_no_external_links`):
    say which pattern is linked, which pattern is canonical, and that the
    canonical pages receive no menu or grid links.
  * A broken URL linked from almost every page is a site-wide element
    (cookie banner, footer, header) - one fix, not 143.
  * Identical pages, empty pages found only in the sitemap, cloned products
    or placeholder policies dilute the site and compete with each other.
  * Title problems have a cause: a theme-added brand suffix that doubles the
    brand, a default "Blog – tagged X" pattern, recipe/product names with no
    search words. Name the cause, not only the count.
  * Thin content is judged by the template: say what a page of that type is
    missing (intro, yield, FAQ answers as headings, links to related pages).
- Evidence first. Every finding has at least one Markdown table of real
  data: metric tables, per-template distributions, issue lists with the
  actual paths (show up to 8 paths per cell, then "and N more").
- The Opportunity is a blockquote of 3-5 sentences: the size of the gap in
  numbers, one sentence on the mechanism (why it costs rankings, clicks or
  crawl budget), and what the site gains when fixed.
- Recommendations are 3-6 imperative bullets that name the exact file,
  section, field, attribute or admin setting, the page group, and the
  success check ("Re-crawl: zero internal links to /collections/*/products/*").
  No generic advice ("improve content quality", "optimise images").
- Healthy areas get **Good**, a short table, and 1-2 sentences under a bold
  "What this means" label instead of "The Opportunity". Do not invent work.
- Numbers must be identical wherever they appear (summary tiles, tables,
  priority matrix). Round nothing except as rule A6 says.
- Example rewrites must be real: take the current text from `pages`, show
  its length, write a new title within `derived.thresholds`, containing the
  words a shopper searches for, keeping the brand suffix pattern the site
  uses, and count its characters carefully before printing the count.
- Target length: 2,000-3,000 words of prose plus the tables. Tight, no
  filler sentences, no repeated numbers.

=====================================================================
C. MARKDOWN CONVENTIONS (the renderer turns these into the page design)
=====================================================================
- One H1 (report title). H2 for the six numbered sections. H3 for numbered
  findings. Bold text for card labels. Horizontal rule (---) between the
  cover and each numbered section.
- Tables for every metric group, distribution, issue list and the priority
  matrix. Inline code for paths, attributes, files and fields.
- "**The Opportunity**" then a blockquote. "**Recommendations**" then bullets.
- Each Executive Summary card and each Health callout is a bold title line
  followed by one or two plain sentences.

=====================================================================
D. BLUEPRINT (this exact order)
=====================================================================

COVER
- Brand name in capitals on its own line.
- Audit month ("September 2026").
- H1: "SEO Audit Report"
- One sentence: "A complete Technical & On-Page SEO evaluation of <host>,
  translating raw crawl data into a clear path to organic growth."
- Table (Field | Detail): Client, Website, Audit Date (full date), Scope
  ("Technical & On-Page"), Platform, Pages Crawled (fetched / analysed),
  Data Sources (only those present: site crawl, Lighthouse <version> mobile
  and/or desktop, Google Search Console, structured-data check).

---

## 01 · Executive Summary
- Bold headline sentence: the strength first, then the next phase (for
  example "The foundation is clean. The next phase is speed and content.").
- Paragraph 1: what the report covers, the data sources and the date.
- Paragraph 2: the strongest results with numbers, ending "Three focused
  initiatives now stand between the current state and a materially stronger
  organic footprint."
- Exactly three initiative cards (bold title + 1-2 sentences with the key
  numbers). Pick the three biggest levers in the data, in this priority:
  site-wide broken link > canonical pages without links > mobile LCP > rich
  results ineligibility > not-indexed pages > titles/descriptions at scale >
  duplicate/empty pages > content depth.
- **Snapshot — Key Metrics**: a table (Metric | Result | Detail) with exactly
  eight rows of real figures. Fill in this order and skip any metric that
  was not collected, continuing down the list until eight rows are filled:
  Crawl health score (avg page score; grades) | Mobile performance (avg score;
  range; LCP range) | Desktop performance | Lighthouse SEO (avg; templates at
  100) | Page indexation (Search Console) | Indexability (crawl: indexable,
  canonicalised, noindex) | Merchant listings | Product snippets | Broken
  links (targets; linked from N pages) | Meta titles (to fix; short/long/in
  range) | Meta descriptions (to fix; long/missing/in range) | H1 headings
  (missing; empty; multiple) | Internal linking (canonical pages with no
  links) | Image alt text (coverage %).

---

## 02 · Overall Website Health
- Bold headline: "A scorecard, at a glance." and one line naming the data
  sources and audit date.
- **Scorecard** table (Area | Score | Rating | Note): Crawl health, Mobile
  performance, Desktop performance (if run), Lighthouse SEO, Accessibility,
  Best Practices, Average on-page score. Mark the weakest "Top priority".
- **Mobile performance by template**: table (Template | Pages | Performance |
  LCP | FCP | Speed Index | TBT | CLS | SEO | Accessibility) from
  `derived.lighthouse.rows`, one row per template, sorted by pages
  descending. Add a Desktop table if a desktop run exists.
- **Indexability**: if Search Console is connected, "X of Y sampled pages
  indexed" and a table (Status | Pages). Otherwise the crawl view: table
  (Status | Pages) with Indexable self-canonical / Canonicalised to another
  URL / Noindex / Blocked, and the indexable percentage.
- Three callout cards (bold number + title, one sentence): pick the three
  most striking single figures from `derived` (for example canonical
  product pages with no links, identical collection pages, pages carrying
  the broken link, merchant listings blocked by one field).

---

## 03 · Technical SEO Findings
- Bold headline: "Where the technical opportunities live."
- Number findings 3.1, 3.2, … in the order below, skipping only the ones the
  rules allow you to skip, and renumber so there are no gaps.
- Finding layout, always:
    ### 3.N Title
    **Badge** · Data source line ("Lighthouse 13.5.0 — mobile, 8 templates";
    "Crawl — 144 HTML pages"; "Google Search Console — sampled 40 URLs")
    Evidence table(s)
    **The Opportunity** + blockquote   (or **What this means** if Good)
    **Recommendations** + bullets

  Mobile Page Performance — headline "NN/100" (mobile average) with rating
  and the score range across templates. Table (Metric | Range across
  templates | Worst template | Good threshold | Status) for LCP, FCP, Speed
  Index, TBT, CLS, INP (INP "—" with a note that it needs field data).
  Then the per-template table from Section 02 is NOT repeated; refer to it.
  Recommendations target the LCP element per template (hero banner, first
  gallery image), `fetchpriority`, lazy-loading, image sizing, app/script
  audit, fonts, re-test.

  Desktop Page Performance — same layout with desktop values, or **Not
  Collected** in one line.

  Page Indexation (Search Console) — table (Reason / coverage state | Pages
  | Source) from the URL-inspection sample; one recommendation per reason
  plus sitemap resubmission. **Not Collected** in one line if not connected.

  Broken Links & Status Codes — from `derived.broken_targets` and crawl
  totals: table (Response | URLs | Detail) with 200 HTML, each non-200 URL
  (status, linked from N pages, site-wide or not), redirects, non-HTML
  files. Skip only if there are no non-200 targets and no redirects.

  Canonicals, Duplicate URLs & Internal Linking — from
  `derived.canonical_groups`, `canonical_pages_with_no_external_links`,
  `duplicate_titles`, `pages_with_no_inlinks`: table (URL pattern | URLs |
  Canonical points to | Linked from). Explain the link-equity mechanism.
  Skip only if there are no canonical groups and no pages without inlinks.

  Duplicate, Empty & Low-Value Pages — from `identical_pages`,
  `empty_pages`, `crawl_health.sitemap_only_pages` and any cloned or
  placeholder content you can see in `pages` (identical titles and
  descriptions on two products, a policy page with a one-line body): table
  (Issue | Pages | URLs). Recommendations: keep / merge / hide / delete with
  redirects, and which page to keep. Skip if all lists are empty.

  Structured Data — Merchant Listings — table (Issue | Items) of missing or
  invalid fields from `structured_data`; how many products are "one field
  away". **Not Collected** in one line if not sampled.

  Structured Data — Product Snippets — same for `aggregateRating`, `review`
  and other product-snippet gaps.

  Crawlability & Indexability (crawl) — always included, usually **Good**:
  table (Check | Result) for status 200 share, canonical tags, self vs
  other canonical, noindex, robots-blocked, sitemap-seeded URLs, pagination
  canonicals, median HTML fetch time, lang attribute. Two short
  recommendations (protect the baseline; keep the sitemap the source of
  truth).

---

## 04 · On-Page SEO Findings
- Bold headline that matches the data ("Strong Lighthouse scores. Now, the
  content layer." — say "perfect" only if the SEO score is 100).
- Same finding layout and numbering rules (4.1, 4.2, …).

  Lighthouse Category Scores — table (Category | Mobile | Desktop | Range
  across templates): SEO, Accessibility, Best Practices, plus the crawler's
  average on-page score.

  Meta Title Optimisation — badge, source "Crawl — N pages". Distribution
  table (Band | Pages | Share) using the threshold labels, then a
  per-template table (Template | Too short | Too long | In range). One line
  in the style "64 too short (< 30) · 25 too long (> 60) · 55 in range".
  Opportunity must name the CAUSES (brand suffix, default patterns, product
  or recipe names without search words, duplicate-URL copies inflating the
  duplicate count). Then **Example rewrites**: a table (Page | Current
  (N chars) | Suggested (N chars)) with 4-5 real pages chosen from
  `derived.titles.most_linked_pages_to_fix` covering different templates
  (homepage or main collection, a product, a blog post, a page, a tag page
  if any). Then **Top titles to fix**: a table of the 15 most-linked pages
  from that list (Page | Template | Length | Issue | Current title).

  Meta Description Optimisation — same layout: distribution with Too long /
  Missing / Too short / In range, per-template table, causes (auto-filled
  excerpts, missing on listings/policies), then **Example rewrite** for one
  real page: "Current · N characters" with the current text quoted (cut at
  160 characters with "…"), then "Suggested · N characters" with your
  rewrite of 140-155 characters as a blockquote, then **Top descriptions to
  fix** (15 rows).

  Heading Structure — table (Issue | Pages | Where) for No H1, Empty H1,
  Multiple H1s, Skipped heading levels, from `derived.headings`. Name the
  templates and the theme section that should output the H1. Skip if all
  are zero (then say so in one line under Lighthouse Category Scores).

  Content Depth & Thin Pages — from `derived.thin_pages`,
  `empty_pages`, template `median_words` and the blog/article pages in
  `pages`: table (Template | Pages | Median words | Pages under threshold).
  Judge by template: what a page of that type is missing; how tag or
  listing pages could become hubs; related-content and internal links.
  Recommendations include a template upgrade and structured data to add or
  validate (state that schema was not checked if `structured_data.sampled`
  is 0). Skip if there are no thin pages.

  Image Alt Text — coverage % from `derived.images`, table (Page | Images
  missing alt | Total images). Usually **Good** when coverage is above 95%.

---

## 05 · Priority Matrix
- Bold headline: "The order of operations." and the line "Ranked by
  combined impact and effort. Start at the top."
- Table (Priority | Issue | Key figure | Effort | Section) with 10-17 rows,
  one per concrete issue found above, ordered P0 -> P3. Effort is Low /
  Medium / High. Section is the finding number (for example 3.4).
    P0 = site-wide broken link, canonical pages receiving no links, Poor
         mobile CWV on all templates, rich-result ineligibility, missing
         H1 on key templates.
    P1 = descriptions/titles at scale, identical pages competing, cloned
         products, placeholder policies, not-indexed groups.
    P2 = content depth, tag/listing hubs, empty or unlinked pages,
         homepage H1, structured-data enhancements.
    P3 = heading skips, alt text, URL renames, accessibility, single-page fixes.
  Key figure cells use the exact numbers ("LCP 6.7–9.2 s · score 64–70").
  Only list issues that exist in the data.

---

## 06 · Conclusion & Next Steps
- Bold headline: "A strong foundation. A defined path forward." (adapt it if
  the foundation is not strong).
- One paragraph (3-4 sentences) restating the strongest results with their
  numbers and naming the three levers of the next phase.
- Final line: "Fix the P0 items first, re-crawl, and compare against the
  numbers in this report."

---

FOOTER
- "<BRAND> · SEO Audit Report · <Month YYYY>"
- "Confidential — Prepared for <Brand>"
- "Source: site crawl <crawl_id first 8 chars>, N URLs, and Lighthouse
  <version> <strategy> tests, <full date>."

Output ONLY the Markdown report - no preamble, no code fences, no raw HTML.

JSON DATA:
{payload}
"""


# ---------------------------------------------------------------------------
# Payload and generation
# ---------------------------------------------------------------------------


_EXPORT_ATTRS = ("export_json", "export", "result_json", "result", "data", "raw", "snapshot")


def _looks_like_pages(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and isinstance(value[0], dict) and "url" in value[0] and ("title" in value[0] or "status_code" in value[0])


def _find_export(payload: dict[str, Any], crawl: SiteCrawl) -> dict[str, Any] | None:
    """Locate the crawl export (crawl / issue_counts / templates / pages) without
    assuming where the project keeps it. Returns None when nothing is found."""
    if _looks_like_pages(payload.get("pages")):
        return payload
    for key, value in payload.items():                    # pages nested under another key
        if isinstance(value, dict) and _looks_like_pages(value.get("pages")):
            return value
        if _looks_like_pages(value):
            return {**payload, "pages": value}
    for attr in _EXPORT_ATTRS:                            # export stored on the crawl row
        value = getattr(crawl, attr, None)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict) and _looks_like_pages(value.get("pages")):
            return value
    return None


async def build_analysis_payload(analysis: SiteAnalysis, crawl: SiteCrawl, export: dict[str, Any] | None = None) -> dict[str, Any]:
    """The crawl report payload, plus derived cross-checks and the analysis's
    Search Console and schema data.

    `export` is the crawl export dict (crawl / issue_counts / templates / pages),
    the same structure as the downloadable crawl JSON. Pass it in from the
    service layer when you have it; otherwise the function looks for it in the
    payload and on the crawl row, and falls back to a payload-only report.
    """
    payload = await build_payload(crawl)
    export = export or _find_export(payload, crawl)

    if export is None:
        logger.warning("analysis report: crawl export with per-page data not found; derived cross-checks skipped")
        payload["derived"] = {"available": False, "reason": "per-page crawl export not available to the report"}
    else:
        payload["derived"] = {"available": True, **build_derived(export)}
        payload["pages"] = compact_pages(export)      # replaces the heavy page list
        payload.pop("issue_counts", None)              # superseded by derived

    extra = analysis.result_json or {}
    payload["search_console"] = extra.get("gsc", {"connected": False})
    payload["structured_data"] = extra.get("schema", {"sampled": 0})
    payload["analysis"] = {
        "requested_options": analysis.options or {},
        "indexed_sampled": analysis.indexed_count,
        "not_indexed_sampled": analysis.not_indexed_count,
        "notes": list(analysis.notes or []),
    }
    return payload


async def generate_analysis_report_html(
    analysis: SiteAnalysis,
    crawl: SiteCrawl,
    export: dict[str, Any] | None = None,
    *,
    engine: ReportEngine = "gemini",
) -> tuple[bytes, str]:
    """-> (HTML bytes, filename). Raises the engine's Error / NotConfigured upward.

    `export`: the crawl export dict (same shape as the downloadable crawl JSON).
    Build it in the service layer, where the DB session is, and pass it here so
    the derived cross-checks run on real per-page data.

    `engine`: who writes the Markdown. The payload, prompt and page are the same
    for both; only the author - and the masthead credit - changes.
    """
    payload = await build_analysis_payload(analysis, crawl, export=export)
    prompt = ANALYSIS_PROMPT.format(
        reference_url=REFERENCE_URL,
        payload=json.dumps(payload, default=str, indent=1),
    )
    logger.info("analysis report prompt: %d characters (engine=%s)", len(prompt), engine)
    service = generator_for(engine)
    # Tip: this prompt benefits from a thinking-enabled model with a low
    # temperature (0.2-0.4) and a generous output ceiling. A small local model
    # follows the blueprint less closely; the derived numbers still keep it honest.
    markdown = await service.generate_text(prompt=prompt, system_instruction=SYSTEM_INSTRUCTION)
    document = _document(crawl, render_markdown(markdown), engine_label=label_for(engine))
    host = (analysis.host or "site").replace("/", "-")
    stamp = (analysis.completed_at or analysis.created_at).strftime("%Y%m%d")
    return document.encode("utf-8"), f"analysis-{host}-{stamp}{report_suffix(engine)}"