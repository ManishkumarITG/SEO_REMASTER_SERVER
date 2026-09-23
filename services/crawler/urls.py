"""URL normalisation and link extraction for the site-wide crawler.

Pure functions, no I/O, because every one of them is a dedupe decision and
dedupe decisions are what make a crawl terminate. Two facts drive the design:

- A crawl that treats `/a`, `/a?utm_source=x` and `/a#top` as three URLs fetches
  the same page three times and reports it three times. Normalisation is the
  only defence, and it has to be applied at ENQUEUE time (before `seen`), not at
  report time.
- A crawl that normalises too hard under-reports. `/a` and `/a/` really can be
  different documents, and a query string really can select content, so those
  are preserved. Only fragments and known tracking parameters are dropped -
  things that provably cannot change the response body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

#: Query keys that are analytics decoration: every one is added by an ad network
#: or a share button and none of them reaches the origin routing layer. Dropping
#: them collapses the "same page, 40 campaign tags" explosion that otherwise eats
#: the whole page budget on a marketing site.
TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "gclid", "gclsrc", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "twclid",
    "igshid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "hsa_cam", "hsa_grp", "hsa_ad",
    "yclid", "ttclid", "li_fat_id", "vero_id", "s_kwcid", "ef_id", "epik", "irclickid",
})

#: Extensions we refuse before spending a request. The content-type check in the
#: fetch path catches these too, but a 40 MB video is a wasted request even when
#: the body is discarded - and on a big site there are thousands of them.
ASSET_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp", ".tiff",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".ogg", ".flac", ".m4a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf",
    ".zip", ".gz", ".tar", ".bz2", ".7z", ".rar", ".dmg", ".exe", ".msi", ".apk",
    ".css", ".js", ".mjs", ".map", ".json", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".rss", ".atom",
})

#: Per-page ceiling on kept internal links. A nav-heavy template can list 600
#: links, almost all of them the same header and footer on every page; the graph
#: value of the tail is nil and 5,000 pages x 600 strings is not.
MAX_LINKS_PER_PAGE = 300

#: Characters a host may contain. Deliberately a whitelist: `urlsplit` happily
#: reports a "hostname" of `not a url at all` for a string somebody typed, and a
#: host with a space in it is not a host - it is a failed parse that would
#: otherwise reach DNS.
_HOST_CHARS = re.compile(r"^[a-z0-9._~%:\[\]-]+$")
#: A leading `scheme:` in something a person typed. Used only to tell "no scheme,
#: assume https" from "a scheme we do not crawl".
_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def normalize_url(href: str, base: str | None = None) -> str | None:
    """Absolute, fragment-free, tracking-free URL - or None when not crawlable.

    None means "this is not an http(s) document URL we could fetch": a
    mailto:/tel:/javascript: href, a malformed one, or an empty string. Callers
    treat None as "ignore", never as an error.
    """
    if not href or not href.strip():
        return None
    candidate = href.strip()
    if base:
        candidate = urljoin(base, candidate)
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    try:
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not host or not _HOST_CHARS.match(host):
        return None

    netloc = host
    try:
        port = parts.port
    except ValueError:
        return None
    # A default port is the same origin written two ways; anything else is load-bearing.
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    query = ""
    if parts.query:
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in TRACKING_PARAMS]
        # Sorted so ?a=1&b=2 and ?b=2&a=1 are one entry in `seen`. Order is not
        # significant to any router that also accepts the reverse.
        query = urlencode(sorted(kept), doseq=True)

    # "" and "/" are the same document; every other path is kept verbatim, trailing
    # slash included (/a and /a/ are allowed to differ, and on some hosts do).
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def normalize_seed_url(value: str) -> str | None:
    """Normalise a URL a PERSON typed, or None when it cannot be one.

    The only difference from `normalize_url` is that a missing scheme is assumed
    to be https rather than treated as a relative path: someone typing
    `example.com/blog` into a form means a site, but `urlsplit` reads it as a
    path with no host. Kept separate so link extraction, which must never invent
    a scheme for an href, cannot accidentally use it.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    if not _HAS_SCHEME.match(candidate):
        # No scheme at all -> assume https. A value that HAS a scheme keeps it,
        # so `mailto:someone@example.com` stays a mailto and is rejected below
        # rather than being rewritten into a website.
        candidate = f"https://{candidate.lstrip('/')}"
    return normalize_url(candidate)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def same_site(url: str, host: str, *, include_subdomains: bool = False) -> bool:
    """True when `url` belongs to the crawl own site.

    With `include_subdomains`, a subdomain of the SEED host counts - not a
    sibling of its registrable domain. There is no public-suffix list here, so
    `endswith` against the seed is the only claim that is safe to make:
    blog.example.com matches a crawl seeded at example.com, and a crawl seeded at
    www.example.com does not wander into blog.example.com.
    """
    target = host_of(url)
    if not target or not host:
        return False
    if target == host:
        return True
    return include_subdomains and target.endswith(f".{host}")


#: A catalog handle: the last path segment of /products/<handle>. Conservative on
#: purpose - anything with a slash, a space or a leading dot is not a handle, it
#: is a path we misread.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")

#: Shopify serves these two as views over the whole catalog rather than as real
#: collections, so counting them as collection pages would report a duplicate of
#: everything else.
_VIRTUAL_COLLECTIONS = frozenset({"all", "frontpage"})

#: The catalog page types a crawl can be filtered to.
PAGE_TYPES: tuple[str, ...] = ("product", "collection")


def handle_ok(handle: object) -> bool:
    return isinstance(handle, str) and bool(_HANDLE_RE.match(handle))


def classify_catalog_url(url: str) -> str | None:
    """"product" | "collection" | None, from the path shape alone.

    Deliberately URL-only, which is what makes a page-type filter cheap: the
    crawl decides whether it wants a page BEFORE spending a request on it. It
    works on any store that uses the /products/ and /collections/ convention,
    not only Shopify.

    Moved here from `services/stores/discovery.py` (which re-imports it) when the
    site crawl needed the same rule. Two copies of this would mean the catalog
    crawl and the site crawl disagreeing about what a product page is.
    """
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) == 2 and segments[0] == "products" and handle_ok(segments[1]):
        return "product"
    if len(segments) == 2 and segments[0] == "collections" and handle_ok(segments[1]):
        if segments[1].lower() in _VIRTUAL_COLLECTIONS:
            return None
        return "collection"
    return None


def is_probably_asset(url: str) -> bool:
    """True when the path ends in a non-document extension."""
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    if dot == -1 or "/" in path[dot:]:
        return False
    return path[dot:] in ASSET_EXTENSIONS


@dataclass(slots=True)
class LinkScan:
    """What the <a href> set of one page says about the site graph."""

    #: Normalised, deduped, followable, same-site URLs, in document order, capped
    #: at MAX_LINKS_PER_PAGE.
    internal: list[str] = field(default_factory=list)
    #: Same-site links we will not follow because of rel="nofollow". Counted, not
    #: kept: they matter as an SEO finding (you nofollow your own pages), and a
    #: page reached only by nofollow links is genuinely orphaned for ranking.
    internal_nofollow: int = 0
    external: int = 0
    external_nofollow: int = 0
    #: Followable links dropped by MAX_LINKS_PER_PAGE, so a report can say "capped".
    dropped: int = 0

    @property
    def total(self) -> int:
        return len(self.internal) + self.internal_nofollow + self.external + self.external_nofollow


def extract_links(
    dom: HTMLParser,
    base_url: str,
    host: str,
    *,
    include_subdomains: bool = False,
    follow_nofollow: bool = False,
    max_links: int = MAX_LINKS_PER_PAGE,
) -> LinkScan:
    """Scan <a href> into internal/external counts plus the followable internal set.

    A <base href> is honoured when present: a template that sets one changes what
    every relative href on the page resolves to, and ignoring it would invent
    URLs that 404.
    """
    base_node = dom.css_first("base[href]")
    if base_node is not None:
        declared = (base_node.attributes.get("href") or "").strip()
        if declared:
            resolved = normalize_url(declared, base_url)
            if resolved:
                base_url = resolved

    scan = LinkScan()
    seen: set[str] = set()
    for node in dom.css("a[href]"):
        href = node.attributes.get("href")
        if not href:
            continue
        rel = (node.attributes.get("rel") or "").lower().split()
        nofollow = "nofollow" in rel and not follow_nofollow
        target = normalize_url(href, base_url)
        if target is None:
            continue
        if not same_site(target, host, include_subdomains=include_subdomains):
            if nofollow:
                scan.external_nofollow += 1
            else:
                scan.external += 1
            continue
        if nofollow:
            scan.internal_nofollow += 1
            continue
        if target in seen:
            continue
        seen.add(target)
        if len(scan.internal) >= max_links:
            scan.dropped += 1
            continue
        scan.internal.append(target)
    return scan
