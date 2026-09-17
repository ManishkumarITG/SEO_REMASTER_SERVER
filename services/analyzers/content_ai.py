"""AI content analysis via the shared LLMService. Skips gracefully without an API key."""

from __future__ import annotations

from typing import Any

from services.analyzers.base import AnalyzerResult, BaseAnalyzer, Finding, Severity
from services.analyzers.registry import register_analyzer
from services.crawler.parser import PageData, extract_main_text
from services.integrations.llm import get_llm_service

_SYSTEM = (
    "You are an expert SEO content strategist. Analyze pages objectively and "
    "answer in exactly the JSON shape requested."
)

_PROMPT_TEMPLATE = """Analyze this web page's content for SEO.

URL: {url}
Current title: {title}
Current meta description: {meta_description}

Main page text (may be truncated):
<page_text>
{text}
</page_text>

Return a JSON object with exactly these keys:
- "search_intent": the search intent this page targets - "informational", "navigational", "transactional" or "commercial" (add a short qualifier if useful)
- "intent_match_score": integer 0-100, how well the content satisfies that intent
- "eeat_assessment": 2-3 sentences assessing the page's experience, expertise, authoritativeness and trust signals
- "content_gaps": array of exactly 3 short strings, each a missing topic or section that would strengthen the page
- "improved_title": a better title tag (30-60 characters)
- "improved_meta_description": a better meta description (70-160 characters)
- "summary": one paragraph summarizing what the page is about and how strong the content is
"""


def _clamp_int(value: Any, lo: int, hi: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, number))


@register_analyzer
class ContentAIAnalyzer(BaseAnalyzer):
    name = "content_ai"
    category = "content"
    weight = 1.0

    async def _analyze(self, page: PageData) -> AnalyzerResult:
        llm = get_llm_service()
        if not llm.available:
            return AnalyzerResult(
                analyzer=self.name,
                category=self.category,
                score=None,  # None = excluded from the weighted overall score
                findings=[Finding(
                    id="content_ai.skipped", severity=Severity.INFO,
                    title="AI analysis skipped - no API key",
                    description="ANTHROPIC_API_KEY is not set, so AI content analysis did not run.",
                    recommendation=(
                        "Add ANTHROPIC_API_KEY to app/server/.env to enable search-intent, "
                        "E-E-A-T and content-gap analysis."
                    ),
                    impact=1, effort=1)],
            )

        dom = page.dom
        title_node = dom.css_first("title")
        title = (title_node.text() or "").strip() if title_node is not None else ""
        meta_description = ""
        for node in dom.css("meta"):
            if (node.attributes.get("name") or "").strip().lower() == "description":
                meta_description = (node.attributes.get("content") or "").strip()
                break

        text = extract_main_text(dom, max_chars=8000)
        if len(text.split()) < 25:
            return AnalyzerResult(
                analyzer=self.name,
                category=self.category,
                score=None,
                findings=[Finding(
                    id="content_ai.no_content", severity=Severity.INFO,
                    title="Not enough text content for AI analysis",
                    description="After stripping navigation and boilerplate, too little text remained to analyze.",
                    recommendation="Add substantive body content, then re-run the audit.",
                    impact=2, effort=3)],
            )

        prompt = _PROMPT_TEMPLATE.format(
            url=page.final_url,
            title=title or "(none)",
            meta_description=meta_description or "(none)",
            text=text,
        )
        data = await llm.complete_json(prompt, system=_SYSTEM, max_tokens=1500)
        if not isinstance(data, dict):
            data = {}

        findings: list[Finding] = []
        intent = str(data.get("search_intent") or "unknown")
        match_score = _clamp_int(data.get("intent_match_score"), 0, 100)

        if match_score is not None:
            severity = Severity.PASSED if match_score >= 70 else Severity.WARNING
            findings.append(Finding(
                id="content_ai.intent", severity=severity,
                title=f"Search intent: {intent} (match {match_score}/100)",
                description=(
                    f"The model classifies this page's target intent as {intent} and scores "
                    f"how well the content satisfies it at {match_score}/100."
                ),
                recommendation=(
                    "Align the content structure with what searchers with this intent expect to find."
                    if severity is Severity.WARNING else "No action needed."
                ),
                impact=4, effort=3,
                evidence={"search_intent": intent, "intent_match_score": match_score}))

        eeat = str(data.get("eeat_assessment") or "").strip()
        if eeat:
            findings.append(Finding(
                id="content_ai.eeat", severity=Severity.INFO,
                title="E-E-A-T assessment",
                description=eeat,
                recommendation="Strengthen weak signals: author bios, citations, first-hand detail, trust markers.",
                impact=3, effort=3))

        raw_gaps = data.get("content_gaps") or []
        gaps = [str(gap).strip() for gap in raw_gaps if str(gap).strip()][:5] if isinstance(raw_gaps, list) else []
        if gaps:
            findings.append(Finding(
                id="content_ai.gaps", severity=Severity.WARNING,
                title=f"{len(gaps)} content gaps identified",
                description="Topics searchers likely expect that the page does not cover: " + "; ".join(gaps),
                recommendation="Add sections covering the identified gaps.",
                impact=4, effort=3, evidence={"content_gaps": gaps}))

        improved_title = str(data.get("improved_title") or "").strip()
        improved_meta = str(data.get("improved_meta_description") or "").strip()
        if improved_title or improved_meta:
            findings.append(Finding(
                id="content_ai.rewrite", severity=Severity.INFO,
                title="Suggested title and meta description",
                description="AI-suggested replacements tuned for relevance and click-through.",
                recommendation=f"Title: {improved_title} | Meta description: {improved_meta}",
                impact=3, effort=1,
                evidence={"improved_title": improved_title, "improved_meta_description": improved_meta}))

        summary = str(data.get("summary") or "").strip()
        if summary:
            findings.append(Finding(
                id="content_ai.summary", severity=Severity.INFO,
                title="Page summary",
                description=summary,
                recommendation="No action needed - context for the report.",
                impact=1, effort=1))

        return AnalyzerResult(
            analyzer=self.name,
            category=self.category,
            score=float(match_score) if match_score is not None else None,
            findings=findings,
        )
