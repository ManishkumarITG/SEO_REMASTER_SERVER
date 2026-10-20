"""What a fetched response actually was, beyond its status code.

A status code is not the whole truth on a store behind a bot wall. Shopify's
edge answers some requests with a "Verifying your connection..." interstitial,
sometimes as a 403 and sometimes as a 200, and a crawl that took either at face
value would score a challenge page as a product page. This module is where that
judgement lives, so the rate limiter and (later) the coverage ledger agree on it.

Detection is deliberately narrow. Cloudflare injects a script under
`/cdn-cgi/challenge-platform/` into ORDINARY pages on sites with JavaScript
detections enabled, so that path alone would flag every page as a challenge and
the limiter would back the crawl down to a crawl. Only markers that appear on the
interstitial itself count.

Nothing here solves, bypasses or evades a challenge. A challenge is a host
telling us to slow down, and it is answered by slowing down.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

#: Read this much of the body. The interstitial is ~4 KB and its markers sit in
#: <head>; scanning a 700 KB product page end to end for them is wasted work.
CHALLENGE_SCAN_BYTES = 64 * 1024

#: Titles of the interstitial pages themselves ("Just a moment..." is
#: Cloudflare's generic one; "Verifying your connection..." is Shopify's).
_CHALLENGE_TITLE = re.compile(
    r"<title>\s*(?:verifying your connection|just a moment)", re.IGNORECASE
)
#: Strings that exist only on a challenge page. `_cf_chl_opt` is the config
#: object the challenge script reads; it is never present on a normal page.
_CHALLENGE_MARKERS = (
    "your connection needs to be verified",
    "_cf_chl_opt",
)


def detect_challenge(headers: Mapping[str, str] | None, text: str | None) -> bool:
    """True when a response is a bot-challenge interstitial rather than the page."""
    if headers:
        # Cloudflare labels its own challenge responses; this is the most
        # reliable signal there is, when the edge sends it.
        mitigated = headers.get("cf-mitigated") or headers.get("Cf-Mitigated") or ""
        if mitigated.strip().lower() == "challenge":
            return True
    if not text:
        return False
    head = text[:CHALLENGE_SCAN_BYTES]
    if _CHALLENGE_TITLE.search(head):
        return True
    lowered = head.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)
