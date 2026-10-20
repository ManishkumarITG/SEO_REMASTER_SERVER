"""PageData (produced once per audit, shared by all analyzers) + HTML text helpers."""

from __future__ import annotations

import re
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
    #: The page's visible text, when the producer already extracted it. The site
    #: crawl computes it once per page; without this the on_page analyzer would
    #: re-serialize and re-parse the whole document just to count its words.
    visible_text: str | None = None


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
    return _truncate_words(_text_of(tree), max_chars)


def _truncate_words(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated


def extract_texts(raw_html: str, max_chars: int = 0) -> tuple[str, str]:
    """-> (visible text, main text) from ONE extra tree, with no re-serialization.

    `visible_text` and `extract_main_text` each serialize the caller's DOM back
    to a string and parse that string again, because stripping tags mutates the
    tree and the caller still needs theirs intact. On a 700 KB Shopify page that
    is three serialize-and-parse round trips per page on the event loop. This
    parses the raw HTML once into a scratch tree and strips it in two stages:
    noise tags first (that is the visible text), then page chrome on the same
    tree (that is the main text). Stripping is cumulative, so the second strip
    sees exactly what `extract_main_text` would.

    The results match `visible_text` and `extract_main_text`; the difference is
    only what it costs. `max_chars` 0 skips the main text entirely.
    """
    tree = HTMLParser(raw_html or "")
    tree.strip_tags(_NOISE_TAGS)
    visible = _text_of(tree)
    if max_chars <= 0:
        return visible, ""
    tree.strip_tags(_CHROME_TAGS)
    return visible, _truncate_words(_text_of(tree), max_chars)


# --- Shared DOM lookups ---------------------------------------------------------
#
# These used to be private copies inside the on_page and technical analyzers and
# the store profiler (three near-identical `_meta_by_name`s between them). One
# home means a case-sensitivity or whitespace fix lands once, and the audit and
# the catalog crawler can never disagree about whether a page has a description.
#
# Two return conventions, deliberately: the `meta_*` / `link_by_rel` helpers
# return `None` when the tag is absent (the analyzers branch on that), while
# `first_text` / `first_attr` return "" (the profiler/crawler feed the value
# straight into string fields). Do not blur them.


def meta_by_name(dom: HTMLParser, name: str) -> str | None:
    """Content of <meta name="..."> (case-insensitive), or None when absent."""
    wanted = name.strip().lower()
    for node in dom.css("meta"):
        if (node.attributes.get("name") or "").strip().lower() == wanted:
            return node.attributes.get("content")
    return None


def meta_by_property(dom: HTMLParser, prop: str) -> str | None:
    """Content of <meta property="..."> (case-insensitive), or None when absent."""
    wanted = prop.strip().lower()
    for node in dom.css("meta"):
        if (node.attributes.get("property") or "").strip().lower() == wanted:
            return node.attributes.get("content")
    return None


def link_by_rel(dom: HTMLParser, rel: str) -> str | None:
    """href of the first <link> whose rel list contains `rel`, or None when absent."""
    wanted = rel.strip().lower()
    for node in dom.css("link[rel]"):
        if wanted in (node.attributes.get("rel") or "").lower().split():
            return node.attributes.get("href")
    return None


def first_text(dom: HTMLParser, selector: str) -> str:
    """Whitespace-collapsed text of the first match, or "" when absent."""
    node = dom.css_first(selector)
    if node is None:
        return ""
    return " ".join((node.text() or "").split())


def first_attr(dom: HTMLParser, selector: str, attribute: str) -> str:
    """Whitespace-collapsed attribute of the first match, or "" when absent."""
    node = dom.css_first(selector)
    if node is None:
        return ""
    return " ".join((node.attributes.get(attribute, "") or "").split())


#: selectolax joins text nodes with the separator, so "<b>linen</b>." becomes
#: "linen ." - a stray space before punctuation on every description that has an
#: inline tag. Undo it; nothing legitimate has whitespace before these characters
#: or after an opening bracket.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?%)\]])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[])\s+")


def text_from_html_fragment(html: str, limit: int) -> str:
    """Strip tags out of an HTML fragment (e.g. a Shopify body_html) into capped plain text."""
    if not html:
        return ""
    text = " ".join(HTMLParser(html).text(separator=" ").split())
    text = _SPACE_AFTER_OPEN.sub(r"\1", _SPACE_BEFORE_PUNCT.sub(r"\1", text))
    return text[:limit].rstrip()
