"""Domain models for the site-wide crawl.

`CrawledPage` is pydantic because it is the unit that gets persisted and returned
over the wire (`model_dump(mode="json")`). `CrawlLimits` and `CrawlStats` are
plain dataclasses - internal knobs and counters, never serialised on their own.

The deliberate omission is HTML. `services/stores/enrichment.py` learned this the
expensive way: 5,000 pages x 90 KiB of retained markup is 450 MB. A page is
reduced to its facts the moment it is parsed, and the markup is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

#: Why this URL was not (fully) used, or "" when it was. Only per-URL reasons
#: live here. Whole-crawl outcomes - the page budget, the deadline, a 429 abort,
#: the depth limit - are counters on `CrawlStats`, because they are facts about
#: the crawl rather than about any one URL, and emitting one record per skipped
#: URL would bury the report on a large site.
#:
#: `off_site` is the odd one: the URL WAS fetched (its status is recorded), but
#: the response landed on another host, so its links were not followed.
#:
#: `page_type` means a page-type filter was active and this URL was neither one
#: of the wanted types nor worth fetching to reach one.
SkipReason = Literal["", "robots", "asset", "non_html", "off_site", "page_type"]


class CrawledPage(BaseModel):
    """One URL the crawl resolved, successfully or not."""

    url: str
    depth: int = 0
    #: The page whose <a href> put this URL in the frontier. Empty for a seed.
    #: This is what lets a report answer "how do I reach this page?".
    discovered_from: str = ""

    final_url: str = ""
    status_code: int | None = None
    content_type: str = ""
    redirect_chain: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    #: True when the body exceeded the page byte cap. Every count below is then
    #: a floor, not a total, and a report can say so instead of quietly lying.
    truncated: bool = False

    # --- head facts ---------------------------------------------------------------
    title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    lang: str = ""
    #: Raw `<meta name="robots">` content, kept verbatim for the report; the two
    #: booleans below are the parsed form the analyzers branch on.
    robots_meta: str = ""
    noindex: bool = False
    nofollow: bool = False

    # --- body facts ---------------------------------------------------------------
    #: Capped: a broken template can emit hundreds of H1s and the finding is
    #: "more than one", which the first few prove.
    h1: list[str] = Field(default_factory=list)
    h1_count: int = 0
    h2_count: int = 0
    word_count: int = 0
    images: int = 0
    images_missing_alt: int = 0
    #: The full H1-H6 outline in document order: [{"level": 2, "text": "..."}].
    #: The counts above say a page HAS headings; only the sequence shows that it
    #: jumps H1 -> H3 and breaks the outline.
    headings: list[dict[str, Any]] = Field(default_factory=list)
    #: Headings found, which can exceed len(headings) when the page is absurd.
    heading_count: int = 0
    #: Transitions that skipped a level going down. 0 is a well-formed outline.
    heading_skips: int = 0
    #: Main body text, page chrome stripped, capped by `CrawlLimits.text_chars`.
    #: The markup is never kept, so this is the only record of what the page said.
    text_content: str = ""

    # --- graph --------------------------------------------------------------------
    internal_links: int = 0
    internal_nofollow_links: int = 0
    external_links: int = 0
    #: Followable same-site targets, deduped and capped. This IS the site graph:
    #: a URL that appears in no page outlinks and came from the sitemap is orphaned.
    outlinks: list[str] = Field(default_factory=list)
    #: Followable links discarded by the per-page cap.
    outlinks_dropped: int = 0

    # --- analysis -------------------------------------------------------------------
    # Filled in only when the crawl was given an analyzer hook. The DOM is gone by
    # the time a page is reported, so analysis happens WHILE the page is parsed or
    # not at all - re-fetching 500 pages to score them would double the crawl.
    #: Weighted 0-100 across the analyzers that ran; None when none did.
    score: float | None = None
    grade: str | None = None
    #: Per-analyzer score, e.g. {"on_page": 84.0, "technical": 100.0}.
    analyzer_scores: dict[str, float | None] = Field(default_factory=dict)
    critical_count: int = 0
    warning_count: int = 0
    #: Non-passed findings only, most urgent first, capped. A passed check is not
    #: worth 500 copies in the database; the score already carries that news.
    findings: list[dict[str, Any]] = Field(default_factory=list)

    # --- catalog classification ---------------------------------------------------
    #: "product" | "collection" | "" (anything else), from the URL path alone.
    page_type: str = ""
    #: Which template rendered the page - "product", "collection", "home", ... See
    #: services/crawler/templates.py. Anything whose cost is per-template rather
    #: than per-page groups on this.
    template: str = ""
    #: False for a page fetched only to find links, because a page-type filter was
    #: active and this page was not one of the wanted types. Such a page is real
    #: and was really fetched - it is just not part of the answer, so it is
    #: excluded from the scored set and from the default page list.
    is_target: bool = True

    # --- outcome ------------------------------------------------------------------
    skipped: SkipReason = ""
    error: str = ""

    @property
    def fetched(self) -> bool:
        return not self.skipped and not self.error and self.status_code is not None

    @property
    def ok(self) -> bool:
        return self.fetched and 200 <= (self.status_code or 0) < 300

    @property
    def indexable(self) -> bool:
        """A 2xx page that does not tell search engines to ignore it."""
        return self.ok and not self.noindex

    @property
    def analyzed(self) -> bool:
        return self.score is not None


@dataclass(slots=True)
class CrawlLimits:
    """Every bound on one crawl, in one object, so a caller cannot set half of them.

    Defaults are deliberately modest: this runs unattended against a third party,
    and an unbounded default is how a tool ends up on a block list. The service
    layer reads the real values from `config.settings`.
    """

    #: Pages FETCHED, not URLs discovered. Reaching it is a normal outcome
    #: (`stats.hit_page_budget`), not an error.
    #:
    #: ZERO MEANS NO PAGE LIMIT. The crawl is then bounded by `deadline_s` and
    #: `max_depth` instead, which are real bounds and not advisory: at the default
    #: pacing an unlimited crawl still cannot outlive its deadline. Zero is the
    #: only special value; every other number is a literal ceiling.
    max_pages: int = 500
    #: Link distance from the seed. 0 crawls the seed alone.
    max_depth: int = 5
    concurrency: int = 6
    #: Wall clock for the whole crawl. A slow host must not hold a worker slot
    #: indefinitely, so this is enforced, not advisory.
    deadline_s: float = 10 * 60
    #: Floor between request starts, before robots Crawl-delay raises it. Higher
    #: than the catalog crawl floor: whole pages are heavier than <head>s.
    min_interval_s: float = 0.1
    include_subdomains: bool = False
    #: Following rel="nofollow" would ignore an explicit instruction from the
    #: site. Off by default; on, it is how you find pages nothing else reaches.
    follow_nofollow: bool = False
    #: Seed the frontier from robots Sitemap: entries and /sitemap.xml as well as
    #: the start URL. This is what makes orphan detection possible: a page in the
    #: sitemap that no crawled page links to has no internal path to it.
    seed_from_sitemap: bool = True
    max_links_per_page: int = 300
    #: Characters of main body text stored per page, chrome stripped. 0 stores
    #: none. 500 pages x 8,000 chars is about 4 MB, which is affordable; the raw
    #: MARKUP still is not, and is still never kept.
    text_chars: int = 8000
    #: Restrict the crawl to catalog page types: ("product",), ("collection",) or
    #: both. Empty means every page, which is the default and the old behaviour.
    #:
    #: The classification is made from the URL, so a page outside the filter costs
    #: nothing unless it is needed to FIND one that is inside it - see
    #: `max_navigation_pages`.
    page_types: tuple[str, ...] = ()
    #: Ceiling on pages fetched purely to discover links while a page-type filter
    #: is active. A store's home page and collection listings are the map to its
    #: product pages, so refusing to fetch them would end the crawl at the seed -
    #: but they are not the answer either, so they get their own small budget and
    #: never eat into `max_pages`. Ignored when no filter is set.
    max_navigation_pages: int = 100

    def __post_init__(self) -> None:
        # 0 survives as "no limit"; anything else is floored at 1 so a negative
        # cannot quietly become unlimited.
        self.max_pages = max(0, int(self.max_pages))
        self.text_chars = max(0, int(self.text_chars))
        self.max_depth = max(0, int(self.max_depth))
        self.concurrency = max(1, int(self.concurrency))
        self.max_navigation_pages = max(0, int(self.max_navigation_pages))
        self.page_types = tuple(dict.fromkeys(self.page_types))  # dedupe, keep order

    @property
    def page_budget(self) -> int | None:
        """The page ceiling, or None when the crawl was asked for every page."""
        return self.max_pages or None

    def wants(self, page_type: str | None) -> bool:
        """True when a page of this type is part of the answer.

        No filter means every page is. With a filter, only the named types are -
        everything else is navigation.
        """
        return not self.page_types or (page_type or "") in self.page_types

    @classmethod
    def from_settings(cls, settings: object, **overrides: object) -> CrawlLimits:
        """Build from the SITE_CRAWL_* settings, with per-call overrides.

        Typed as `object` and read with getattr on purpose: `config.settings` is
        the layer every other module depends on, so it must not import this one.
        Putting the adapter on this side keeps that edge one-way. Unknown or
        missing settings fall back to the field defaults above.
        """
        mapped = {
            field_name: getattr(settings, f"site_crawl_{field_name}")
            for field_name in ("max_pages", "max_depth", "concurrency", "min_interval_s",
                               "include_subdomains", "text_chars")
            if hasattr(settings, f"site_crawl_{field_name}")
        }
        if hasattr(settings, "site_crawl_deadline_s"):
            mapped["deadline_s"] = float(settings.site_crawl_deadline_s)  # type: ignore[attr-defined]
        mapped.update(overrides)
        return cls(**mapped)  # type: ignore[arg-type]


@dataclass(slots=True)
class CrawlStats:
    """Counters for one crawl. Every skip has its own bucket, because "we crawled
    40 of your 900 pages" is only actionable once you know which wall was hit."""

    discovered: int = 0
    fetched: int = 0
    ok: int = 0
    redirected: int = 0
    client_errors: int = 0
    server_errors: int = 0
    transport_errors: int = 0
    retries: int = 0
    skipped_by_robots: int = 0
    skipped_asset: int = 0
    skipped_non_html: int = 0
    skipped_off_site: int = 0
    #: Discovered inside the site but past `max_depth`. Counted, never fetched:
    #: this is the number that tells a user their crawl depth was the binding
    #: limit rather than their page budget.
    beyond_depth: int = 0
    #: Pulled from the frontier after the crawl had already stopped (budget,
    #: deadline or abort), so it was drained rather than fetched.
    not_attempted: int = 0
    truncated_pages: int = 0
    max_depth_seen: int = 0
    frontier_peak: int = 0
    seeded_from_sitemap: int = 0
    #: Pages matching the page-type filter - the ones the crawl was asked for.
    targets: int = 0
    #: Pages fetched only to find links, under a page-type filter. Reported so a
    #: user can see what a filtered crawl cost beyond the pages it returned.
    navigation_pages: int = 0
    #: URLs skipped outright because their type was outside the filter.
    skipped_page_type: int = 0
    hit_navigation_budget: bool = False
    #: Responses that told us to slow down, and pages finally abandoned because
    #: the host never stopped saying it. Reported so "we got 280 of your 300
    #: pages, the rest were rate-limited" is a sentence the UI can say.
    throttled: int = 0
    gave_up_throttled: int = 0
    #: A person pressed Stop. Distinguished from every other early exit because
    #: it is the only one that is not a limitation to report or work around.
    stopped_by_user: bool = False
    #: Pages an analyzer actually scored, and the running total of their scores.
    #: Summed rather than averaged as we go, so the mean is exact at the end
    #: instead of drifting through repeated division.
    analyzed: int = 0
    score_total: float = 0.0
    critical_findings: int = 0
    warning_findings: int = 0
    hit_page_budget: bool = False
    deadline_hit: bool = False
    aborted_reason: str = ""
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def average_score(self) -> float | None:
        """Mean analyzer score across the pages that were scored, or None."""
        return round(self.score_total / self.analyzed, 1) if self.analyzed else None

    @property
    def pages_per_second(self) -> float:
        return round(self.fetched / self.elapsed_s, 2) if self.elapsed_s > 0 else 0.0

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


@dataclass(slots=True)
class SiteCrawlResult:
    """The whole crawl, for callers that want it in memory rather than streamed."""

    start_url: str
    origin: str
    host: str
    pages: list[CrawledPage] = field(default_factory=list)
    stats: CrawlStats = field(default_factory=CrawlStats)

    @property
    def indexable_pages(self) -> list[CrawledPage]:
        return [p for p in self.pages if p.indexable]

    def orphans(self) -> list[str]:
        """Fetched URLs that no other crawled page links to, excluding the seed.

        A page only the sitemap knows about is invisible to a visitor navigating
        the site, and link equity never reaches it. This is the single most
        actionable thing a site-wide crawl produces that a single-page audit cannot.
        """
        linked: set[str] = set()
        for page in self.pages:
            linked.update(page.outlinks)
        return [
            page.url
            for page in self.pages
            if page.fetched and page.url != self.start_url and page.url not in linked
        ]
