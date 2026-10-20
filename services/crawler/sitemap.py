"""Extract <loc> values from a sitemap document.

Moved here unchanged from `services/stores/discovery.py` (which re-imports it)
when the site-wide crawler needed the same primitive to seed its frontier.
Sitemaps are a crawler concern, not a store concern.

Parsed with a bounded regex over <loc>, on purpose. The XML comes from a
user-supplied host. XXE is not reachable on CPython 3.12, but entity-expansion
DoS is - expat expands inline DOCTYPE entities with no limit and the byte cap
bounds input, not the blowup. We need one thing: the text of <loc>. This is
token extraction from an untrusted document, not parsing.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

#: A sitemap index may legitimately list 50,000 URLs; beyond that it is a bomb.
MAX_SITEMAP_LOCS = 50_000
#: A first path segment that names a locale: `fr`, `en-gb`, `pt-BR`.
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")
#: Linear, no nested quantifiers - cannot backtrack catastrophically.
_LOC_RE = re.compile(r"<loc\b[^>]*>(.*?)</loc>", re.IGNORECASE | re.DOTALL)
#: `<loc>` is absent from a sitemap index only when the document is not one.
_SITEMAP_HINTS = ("<sitemapindex", "<urlset")


def parse_sitemap_locs(xml_text: str, *, limit: int = MAX_SITEMAP_LOCS) -> list[str]:
    """Every <loc> value, unescaped, in document order, capped. Never parses XML."""
    locs: list[str] = []
    for match in _LOC_RE.finditer(xml_text):
        value = html.unescape(match.group(1)).strip()
        if value:
            locs.append(value)
            if len(locs) >= limit:
                break
    return locs


def looks_like_sitemap(xml_text: str) -> bool:
    """True when the body is a sitemap or sitemap index rather than an HTML 404 page.

    Some hosts answer /sitemap.xml with 200 and their themed not-found page, so a
    status code alone cannot be trusted to mean "this is a sitemap".
    """
    head = xml_text[:4096].lower()
    return any(hint in head for hint in _SITEMAP_HINTS) or "<loc" in head


def is_sitemap_url(url: str) -> bool:
    """True for a URL that names a nested sitemap rather than a crawlable page.

    `.gz` is matched case-insensitively as well: a gzipped child sitemap is still
    a sitemap, even though the seeder skips it rather than inflating it.
    """
    low = url.lower()
    return low.endswith((".xml", ".xml.gz")) or "sitemap" in low


def is_locale_sitemap(url: str) -> bool:
    """True for a translated copy of the sitemap, e.g. `/fr/sitemap_products_1.xml`.

    A multi-language Shopify store lists one child sitemap per locale, and each
    repeats the same catalogue under `/fr/...`, `/de/...`. Seeding all of them
    multiplies the crawl by the number of languages for pages that differ only in
    translation. Moved here from `services/stores/discovery.py`, which re-imports
    it, so the catalog crawl and the site crawl agree on what a locale copy is.
    """
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return len(segments) >= 2 and bool(_LOCALE_SEGMENT_RE.match(segments[0])) and "sitemap" in segments[-1].lower()
