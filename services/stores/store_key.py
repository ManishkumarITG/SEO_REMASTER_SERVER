"""The catalog identity: a store is its host.

`normalize_store_url` (services.backlinks.store_profile) keeps the path, which is
right for display and wrong for identity - `https://shop.com/` and
`https://shop.com/collections/all` must land on the same catalog. Discovery is
origin-scoped anyway (`/products.json` knows nothing about paths), so two
"stores" under different paths on one host merge. That is accepted and decided
here, once: adding `store_path` to the identity later is a breaking index change.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def store_domain_of(url: str) -> str:
    """Lowercased host without a leading "www.", or "" when the URL has no host."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")
