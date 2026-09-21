"""SEO gap flags for catalog items. Pure functions, no I/O.

The thresholds are imported from the on-page analyzer so a product's flags here
agree with a full audit of that product's URL. If they were hardcoded twice,
"too short" would eventually mean two different things.

The flag that makes or breaks the feature is `meta_title_is_default`. Shopify
renders <title> from `page_title`, which - when the SEO title metafield is unset -
is just the product name plus a " – Shop Name" suffix. So a store that has set
ZERO SEO titles has ZERO empty <title>s, and a naive "missing meta title" check
reports nothing wrong. Detecting the default is how the headline number stays
honest. Validated live: 3 of 19 products on a real store were defaults while all
19 had a non-empty <title>.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from services.analyzers.on_page import (
    META_DESC_MAX_CHARS,
    META_DESC_MIN_CHARS,
    TITLE_MAX_CHARS,
    TITLE_MIN_CHARS,
)
from services.stores.models import CatalogItem

#: Stable identifiers - the frontend groups on these and Mongo indexes them.
SEO_GAP_FLAGS: tuple[str, ...] = (
    "meta_title_missing",
    "meta_title_short",
    "meta_title_long",
    "meta_title_is_default",
    "meta_description_missing",
    "meta_description_short",
    "meta_description_long",
    "meta_description_is_default",
    "description_missing",
    "image_missing",
    "image_alt_missing",
    "duplicate_meta_title",
    "duplicate_meta_description",
    "canonical_mismatch",
    "collection_empty",
    "not_enriched",
    "page_unreachable",
    "skipped_by_robots",
)

#: How many characters of body text the default meta description is compared on.
#: Shopify truncates body_html into the description; the prefix is what survives.
_DEFAULT_DESC_PREFIX = 120
#: A suffix must appear in this share of titles to be treated as the shop suffix.
_SUFFIX_MAJORITY = 0.6
#: Excerpt stored alongside the full description so list reads stay small.
EXCERPT_CHARS = 200

#: " – Shop", " | Shop", " - Shop", " — Shop": the trailing segment after the last separator.
_SUFFIX_RE = re.compile(r"\s+[|–—-]\s+([^|–—]+?)\s*$")


def _collapse(text: str) -> str:
    return " ".join(text.split()).casefold()


def infer_shop_suffix(titles: Iterable[str]) -> str:
    """The trailing " – X" segment shared by most titles, or "" when there is no majority.

    Self-calibrating and theme-independent: no extra request, and it works for
    "|", "-", "–" and "—" separators alike.
    """
    counts: Counter[str] = Counter()
    total = 0
    for title in titles:
        if not title:
            continue
        total += 1
        match = _SUFFIX_RE.search(title)
        if match:
            counts[_collapse(match.group(1))] += 1
    if not counts or total == 0:
        return ""
    suffix, n = counts.most_common(1)[0]
    return suffix if n / total >= _SUFFIX_MAJORITY else ""


def strip_shop_suffix(title: str, shop_suffix: str) -> str:
    """Remove a trailing " – <shop suffix>" (any separator) if present."""
    if not shop_suffix:
        return title
    match = _SUFFIX_RE.search(title)
    if match and _collapse(match.group(1)) == shop_suffix:
        return title[: match.start()]
    return title


def is_default_title(meta_title: str, name: str, shop_suffix: str) -> bool:
    if not meta_title or not name:
        return False
    return _collapse(strip_shop_suffix(meta_title, shop_suffix)) == _collapse(name)


def is_default_description(meta_description: str, description: str) -> bool:
    if not meta_description or not description:
        return False
    a = _collapse(meta_description)[:_DEFAULT_DESC_PREFIX]
    b = _collapse(description)[:_DEFAULT_DESC_PREFIX]
    return bool(a) and a == b


def _same_path(a: str, b: str) -> bool:
    from urllib.parse import urlsplit

    return urlsplit(a).path.rstrip("/") == urlsplit(b).path.rstrip("/")


def item_gaps(item: CatalogItem, *, shop_suffix: str = "") -> list[str]:
    """Per-item flags. Cross-item flags (duplicates) come from `catalog_gaps`."""
    gaps: list[str] = []

    if item.kind == "collection" and item.products_count == 0:
        gaps.append("collection_empty")
    if not item.description:
        gaps.append("description_missing")
    if not item.featured_image_url:
        gaps.append("image_missing")
    elif not item.featured_image_alt:
        gaps.append("image_alt_missing")

    if not item.enriched:
        if item.enrich_error == "skipped_by_robots":
            gaps.append("skipped_by_robots")
        elif item.enrich_error:
            gaps.append("page_unreachable")
        else:
            gaps.append("not_enriched")
        return gaps

    title = item.meta_title.strip()
    if not title:
        gaps.append("meta_title_missing")
    else:
        if len(title) < TITLE_MIN_CHARS:
            gaps.append("meta_title_short")
        elif len(title) > TITLE_MAX_CHARS:
            gaps.append("meta_title_long")
        if is_default_title(title, item.name, shop_suffix):
            gaps.append("meta_title_is_default")

    desc = item.meta_description.strip()
    if not desc:
        gaps.append("meta_description_missing")
    else:
        if len(desc) < META_DESC_MIN_CHARS:
            gaps.append("meta_description_short")
        elif len(desc) > META_DESC_MAX_CHARS:
            gaps.append("meta_description_long")
        if is_default_description(desc, item.description):
            gaps.append("meta_description_is_default")

    if item.canonical_url and item.url and not _same_path(item.canonical_url, item.url):
        gaps.append("canonical_mismatch")

    return gaps


def catalog_gaps(items: Sequence[CatalogItem]) -> str:
    """Compute every item's flags in place, including cross-item duplicates.

    One pass to infer the shop suffix, one Counter per field for duplicates - O(n).
    Per-item comparison would be O(n^2): 25M string compares at 5,000 items.
    Returns the inferred suffix so the caller can record it.
    """
    suffix = infer_shop_suffix(i.meta_title for i in items if i.enriched)

    title_counts: Counter[str] = Counter()
    desc_counts: Counter[str] = Counter()
    for item in items:
        if item.enriched:
            if item.meta_title.strip():
                title_counts[_collapse(item.meta_title)] += 1
            if item.meta_description.strip():
                desc_counts[_collapse(item.meta_description)] += 1

    for item in items:
        gaps = item_gaps(item, shop_suffix=suffix)
        if item.enriched:
            if item.meta_title.strip() and title_counts[_collapse(item.meta_title)] > 1:
                gaps.append("duplicate_meta_title")
            if item.meta_description.strip() and desc_counts[_collapse(item.meta_description)] > 1:
                gaps.append("duplicate_meta_description")
        item.gaps = gaps
    return suffix


#: Fields whose change means "the SEO-relevant content changed" and earns a
#: revision. Timestamps and crawl ids are deliberately absent.
_HASH_FIELDS = (
    "name", "description", "meta_title", "meta_description", "canonical_url",
    "price", "available", "featured_image_url", "featured_image_alt", "url",
)


def content_hash(item: CatalogItem) -> str:
    payload = {key: getattr(item, key) for key in _HASH_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def build_catalog_record(item: CatalogItem, *, store_domain: str, store_url: str,
                         crawl_id: str, source: str) -> dict[str, Any]:
    """The plain dict `db/store_catalog.py` persists. Keeps `db/` free of any `services/` import."""
    record = item.model_dump(mode="json")
    record.pop("gaps", None)
    record.update(
        store_domain=store_domain,
        store_url=store_url,
        description_excerpt=item.description[:EXCERPT_CHARS].rstrip(),
        seo_flags=list(item.gaps),
        seo_gap_count=len(item.gaps),
        content_hash=content_hash(item) if item.enriched else None,
        last_crawl_id=crawl_id,
        source=source,
    )
    return record
