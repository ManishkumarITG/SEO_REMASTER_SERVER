"""On-page SEO checks. Pure DOM analysis - no external calls."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

from services.analyzers.base import (
    AnalyzerResult,
    BaseAnalyzer,
    Finding,
    Severity,
    score_from_findings,
)
from services.analyzers.registry import register_analyzer
from services.crawler.parser import PageData, visible_text

_SKIP_HREF_PREFIXES = ("mailto:", "tel:", "javascript:", "data:")
_OG_REQUIRED = ("og:title", "og:description", "og:image")


def _meta_by_name(dom: HTMLParser, name: str) -> str | None:
    for node in dom.css("meta"):
        if (node.attributes.get("name") or "").strip().lower() == name:
            return node.attributes.get("content")
    return None


def _meta_by_property(dom: HTMLParser, prop: str) -> str | None:
    for node in dom.css("meta"):
        if (node.attributes.get("property") or "").strip().lower() == prop:
            return node.attributes.get("content")
    return None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


@register_analyzer
class OnPageAnalyzer(BaseAnalyzer):
    name = "on_page"
    category = "on_page"
    weight = 1.0

    async def _analyze(self, page: PageData) -> AnalyzerResult:
        dom = page.dom
        findings: list[Finding] = []
        add = findings.append

        # --- Title -----------------------------------------------------
        title_node = dom.css_first("title")
        title = (title_node.text() or "").strip() if title_node is not None else ""
        if not title:
            add(Finding(
                id="on_page.title", severity=Severity.CRITICAL,
                title="Missing <title> tag",
                description=(
                    "The page has no <title> (or it is empty). The title is the strongest "
                    "on-page relevance signal and the headline shown in search results."
                ),
                recommendation="Add a unique, descriptive <title> of 30-60 characters that leads with the primary topic.",
                impact=5, effort=1))
        elif len(title) < 30:
            add(Finding(
                id="on_page.title", severity=Severity.WARNING,
                title=f"Title too short ({len(title)} chars)",
                description="Titles under 30 characters usually waste ranking opportunity and look thin in search results.",
                recommendation="Expand the title to 30-60 characters, adding the primary keyword and a differentiator.",
                impact=3, effort=1, evidence={"title": title, "length": len(title)}))
        elif len(title) > 60:
            add(Finding(
                id="on_page.title", severity=Severity.WARNING,
                title=f"Title too long ({len(title)} chars)",
                description="Titles over ~60 characters get truncated in search results, hiding your message.",
                recommendation="Shorten the title to 30-60 characters, keeping the primary keyword near the front.",
                impact=3, effort=1, evidence={"title": title, "length": len(title)}))
        else:
            add(Finding(
                id="on_page.title", severity=Severity.PASSED,
                title=f"Title present and well-sized ({len(title)} chars)",
                description="The <title> exists and is within the 30-60 character sweet spot.",
                recommendation="No action needed.",
                impact=1, effort=1, evidence={"title": title}))

        # --- Meta description --------------------------------------------
        meta_desc = (_meta_by_name(dom, "description") or "").strip()
        if not meta_desc:
            add(Finding(
                id="on_page.meta_description", severity=Severity.WARNING,
                title="Missing meta description",
                description="Without a meta description, search engines improvise a snippet, which usually lowers click-through rate.",
                recommendation="Add a compelling meta description of 70-160 characters summarizing the page and its value.",
                impact=4, effort=1))
        elif not 70 <= len(meta_desc) <= 160:
            add(Finding(
                id="on_page.meta_description", severity=Severity.WARNING,
                title=f"Meta description length off ({len(meta_desc)} chars)",
                description="Descriptions shorter than 70 characters look thin; longer than 160 get truncated in results.",
                recommendation="Rewrite the meta description to 70-160 characters.",
                impact=3, effort=1, evidence={"meta_description": meta_desc, "length": len(meta_desc)}))
        else:
            add(Finding(
                id="on_page.meta_description", severity=Severity.PASSED,
                title=f"Meta description present ({len(meta_desc)} chars)",
                description="The meta description exists and is within the 70-160 character range.",
                recommendation="No action needed.",
                impact=1, effort=1))

        # --- H1 ------------------------------------------------------------
        h1_count = len(dom.css("h1"))
        if h1_count == 0:
            add(Finding(
                id="on_page.h1", severity=Severity.CRITICAL,
                title="No <h1> heading",
                description="The page has no <h1>. Search engines and screen readers use it as the primary topic statement.",
                recommendation="Add exactly one <h1> that states the page topic.",
                impact=4, effort=1))
        elif h1_count > 1:
            add(Finding(
                id="on_page.h1", severity=Severity.WARNING,
                title=f"Multiple <h1> headings ({h1_count})",
                description="More than one <h1> dilutes the page's primary topic signal.",
                recommendation="Keep one <h1>; demote the others to <h2>.",
                impact=2, effort=2, evidence={"count": h1_count}))
        else:
            add(Finding(
                id="on_page.h1", severity=Severity.PASSED,
                title="Exactly one <h1>",
                description="The page has a single <h1> heading.",
                recommendation="No action needed.",
                impact=1, effort=1))

        # --- Heading hierarchy ----------------------------------------------
        levels = [int(node.tag[1]) for node in dom.css("h1, h2, h3, h4, h5, h6")]
        skips: list[str] = []
        prev = 0
        for level in levels:
            if level > prev + 1:
                prev_label = f"h{prev}" if prev else "document start"
                skips.append(f"{prev_label} -> h{level}")
            prev = level
        if skips:
            add(Finding(
                id="on_page.heading_hierarchy", severity=Severity.WARNING,
                title="Heading levels skipped",
                description=(
                    "The heading outline jumps levels (e.g. h2 straight to h4), which weakens "
                    "document structure for crawlers and assistive tech."
                ),
                recommendation="Restructure headings so each level increases by at most one.",
                impact=2, effort=2, evidence={"skips": skips[:10]}))
        else:
            add(Finding(
                id="on_page.heading_hierarchy", severity=Severity.PASSED,
                title="Heading hierarchy is sane",
                description="No skipped heading levels detected.",
                recommendation="No action needed.",
                impact=1, effort=1))

        # --- Image alt coverage -----------------------------------------------
        images = dom.css("img")
        if not images:
            add(Finding(
                id="on_page.image_alt", severity=Severity.PASSED,
                title="No images to check",
                description="The page contains no <img> elements.",
                recommendation="No action needed.",
                impact=1, effort=1))
        else:
            missing = [
                (node.attributes.get("src") or "(no src)")[:120]
                for node in images
                if not (node.attributes.get("alt") or "").strip()
            ]
            coverage = round(100 * (len(images) - len(missing)) / len(images))
            if missing:
                add(Finding(
                    id="on_page.image_alt", severity=Severity.WARNING,
                    title=f"Image alt coverage {coverage}% ({len(missing)}/{len(images)} missing)",
                    description="Images without alt text are invisible to image search and screen readers.",
                    recommendation='Add descriptive alt text to every meaningful image (alt="" only for purely decorative ones).',
                    impact=3, effort=2,
                    evidence={"coverage_pct": coverage, "missing_alt_srcs": missing[:10]}))
            else:
                add(Finding(
                    id="on_page.image_alt", severity=Severity.PASSED,
                    title=f"All {len(images)} images have alt text",
                    description="Every image on the page carries a non-empty alt attribute.",
                    recommendation="No action needed.",
                    impact=1, effort=1))

        # --- Internal vs external links ------------------------------------------
        base_host = _host(page.final_url)
        internal = external = 0
        for node in dom.css("a[href]"):
            href = (node.attributes.get("href") or "").strip()
            if not href or href.startswith("#") or href.lower().startswith(_SKIP_HREF_PREFIXES):
                continue
            absolute = urljoin(page.final_url, href)
            if urlsplit(absolute).scheme not in ("http", "https"):
                continue
            if _host(absolute) == base_host:
                internal += 1
            else:
                external += 1
        if internal == 0:
            add(Finding(
                id="on_page.links", severity=Severity.WARNING,
                title="No internal links found",
                description=(
                    "Pages without internal links pass no authority onward and are harder "
                    "for crawlers to relate to the rest of the site."
                ),
                recommendation="Link to at least a few relevant pages on the same site.",
                impact=3, effort=2, evidence={"internal": internal, "external": external}))
        else:
            add(Finding(
                id="on_page.links", severity=Severity.PASSED,
                title=f"Links: {internal} internal / {external} external",
                description="The page links into the rest of the site.",
                recommendation="No action needed.",
                impact=1, effort=1, evidence={"internal": internal, "external": external}))

        # --- Canonical ---------------------------------------------------------------
        canonical = None
        for node in dom.css("link[rel]"):
            rel = (node.attributes.get("rel") or "").lower()
            if "canonical" in rel.split():
                canonical = node.attributes.get("href")
                break
        if canonical:
            add(Finding(
                id="on_page.canonical", severity=Severity.PASSED,
                title="Canonical tag present",
                description="A rel=canonical link tells search engines the preferred URL for this content.",
                recommendation="No action needed.",
                impact=1, effort=1, evidence={"canonical": canonical}))
        else:
            add(Finding(
                id="on_page.canonical", severity=Severity.WARNING,
                title="Missing canonical tag",
                description="Without rel=canonical, URL variants (tracking params, trailing slashes) can split ranking signals.",
                recommendation='Add <link rel="canonical" href="..."> pointing at the preferred URL.',
                impact=3, effort=1))

        # --- Open Graph -----------------------------------------------------------------
        og_missing = [prop for prop in _OG_REQUIRED if not (_meta_by_property(dom, prop) or "").strip()]
        if og_missing:
            add(Finding(
                id="on_page.open_graph", severity=Severity.WARNING,
                title=f"Missing Open Graph tags: {', '.join(og_missing)}",
                description="Without basic OG tags, shares on social platforms render with poor or missing previews.",
                recommendation="Add og:title, og:description and og:image meta tags.",
                impact=2, effort=1, evidence={"missing": og_missing}))
        else:
            add(Finding(
                id="on_page.open_graph", severity=Severity.PASSED,
                title="Basic Open Graph tags present",
                description="og:title, og:description and og:image are all set.",
                recommendation="No action needed.",
                impact=1, effort=1))

        # --- Word count -------------------------------------------------------------------
        word_count = len(visible_text(dom).split())
        if word_count < 300:
            add(Finding(
                id="on_page.word_count", severity=Severity.WARNING,
                title=f"Thin content ({word_count} words)",
                description="Pages under ~300 words rarely satisfy a search intent on their own.",
                recommendation="Expand the page to cover the topic thoroughly, or consolidate it into a stronger page.",
                impact=4, effort=4, evidence={"word_count": word_count}))
        else:
            add(Finding(
                id="on_page.word_count", severity=Severity.PASSED,
                title=f"Word count OK ({word_count} words)",
                description="The page has a substantive amount of text content.",
                recommendation="No action needed.",
                impact=1, effort=1, evidence={"word_count": word_count}))

        # --- lang attribute ------------------------------------------------------------------
        html_node = dom.css_first("html")
        lang = (html_node.attributes.get("lang") or "").strip() if html_node is not None else ""
        if lang:
            add(Finding(
                id="on_page.lang", severity=Severity.PASSED,
                title=f"lang attribute set ({lang})",
                description="The <html> element declares its language.",
                recommendation="No action needed.",
                impact=1, effort=1))
        else:
            add(Finding(
                id="on_page.lang", severity=Severity.WARNING,
                title="Missing lang attribute",
                description='Without <html lang="...">, search engines and screen readers must guess the page language.',
                recommendation='Add the correct lang attribute to the <html> element (e.g. lang="en").',
                impact=2, effort=1))

        return AnalyzerResult(
            analyzer=self.name,
            category=self.category,
            score=score_from_findings(findings),
            findings=findings,
        )
