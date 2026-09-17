"""PageData (produced once per audit, shared by all analyzers) + HTML text helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from selectolax.parser import HTMLParser


@dataclass
class PageData:
    """Everything an analyzer may need about the fetched page."""

    requested_url: str
    final_url: str
    status_code: int
    redirect_chain: list[str]
    headers: dict[str, str]  # lower-cased keys
    html: str
    dom: HTMLParser
    load_time_ms: float
    fetched_at: datetime
    robots_txt: dict[str, Any] = field(default_factory=dict)
    #: True when the response exceeded fetcher.MAX_BYTES and only its first
    #: chunk was read. Analyzers that count things (word count, links, images)
    #: are looking at a partial document, so they can say so.
    truncated: bool = False
    #: Session/user that requested the audit, when there is one. Lets analyzers
    #: reach per-user integrations (e.g. Search Console credentials). The
    #: fetcher never sets this - workers.tasks attaches it after fetching.
    user_id: str | None = None


def parse_html(html: str) -> HTMLParser:
    return HTMLParser(html)


_NOISE_TAGS = ["script", "style", "noscript", "template", "svg"]
_CHROME_TAGS = ["nav", "footer", "header", "aside", "form", "iframe"]


def _text_of(tree: HTMLParser) -> str:
    node = tree.body if tree.body is not None else tree.root
    if node is None:
        return ""
    return " ".join(node.text(separator=" ").split())


def visible_text(dom: HTMLParser) -> str:
    """Body text with scripts/styles removed. Used for word counts."""
    tree = HTMLParser(dom.html or "")
    tree.strip_tags(_NOISE_TAGS)
    return _text_of(tree)


def extract_main_text(dom: HTMLParser, max_chars: int = 8000) -> str:
    """Main content text: strips page chrome (nav/footer/etc.) and truncates for LLM prompts."""
    tree = HTMLParser(dom.html or "")
    tree.strip_tags(_NOISE_TAGS + _CHROME_TAGS)
    text = _text_of(tree)
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated
