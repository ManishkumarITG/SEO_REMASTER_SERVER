# The crawler

How the site-wide crawler behaves, what bounds it, and where each decision lives
in the code. Written against the code, not from memory: every number below is the
value in the source at the time of writing, and each one is named so you can grep
for it rather than trust this file.

---

## 1. Three crawl shapes, deliberately separate

The service layer contains three different things that all "crawl", and they are
not variants of each other. Knowing which one you are looking at prevents most
confusion.

| Shape | Entry point | What it walks | Frontier |
|---|---|---|---|
| Single page | `services/crawler/fetcher.py::fetch_page` | One URL the user typed | None |
| Store catalog | `services/stores/` | A Shopify catalog, from `products.json` or a sitemap | Known up front |
| **Site-wide** | `services/crawler/site_crawl.py::crawl_site` | A whole site, following links | **Grows as it runs** |

This document is about the third. The growing frontier is what makes it a
different problem: workers must block waiting for URLs that other workers have
not produced yet, and the crawl is finished only when the frontier is empty *and*
no worker is mid-page. That is `asyncio.Queue.join()`, which is why the site
crawl uses a queue with `task_done()` rather than the fixed worker-pool-plus-
sentinels pattern in `services/stores/enrichment.py`.

Everything below the crawl shape is shared on purpose, so a fix to politeness
lands everywhere at once:

```
services/crawler/
  site_crawl.py    the site-wide engine, the pre-flight probe, page facts
  urls.py          URL normalisation, link extraction, catalog classification
  models.py        CrawledPage, CrawlLimits, CrawlStats, SiteCrawlResult
  http_client.py   pooled client, capped reads, SSRF guard, robots fetch
  throttle.py      HostGate: per-host pacing, back-off, recovery
  robots.py        robots.txt parsing and wildcard path matching
  sitemap.py       <loc> extraction from sitemap documents
  parser.py        PageData and shared DOM lookups
  fetcher.py       the single-page fetcher the other two are built beside
```

---

## 2. The request lifecycle

```
POST /api/site-crawls
  │
  ├─ validate + normalise the URL            schemas/site_crawl.py
  ├─ reuse an in-flight crawl of this host?  services/site_crawl_service.py
  ├─ probe_site: is it reachable and allowed? (2 requests)
  │     unreachable      -> 422
  │     robots.txt Disallow: /  -> 403
  ├─ INSERT site_crawls row, COMMIT
  └─ schedule_site_crawl(row.id)             workers/tasks.py
        │
        └─ asyncio task, detached from this request
              ├─ probe again (idempotent; only the id crossed the boundary)
              ├─ crawl_site(...) yields CrawledPage objects
              │     each page: fetch -> parse -> facts -> links -> analyzers
              ├─ every 25 pages / 2s: flush counters + heartbeat to SQL,
              │                        bulk-write pages to MongoDB
              └─ on completion: mark orphans, write the final row

GET /api/site-crawls/{id}          poll status and progress
GET /api/site-crawls/{id}/issues   per-issue counts + orphan list
GET /api/site-crawls/{id}/pages    the page list, filtered and sorted
GET /api/site-crawls/{id}/page     one page, with inlinks and outlinks
POST /api/site-crawls/{id}/stop    ask it to finish now
```

**The task is not a FastAPI `BackgroundTask`.** Those run inside the response and
are cancelled when the client connection goes away, which silently truncated long
crawls. `schedule_site_crawl` uses `asyncio.create_task` and holds a strong
reference in `_running_site_crawls`, because asyncio only keeps a weak one and a
crawl could otherwise be garbage collected mid-run.

---

## 3. What bounds a crawl

Every exit is explicit and recorded in `CrawlStats`. Reaching one is a normal
outcome, never an exception: a partial crawl with an honest reason is worth more
than a raised error.

| Bound | Default | Setting / field | Recorded as |
|---|---|---|---|
| Pages fetched | 500 | `SITE_CRAWL_MAX_PAGES`, `CrawlLimits.max_pages` | `hit_page_budget` |
| Link depth from the seed | 5 | `SITE_CRAWL_MAX_DEPTH` | `beyond_depth` |
| Wall clock | 1800s, sized up to the site (below) | `SITE_CRAWL_DEADLINE_S`, `..._PER_TARGET_S`, `..._MAX_S` | `deadline_hit`, `deadline_s` |
| Navigation detour | 100 pages | `CrawlLimits.max_navigation_pages` | `hit_navigation_budget` |
| Bytes per page | 2 MiB | `MAX_PAGE_BYTES` | `truncated` on the page |
| A person pressing Stop | — | `POST .../stop` | `stopped_by_user` |

**`max_pages = 0` means no page limit.** Zero is the only special value; every
other number is a literal ceiling, and a negative is floored to zero rather than
becoming unlimited by accident. With no page limit the deadline and the depth
limit are the only bounds left, which is why neither can be switched off from the
API. `CrawlLimits.page_budget` returns `None` for the unlimited case so callers
cannot accidentally compare against `0`.

**The deadline is sized to the site.** A fixed 30 minutes, sized for a 500-page
crawl, cut an 8,000-page store off at 701 pages. Once the sitemap has been read and
the frontier size is known, the deadline becomes
`max(SITE_CRAWL_DEADLINE_S, expected pages x SITE_CRAWL_DEADLINE_PER_TARGET_S)`,
capped at `SITE_CRAWL_DEADLINE_MAX_S` (6 h). Expected pages is the frontier size,
or the page budget if that is smaller. The crawl adds a note ("time limit raised to
2h 13m for 8,001 pages") and the applied value is on the row as `deadline_s`.
`SITE_CRAWL_DEADLINE_PER_TARGET_S = 0` restores the fixed deadline.

Requests are additionally capped at the edge, in `schemas/site_crawl.py`:
`MAX_REQUESTABLE_PAGES = 25000`, `MAX_REQUESTABLE_DEPTH = 10`. They are rejected
with a 422 rather than clamped silently three layers in. The page ceiling bounds
how *long* a crawl runs, not how hard it hits the host: the rate limiter (section 5)
holds the request rate under what the host tolerates however many pages are asked
for.

**Rate limiting is NOT a bound.** See section 5.

---

## 4. Politeness

The crawl makes hundreds of unattended requests to a third party. Every control
here exists because ignoring it is how an IP ends up on a block list.

- **Identity.** `SEOAnalyzerBot/0.1 (+https://github.com/seo-remaster/bot)`. An
  honest bot UA with a contact URL, never a spoofed browser. Several stores
  throttle named SEO bots specifically; impersonating a browser to dodge that is
  a terms breach.
- **robots.txt** is fetched once per crawl and honoured, including wildcards and
  `$` anchors (`robots.py::path_allowed`, longest match wins, `Allow` beats
  `Disallow` on a tie). A group naming our own token beats `*`. A blanket
  `Disallow: /` is refused at the POST with a 403.
- **The matched path includes the query string** (`robots_target`). Shopify ships
  `Disallow: /*?*oseid=*`, and matching the bare path would allow every URL such
  a rule exists to exclude.
- **`Crawl-delay`** is used as the floor between requests, clamped to 2s so a
  hostile value cannot stall a job for hours.
- **A rate ceiling.** The site crawl's adaptive limiter (section 5) never sends
  faster than `1 / max(min_interval_s, Crawl-delay)`: 10 req/s at the default
  0.1s floor. It books request *starts* rather than wrapping whole requests, so the
  ceiling holds while `concurrency` workers stay in flight. The catalog crawl still
  uses the fixed `HostGate` with a 0.06s floor, because a `<head>` read is lighter
  than a whole page.
- **Bot challenges are a signal, never an obstacle.** A response that is a
  challenge interstitial ("Verifying your connection...", Cloudflare's
  `cf-mitigated: challenge`) makes the limiter back off hard. It is never solved,
  bypassed or retried around. See `services/crawler/outcomes.py`.
- **SSRF.** The seed host is checked with `is_public_host` (rejects loopback,
  private, link-local, reserved, multicast). Every URL entering the frontier must
  be same-site, so a document the target controls cannot aim the crawler at a new
  host. httpx still follows redirects anywhere, so the same-site rule is
  re-applied to the *final* URL before its links are followed.

---

## 5. Rate limiting: learn the host's rate, do not give up

A 429 means "too fast", not "go away". The page is retried, and the crawl learns
how fast this host actually lets it go.

### Why the old back-off was replaced

The site crawl used `HostGate.back_off()`, which multiplied the gap between
requests by 4 **per 429 response**. With 6 requests in flight, one rate-limit
burst arrives as five or six 429s, so the gap compounded 0.1 → 0.4 → 1.6 → 6.4 →
25.6 → 30s from a single event, then decayed at ×0.8 per page and needed ~150s to
recover. On a real crawl of go2marine.com (701 pages in 1,821s, stopped on the
deadline), ~1,500s was that recovery: the crawler spent about 85% of its time
waiting out its own over-reaction. Fetching itself ran at ~4.5 pages/s between
stalls.

### How it works now (`services/crawler/throttle.py`)

`LocalRateLimiter` wraps `AimdState`, a pure state machine (it is handed `now`,
never reads a clock) that controls a **rate** rather than an interval:

- **Episode coalescing.** Every request carries the epoch it was sent under. Only
  a 429 from the current epoch cuts the rate; the rest of the same burst are
  counted but ignored. Six simultaneous 429s are one signal.
- **AIMD.** A throttle cuts the rate ×0.6 (×0.36 for a bot challenge) and pauses
  the host for 4 send slots at the new rate (0.25-30s; ×2 then ×4 when episodes
  repeat within 60s; at least 60s for a challenge). Success raises the rate by
  +0.5 req/s per second of traffic while well below the *knee* (the rate that last
  drew a 429), and by 2% of the knee near it.
- **Lockouts are waited out, not cut into.** Measured on go2marine: after a 429
  the store refused *everything* for ~20s, whatever our rate. A 429 that arrives
  with no success since the last one is therefore the same lockout, not new
  evidence that we are too fast: the rate is left alone and the pause doubles
  instead. The old logic cut the rate on every refused retry and collapsed it
  1.46 → 0.88 → 0.53 req/s on a single lockout.
- **Lockout length is learned** from *send* times: it began when the first refused
  request was sent and ended between the last refused send and the first
  successful one (the midpoint is the estimate). Later episodes wait the whole
  lockout out in one pause, with a 10% margin, instead of spending retries
  rediscovering it.
- **Cruise.** After an episode the rate is held at `cruise_fraction x knee` for 60s,
  doubling up to 600s while the knee holds steady (moves by under 25%), then probes
  upward again. The band is 25%, not 10%: probing noise alone moves the knee 10-20%,
  and on a lockout store every unnecessary probe costs a 15-20s lockout. A knee the
  rate has sustained a full second above without a refusal rises with it, so a
  stale or pessimistic starting point does not pin growth.
- **Learned cruise fraction.** A host with a burst bucket lets us exceed its
  sustainable rate until the bucket runs dry, so the knee overstates what it can
  take. A 429 *during* a cruise lowers the fraction ×0.9 (floor 0.5); a clean
  cruise earns 0.02 back. A cut never lands above the cruise cap, so a 429 during
  cruise really does mean the cap was too fast.
- **Retry-After** is honoured in both forms. A date is measured against the
  response's own `Date` header, so local clock drift (WSL2, sleep) cannot distort
  it. It pauses the whole host, the furthest pause wins, capped at
  `MAX_RETRY_AFTER_S = 60`.
- **No sleeping inside a lock.** A worker books a slot, sleeps until it in slices of
  0.5s, then checks the booking is still valid (re-booking if a throttle landed
  meanwhile). Stop and the deadline interrupt a wait within one slice.

**Start rate.** A host never crawled before starts at `SITE_CRAWL_INITIAL_RATE_RPS
= 4.0` and grows ×1.25 per second until it first pushes back. A host crawled in
the last 30 days starts at the **learned rate** stored on its last crawl
(`learned_rate_rps`), so a re-crawl does not rediscover the limit by drawing 429s.

**Measured in simulation** (`tests/test_rate_limiter.py`, six workers, 30 min):

| Store | First crawl (slow start) | Re-crawl (learned rate) |
|---|---|---|
| locks out 10-20s after a 429, 1.0-2.0 req/s (go2marine-like) | 80-88% of its pace, 2.0-3.4% throttled | 84-91% of its pace, 0.8-1.1% throttled |
| plain token bucket, 1.5-6.0 req/s | 84-93%, 0.1-1.0% | 84-97%, 0.1-0.6% |

The old gate ran at 6.7% throttled on the real go2marine crawl. A first crawl of a
store pays a one-off discovery cost (roughly 40-50 refused requests in the first
three minutes on a lockout store) because the only way to find an unknown limit is
to reach it; the learned rate makes every later crawl of that store start informed.

**Measured on the real store** (go2marine, 2026-10-20, same settings as the old
crawl, 15-minute window):

| | Old gate | New limiter |
|---|---|---|
| Pages in 15 min | 350 | **815** |
| Throughput shape | bursts of ~70, then 2-3 min dead | steady ~54 pages every minute |
| Time stalled (gaps ≥10s) | 96% | **2%** |
| 429s, share of requests | 6.7% | **0.7%** (one per episode) |
| 4xx / 5xx / bot challenges | 0 | 0 |

Its sustainable rate is about **0.9 req/s**. The ~4.5 pages/s the old crawl showed
between stalls was burst capacity, and the store locks a client out for ~20s after
a 429. A full HTML crawl of a store like this is bounded by that rate, whatever the
hardware: 5,000 pages ≈ 1.5 h, 20,000 ≈ 6 h. The way to be faster on such a store
is to request fewer pages (the catalog JSON, incremental re-crawls), not to request
them faster.

The page itself is still retried up to `MAX_THROTTLE_RETRIES = 4` times; each retry
simply waits for its next slot from the limiter (there is no second sleep on top).
If a page still cannot be read, **that page** is recorded as unreachable
(`gave_up_throttled`) and the crawl continues. `503` is treated as throttling too.

Counters, all on the row and the API: `throttled` (429/503 responses),
`throttle_episodes` (distinct pushbacks, the honest measure of how often the host
objected), `throttle_pause_s`, `current_rate_rps`, `learned_rate_rps`,
`gave_up_throttled`. The live progress flush now writes the throttle counters, so
the "rate limiting us" banner appears while it is true, not only at the end.

---

## 6. URL rules

Dedupe decisions are what make a crawl terminate, so they are applied at
**enqueue** time, before `seen`, never at report time.

**Normalisation** (`urls.py::normalize_url`):

- Fragments dropped. `#anything` cannot change the response body.
- 33 known tracking parameters dropped (`utm_*`, `gclid`, `fbclid`, `msclkid`,
  `mc_cid`, …). Without this, a marketing site spends its whole page budget on
  one page wearing forty campaign tags.
- Remaining query parameters **sorted**, so `?a=1&b=2` and `?b=2&a=1` are one
  entry in `seen`.
- Scheme and host lower-cased; default ports stripped, non-default kept.
- Empty path becomes `/`.
- **Trailing slashes are preserved.** `/a` and `/a/` really can be different
  documents, so collapsing them would under-report.
- Non-http(s) schemes, malformed URLs and hosts containing invalid characters
  return `None`, which callers treat as "ignore", never as an error.

`normalize_seed_url` is the separate variant for a URL a *person typed*: it
assumes `https` when there is no scheme at all. It is deliberately not used for
link extraction, which must never invent a scheme for an `href`.

**Not fetched at all:** 51 asset extensions (images, video, archives, fonts,
scripts, documents). The content-type check would catch these anyway, but a 40 MB
video is a wasted request even when the body is discarded.

**Link extraction** honours `<base href>`, splits internal from external, counts
`rel="nofollow"` separately, and caps kept links at `MAX_LINKS_PER_PAGE = 300` —
a nav-heavy template lists the same header and footer on every page, and the tail
has no graph value.

---

## 7. Page types: products, collections, or both

`CrawlLimits.page_types` restricts the crawl to catalog page types. Empty means
every page, which is the default and the original behaviour.

Classification is **from the URL path alone**
(`urls.py::classify_catalog_url`), which is what makes the filter cheap: the
crawl decides whether it wants a page *before* spending a request on it.
`/products/<handle>` is a product, `/collections/<handle>` is a collection, and
Shopify's virtual `/collections/all` and `/collections/frontpage` are neither.
The rule works on any store using that convention, not only Shopify.

A filtered crawl still has to *reach* the pages it wants — a store's home page
and collection listings are the only route to its product pages. So:

- A URL matching the filter is a **target**: fetched, analyzed, stored, and
  counted against `max_pages`.
- A URL outside the filter is **navigation**: fetched for its links only, not
  analyzed, and charged to a separate `max_navigation_pages` budget so it never
  eats the page budget.
- Running out of navigation budget ends the *detour*, not the crawl.

`CrawledPage.is_target` records which one a page was. Reads exclude navigation
pages by default (`include_navigation=true` opts in), and the filter uses
`{"is_target": {"$ne": False}}` so documents written before the field existed
still count as targets, which they were.

A skipped URL is classified too. A robots-blocked *product* page is still part of
a products-only report, because we asked for it and could not have it; a
robots-blocked account page is not.

---

## 8. Seeding from the sitemap

On by default (`CrawlLimits.seed_from_sitemap`). `/sitemap.xml` plus every
`Sitemap:` line in robots.txt, following nested indexes one level, at most
`MAX_SITEMAP_FETCHES = 50` documents (index files and failed fetches count). It was
10, and a large Shopify store, which splits its catalog across many child sitemaps,
stopped seeding at 8,001 URLs.

This is what makes **orphan detection** possible: a page listed in the sitemap
that no crawled page links to has no internal path to it.

Two details worth knowing:

- Sitemap XML is parsed with a **bounded regex over `<loc>`**, never an XML
  parser. The document comes from a user-supplied host, and while XXE is not
  reachable on CPython 3.12, entity-expansion DoS is. We need exactly one thing,
  the text of `<loc>`, so this is token extraction from an untrusted document
  rather than parsing.
- `looks_like_sitemap` checks the body, because some hosts answer `/sitemap.xml`
  with HTTP 200 and their themed not-found page. python.org does exactly this.

**Seed cap.** Twice the page budget, floored at 100 — or `MAX_SITEMAP_SEEDS =
50000` when there is no page budget to derive it from. The note *"sitemap seeding
stopped at N URLs"* means that cap was reached; it is information, not an error.

---

## 8a. Catalog-first on Shopify stores

A store with 10,000 products is 10,000 page fetches, and a Shopify storefront
sustains roughly one request a second (section 5), so the HTML crawl of that store
takes about three hours. Its public `/products.json` returns 250 products per
request, so the same catalog as JSON is ~40 requests. When the probe sees
Shopify's markers on the homepage (`SiteProbe.is_shopify`, read from the probe's
own GET, so it costs no request), the worker reads the whole catalog **before**
fetching any page (`workers/tasks.py::_run_catalog_phase`,
`services/site_crawl_catalog.py`).

What it produces, within about a minute:

- **Findings about the catalog's content**: `description_missing`,
  `image_missing`, `image_alt_missing`, `collection_empty`, and
  `duplicate_name` (two products whose names differ only in case or spacing).
  The definitions are the store crawl's own (`services/stores/seo_gaps.py`), so
  the two features cannot disagree about what a gap means.
- **One document per item** in MongoDB `site_crawl_catalog`, with the item's
  `updated_at`: what an incremental re-crawl will compare against.
- **Seeds**: every product and collection URL goes into the page crawl's
  frontier through `extra_seeds`, counted in `seeded_from_catalog`.

What JSON cannot say: meta title, meta description and canonical live in the
rendered `<head>`, so they still come from the page crawl, which runs straight
after. The two are complementary.

Rules it keeps:

- **One paced client.** The catalog read uses the same `LocalRateLimiter` the
  page crawl then uses (`limiter_for` builds it before either phase). The store
  sees one client, and the page crawl starts from what the catalog read learned.
- **A 429 is asked again, not a silent end.** The paced walk
  (`services/stores/discovery.py::_walk` with a `pacer`) retries a 429/503 or a
  transport error up to `MAX_JSON_THROTTLE_RETRIES = 4` times. The unpaced walk
  the store crawl uses is unchanged.
- **Honest completeness.** `complete` means both endpoints were read to a short
  or empty page. A cap, a refusal, a Stop or the time limit leaves it `partial`
  with a note, so "we read 4,000 products" is never reported as "the store has
  4,000".
- **robots.txt per endpoint.** A store that disallows `/products.json` gets no
  JSON read; its products come from the sitemap.
- **A 401/403 is a wall.** Reported as `blocked`, fed to the limiter as severe,
  not retried, and the next endpoint is not tried.
- **It never fails the crawl.** Any exception leaves `catalog_status =
  unavailable` and a note, and the crawl continues from the sitemap.

| Setting | Default | Meaning |
|---|---|---|
| `site_crawl_catalog_first` | `True` | run the phase on Shopify stores |
| `site_crawl_catalog_max_products` | 50,000 | item cap for `/products.json` |
| `site_crawl_catalog_max_collections` | 5,000 | item cap for `/collections.json` |
| `site_crawl_catalog_timeout_s` | 600 | the phase stops here and the page crawl starts |

Row columns: `catalog_status` (`reading`, `complete`, `partial`,
`unavailable`, or null when the phase did not run), `catalog_products`,
`catalog_collections`, `catalog_requests`, `catalog_elapsed_s`, `catalog_gaps`
(gap -> count). Items: `GET /api/site-crawls/{id}/catalog?kind=&gap=&offset=&limit=`,
most gaps first. The Site crawl page shows the panel while the crawl is still
running.

---

## 9. Page analysis

Analyzers run **inside** the crawl, through the `page_hook` seam, because the DOM
exists only while a page is being reduced to facts. Scoring afterwards would mean
fetching the whole site twice.

Only analyzers that read the `PageData` they are handed are safe in bulk:

| Analyzer | Bulk-safe | Why |
|---|---|---|
| `on_page` | yes | titles, descriptions, headings, images, links |
| `technical` | yes | robots, canonical, viewport, status, timing |
| `pagespeed` | no | a Lighthouse run per page |
| `content_ai` | no | an LLM completion per page |
| `search_console` | no | a Google API query per page |

`services/site_crawl_analysis.py::BULK_SAFE` enforces this: a name configured
outside the set is refused with a log line rather than quietly running up an API
bill. Configure with `SITE_CRAWL_ANALYZERS`; an empty value turns scoring off,
which is different from every page scoring zero (`avg_score` is then `null`).

**Parsing cost.** All of this runs on the API's event loop, so it is CPU every
other worker waits behind. Each page is parsed twice: once into the DOM, once into
a scratch tree for its text (`parser.extract_texts`, which yields both the visible
text and the chrome-stripped main text from one tree). It used to be parsed four
times with three full re-serializations, because `visible_text` and
`extract_main_text` each serialized the DOM back to a string and re-parsed it, and
`on_page` did it again to count words. Measured on a real 744 KB Shopify page:
99.9 ms → 37.6 ms per page (62% less), identical facts. `PageData.visible_text`
carries the text to the analyzer so it does not redo it.

Each page gets a weighted `score`, a `grade`, per-analyzer scores, and up to
`MAX_FINDINGS_PER_PAGE = 25` non-passed findings, most urgent first. Passed
checks are not stored — the score already carries that news. Navigation pages are
never scored.

---

## 10. What gets stored

**SQL (`site_crawls`)** holds job state, so the client can always poll it. Every
counter ships in one row: progress, skip reasons per category, why it stopped,
the headline findings, and the analysis roll-up.

> Columns added after the table first existed are **nullable** and read as
> `row.x or 0`. `db/session.py::_add_missing_columns` can only `ALTER TABLE ADD` a
> column that is nullable or carries a server default, and
> `Integer, nullable=False, default=0` is a Python-side default that does not
> qualify. This has bitten twice; check it before adding a counter.

**MongoDB (`site_crawl_pages`)** holds one document per `(crawl_id, url)`. Keyed
by crawl rather than by host on purpose: a site crawl is a snapshot, and the value
of keeping two is comparing them. Writes are best-effort — a failure is logged and
surfaced as `persistence_ok`, never raised to the client. The crawl still ran and
its counters are in SQL.

HTML is **never** retained. `services/stores/enrichment.py` learned that the
expensive way: 5,000 pages x 90 KiB of markup is 450 MB. A page is reduced to its
facts the moment it is parsed and the markup is dropped.

Reads support 17 issue filters (`broken`, `orphan`, `missing_title`, `thin`,
`critical`, `product`, …) and 6 sort orders (`depth`, `url`, `status`, `words`,
`links`, `score`). Both are defined once in `db/site_pages.py` so the route, the
counters and the export cannot drift apart.

---

## 11. Orphan detection

Computed once at the end, because a page crawled first may be linked from a page
crawled last.

The worker keeps a set of every outlink seen and a list of every page fetched;
the orphans are the difference, minus the seed. Memory is two sets of unique
URLs, not a list of edges — the edge count is pages x links, the URL count is the
site. The result is written back with `mark_orphans`, chunked at 500 per query
because `$in` with ten thousand URLs is a query document megabytes wide.

The seed is never an orphan. A skipped URL is never an orphan either: it was
never fetched, so "nothing links to it" says nothing about it.

---

## 12. Statuses, stopping, and interruption

| Status | Meaning |
|---|---|
| `pending` | row created, worker has not started |
| `crawling` | running |
| `complete` | reached the end of the site inside every bound |
| `partial` | a bound was reached, or some pages failed — an honest answer, not a failure |
| `stopped` | a person pressed Stop |
| `failed` | nothing was fetched, or the crawl could not start |

**Stop is cooperative.** `POST .../stop` sets a flag that `crawl_site` checks
before each page *and* inside every wait for a slot from the rate limiter; the crawl
then exits through its normal completion path. Cancelling the task instead would
lose pages already fetched but not yet written. A request already on the wire still
finishes, but a page waiting out a throttle pause is released within 0.5s, so a
stop no longer waits out a back-off. The deadline interrupts waits the same way.
Running out of page budget does **not** interrupt a waiting page: one that reserved
its slot before the budget ran out is part of the budget.

**Heartbeats.** The worker writes `heartbeat_at` on every progress flush **and on
a 10s timer** (`SITE_HEARTBEAT_EVERY_S`) for as long as the crawl runs, including
the Lighthouse template phase. Progress used to be flushed only when a page
arrived, so a crawl waiting out a throttle pause or measuring templates for
minutes looked dead to any other process reading `heartbeat_at`.
Startup reconciliation only fails rows whose heartbeat has been silent for
`SITE_CRAWL_STALE_AFTER_S = 180`. It used to fail *every* non-terminal row, which
meant a second process starting — even one that immediately died on a port clash —
marked healthy running crawls as failed.

**Reaping on read.** Startup reconciliation alone is not enough: a crawl killed
mid-run leaves a row saying `crawling` that never changes, and the client polls it
forever. `_reap_if_abandoned` closes out a silent row the moment anyone reads it.
A crawl running in another process is still writing a heartbeat, so it is never
reaped by this one.

### Operational note: `uvicorn --reload`

A reload restarts the process, which kills any crawl in flight. Nothing in the
application can survive that. **Run the backend without `--reload` for real
crawls** (`python main.py`). Under `--reload`, saving any file ends a running
crawl and the row is closed out as interrupted.

Crawls are **not resumable** across a restart. Pages already written to MongoDB
survive and stay readable, but the crawl itself does not continue.

---

## 13. Concurrency and resource budget

`MAX_CONCURRENT_SITE_CRAWLS = 1`. One, not two: a site crawl fetches whole pages
(up to 2 MiB each) where the catalog crawl reads only `<head>`s, and parsing them
runs on the API's own event loop. Each crawl opens its **own** connection pool
(sized by `http_client.DEFAULT_LIMITS`, 24 connections); pools are not shared with
catalog crawls or audits, so this limit is about event-loop CPU, not connections.

The SQL progress row is updated every 25 pages or 2 seconds, whichever comes first,
plus the 10s heartbeat. MongoDB is written every **25 pages** only (the 2s timer
applies to SQL, not Mongo).

---

## 14. Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_site_crawl.py` | 64 | the engine: traversal, bounds, robots, throttling, page types, stop |
| `tests/test_rate_limiter.py` | 42 | the adaptive limiter on a fake clock, plus a simulation against a token-bucket host |
| `tests/test_crawler_urls.py` | 19 | normalisation and link extraction, no I/O |
| `tests/test_site_crawl_worker.py` | 30 | status transitions, orphans, scheduling, heartbeats, reconciliation, the catalog phase |
| `tests/test_site_crawl_routes.py` | 61 | HTTP contract, validation, reaping, stop, the catalog endpoint |
| `tests/test_site_crawl_catalog.py` | 10 | the paced JSON walk (429 retry, refusal, robots, caps, Stop), catalog findings and records |
| `tests/test_site_crawl_integration.py` | 15 | POST through worker to storage, only HTTP and Mongo stubbed |

The integration tests stub only the network (respx) and MongoDB (a dict). They
drive `run_site_crawl` directly rather than through the scheduler, because the
detached task lands on TestClient's own event loop, which stops when the response
is sent.

---

## 15. Things to know before changing this

- **Bounds are not advisory.** If you add a loop that can fetch, give it a
  ceiling and a counter, and make reaching it a reported outcome rather than an
  exception.
- **A new counter column must be nullable.** See section 10.
- **Do not import upward.** `services/crawler/` must not import from
  `services/stores/`; shared helpers moved down into the crawler layer for
  exactly this reason (`throttle.py`, `sitemap.py`, the URL classifier).
  `config/settings.py` must not import from `services/` either, which is why
  `CrawlLimits.from_settings` reads settings with `getattr` from the crawler side.
- **The analyzer seam is a hook, not an import.** `site_crawl.py` stays ignorant
  of `services.analyzers`, which itself imports `services.crawler.parser`.
- **One page must never take down the crawl.** Every per-page failure path ends
  in a `CrawledPage` with `error` or `skipped` set.
- **Every request goes through the limiter and reports back.** A new fetch path
  must `acquire` a ticket and `report` the response, or the limiter learns nothing
  from it and the host sees an unpaced request. Keep `AimdState` free of I/O and
  clocks so the simulation tests stay deterministic.
- **Challenge detection must stay narrow.** Cloudflare injects
  `/cdn-cgi/challenge-platform/` scripts into *ordinary* pages on some sites;
  matching that path alone would flag every page as a challenge and grind the crawl
  to its slowest rate. Only markers that exist on the interstitial itself count.
- **The catalog phase is a head start, never a requirement.** It must not raise
  out of `_run_catalog_phase`, and every catalog request goes through the crawl's
  limiter. A partial read must say it is partial.
