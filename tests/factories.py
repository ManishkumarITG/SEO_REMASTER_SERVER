"""Shared test fixtures: PageData factory + fixture HTML strings. No network anywhere."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from services.crawler.parser import PageData

_PARAGRAPH = " ".join(
    ["search engines reward pages that answer the reader question clearly and quickly"] * 30
)

GOOD_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Complete Guide to Technical SEO Audits</title>
  <meta name="description" content="Learn how to run a complete technical SEO audit, fix crawl issues, and improve your rankings with this checklist.">
  <link rel="canonical" href="https://example.com/">
  <link rel="icon" href="/favicon.ico">
  <meta property="og:title" content="The Complete Guide to Technical SEO Audits">
  <meta property="og:description" content="Run a complete technical SEO audit with this practical checklist.">
  <meta property="og:image" content="https://example.com/og.png">
</head>
<body>
  <h1>The Complete Guide to Technical SEO Audits</h1>
  <p>{_PARAGRAPH}</p>
  <h2>Crawling and indexing</h2>
  <p>{_PARAGRAPH}</p>
  <h3>Robots directives</h3>
  <p>{_PARAGRAPH}</p>
  <h2>Page experience</h2>
  <img src="/chart.png" alt="Core Web Vitals chart">
  <img src="/team.jpg" alt="Our SEO team at work">
  <p><a href="/guides/crawling">Crawling guide</a> <a href="/guides/indexing">Indexing guide</a>
     <a href="https://developers.google.com/search">Google Search docs</a></p>
</body>
</html>"""

BROKEN_HTML = """<!doctype html>
<html>
<head>
</head>
<body>
  <h2>Welcome</h2>
  <h4>Some subsection</h4>
  <img src="/a.png">
  <p>Just a few words here.</p>
</body>
</html>"""

DEFAULT_ROBOTS: dict[str, Any] = {
    "url": "https://example.com/robots.txt",
    "fetched": True,
    "status_code": 200,
    "disallow": [],
    "allow": [],
    "sitemaps": [],
}


def make_page(
    html: str,
    *,
    requested_url: str = "https://example.com/",
    final_url: str | None = None,
    status_code: int = 200,
    redirect_chain: list[str] | None = None,
    headers: dict[str, str] | None = None,
    load_time_ms: float = 120.0,
    robots_txt: dict[str, Any] | None = None,
) -> PageData:
    merged_headers = {"content-type": "text/html; charset=utf-8"}
    merged_headers.update({k.lower(): v for k, v in (headers or {}).items()})
    return PageData(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        status_code=status_code,
        redirect_chain=redirect_chain or [],
        headers=merged_headers,
        html=html,
        dom=HTMLParser(html),
        load_time_ms=load_time_ms,
        fetched_at=datetime.now(UTC),
        robots_txt=robots_txt if robots_txt is not None else dict(DEFAULT_ROBOTS),
    )


# --- Store catalog fixtures -------------------------------------------------------

def shopify_product(handle: str, *, pid: int, title: str | None = None, price: str = "18.00",
                    available: bool = True, alt: str | None = None, body: str = "<p>Soft <b>linen</b>.</p>",
                    updated: str = "2026-09-01T10:00:00-04:00") -> dict[str, Any]:
    """One entry as /products.json returns it. `alt=None` mirrors Shopify's usual null."""
    return {
        "id": pid, "handle": handle, "title": title or handle.replace("-", " ").title(),
        "body_html": body, "vendor": "Ember & Oak", "product_type": "Shirts",
        "tags": ["linen", "summer"], "published_at": updated, "updated_at": updated,
        "variants": [{"id": pid * 10, "price": price, "available": available, "sku": f"SKU{pid}"}],
        "images": [{"position": 1, "src": f"https://cdn.shopify.test/{handle}.jpg", "alt": alt}],
        "options": [],
    }


def shopify_collection(handle: str, *, cid: int, title: str | None = None, count: int = 4,
                       description: str = "") -> dict[str, Any]:
    """One entry as /collections.json returns it. NB `image` is a dict or None, not a list."""
    return {
        "id": cid, "handle": handle, "title": title or handle.replace("-", " ").title(),
        "description": description, "products_count": count,
        "published_at": "2026-01-01T00:00:00-05:00", "updated_at": "2026-09-01T00:00:00-04:00",
        "image": {"src": f"https://cdn.shopify.test/c-{handle}.jpg", "alt": None},
    }


def head_html(title: str, description: str = "", canonical: str = "", og_title: str = "",
              body_kib: int = 0) -> str:
    """A product page: real <head>, then an optional large body to exercise the sentinel."""
    metas = [f"<title>{title}</title>"]
    if description:
        metas.append(f'<meta name="description" content="{description}">')
    if canonical:
        metas.append(f'<link rel="canonical" href="{canonical}">')
    if og_title:
        metas.append(f'<meta property="og:title" content="{og_title}">')
    head = "\n".join(metas)
    filler = ("<div>" + "x" * 1000 + "</div>\n") * body_kib
    return f"<!doctype html><html><head>\n{head}\n</head><body>{filler}</body></html>"


SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.test/sitemap_products_1.xml?from=1&amp;to=9</loc></sitemap>
  <sitemap><loc>https://shop.test/sitemap_collections_1.xml</loc></sitemap>
  <sitemap><loc>https://shop.test/sitemap_pages_1.xml</loc></sitemap>
  <sitemap><loc>https://shop.test/es-US/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://evil.test/sitemap_products_1.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_PRODUCTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/products/linen-shirt</loc></url>
  <url><loc>https://shop.test/products/wool-scarf</loc></url>
  <url><loc>https://evil.test/products/pwned</loc></url>
  <url><loc>https://shop.test/products/bad handle/extra</loc></url>
</urlset>"""

SITEMAP_COLLECTIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/collections/summer</loc></url>
  <url><loc>https://shop.test/collections/all</loc></url>
</urlset>"""

#: Entity-expansion bomb. A real XML parser would try to expand this; a <loc> regex must not care.
BILLION_LAUGHS_XML = (
    '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;"><!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
    "<urlset><url><loc>https://shop.test/products/one</loc></url><url><loc>&lol3;</loc></url></urlset>"
)
