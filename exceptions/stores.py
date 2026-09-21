"""Store catalog crawl errors.

Two classes, because they mean different things to the person reading the crawl
row: the store *has* no catalog we can reach, versus the store *forbids* us.
"""

from __future__ import annotations


class CatalogUnavailable(Exception):
    """The store answered but exposes no crawlable catalog.

    Raised synchronously by the probe -> the POST answers 422 naming what was
    tried, so `example.com` never gets a background job. Carrying `tried` is the
    point of the class: only the discovery layer knows which endpoints it hit,
    and "tried /products.json, /collections.json, /sitemap.xml" is a materially
    better error than "no products found".
    """

    def __init__(self, store_url: str, tried: list[str]) -> None:
        self.store_url = store_url
        self.tried = tried
        self.message = f"No product or collection catalog found at {store_url} (tried: {', '.join(tried)})"
        super().__init__(self.message)


class RobotsDisallowed(Exception):
    """robots.txt forbids our user agent from the whole site. The POST answers 403.

    We honour it: this feature makes hundreds of unattended requests to a third
    party, and ignoring a blanket Disallow at that volume is how an IP ends up on
    a block list.
    """

    def __init__(self, store_url: str) -> None:
        self.store_url = store_url
        self.message = f"{store_url} disallows crawling in its robots.txt"
        super().__init__(self.message)
