"""Which template rendered a page.

A store has hundreds of product pages and ONE product template. Anything whose
cost is per-template rather than per-page - a Lighthouse run above all - needs to
know that grouping, or it pays three hundred times for one answer.

Detection is markup-first, URL-second:

1. `<body data-template="product">`. Shopify's own attribute, exact when present.
2. A `template_product` / `template-product` class on `<body>`. The other common
   theme convention, and the one Dawn-derived themes emit.
3. The URL shape, via the same path rules `urls.classify_catalog_url` uses.

Measured on a real store: the home page, a product page and a collection page
carried `data-template="index"`, `"product"` and `"collection"` respectively, and
the same words again in the body class. The URL fallback exists for the themes
that emit neither, and for stores that are not Shopify at all.

Template names are OURS, not the theme's: a theme may call the home page
"index", but a report says "home". Keeping a fixed vocabulary is what lets the
frontend label groups without knowing anything about Shopify.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from services.crawler.urls import handle_ok

#: The vocabulary. Anything unrecognised is "other" rather than invented, so a
#: report never shows a template name that came from a third party's markup.
HOME = "home"
PRODUCT = "product"
COLLECTION = "collection"
BLOG = "blog"
ARTICLE = "article"
PAGE = "page"
SEARCH = "search"
CART = "cart"
ACCOUNT = "account"
POLICY = "policy"
OTHER = "other"

TEMPLATES: tuple[str, ...] = (
    HOME, PRODUCT, COLLECTION, BLOG, ARTICLE, PAGE, SEARCH, CART, ACCOUNT, POLICY, OTHER,
)

#: Theme vocabulary -> ours. Only exact matches are translated; a theme that
#: emits `template_product.custom` still lands on `product` via the prefix split.
_THEME_NAMES: dict[str, str] = {
    "index": HOME,
    "home": HOME,
    "product": PRODUCT,
    "collection": COLLECTION,
    "list-collections": COLLECTION,
    "blog": BLOG,
    "article": ARTICLE,
    "page": PAGE,
    "search": SEARCH,
    "cart": CART,
    "customers/account": ACCOUNT,
    "customers/login": ACCOUNT,
    "customers/register": ACCOUNT,
    "customers/order": ACCOUNT,
    "customers/addresses": ACCOUNT,
    "policy": POLICY,
    "404": OTHER,
    "gift_card": OTHER,
    "password": OTHER,
}

#: `template_product`, `template-product`, and the suffixed variants a theme adds
#: for an alternate template (`template_product.bundle`).
_BODY_CLASS_RE = re.compile(r"\btemplate[_-]([a-z0-9][a-z0-9._/-]*)", re.IGNORECASE)


def _normalise(raw: str | None) -> str | None:
    """A theme's template name mapped into our vocabulary, or None if unusable."""
    if not raw:
        return None
    name = raw.strip().lower()
    if not name:
        return None
    if name in _THEME_NAMES:
        return _THEME_NAMES[name]
    # `product.bundle` and `customers/account` both reduce to their first part;
    # check the full string first so `customers/account` is not read as `customers`.
    head = name.split(".", 1)[0]
    return _THEME_NAMES.get(head)


def template_from_markup(dom: HTMLParser) -> str | None:
    """The template a theme declares, or None when it declares nothing we know."""
    body = dom.css_first("body")
    if body is None:
        return None
    declared = _normalise(body.attributes.get("data-template"))
    if declared is not None:
        return declared
    match = _BODY_CLASS_RE.search(body.attributes.get("class") or "")
    return _normalise(match.group(1)) if match else None


def template_from_url(url: str) -> str:
    """The template a URL shape implies. Always answers; "other" is a real answer."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments:
        return HOME
    first = segments[0].lower()

    if first == "products" and len(segments) == 2 and handle_ok(segments[1]):
        return PRODUCT
    # /collections/<handle>/products/<handle> is a product page reached through a
    # collection, not a collection page.
    if first == "collections":
        if len(segments) == 4 and segments[2].lower() == "products":
            return PRODUCT
        if len(segments) == 2:
            return COLLECTION
        if len(segments) == 1:
            return COLLECTION
    if first == "blogs":
        return ARTICLE if len(segments) >= 3 else BLOG
    if first == "pages":
        return PAGE
    if first == "search":
        return SEARCH
    if first == "cart":
        return CART
    if first in ("account", "customers"):
        return ACCOUNT
    if first == "policies":
        return POLICY
    return OTHER


def detect_template(url: str, dom: HTMLParser | None = None) -> str:
    """The template that rendered this page. Markup wins; the URL is the fallback."""
    if dom is not None:
        declared = template_from_markup(dom)
        if declared is not None:
            return declared
    return template_from_url(url)
