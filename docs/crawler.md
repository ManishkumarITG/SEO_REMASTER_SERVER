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
| Wall clock | 1800s | `SITE_CRAWL_DEADLINE_S` | `deadline_hit` |
| Navigation detour | 100 pages | `CrawlLimits.max_navigation_pages` | `hit_navigation_budget` |
| Bytes per page | 2 MiB | `MAX_PAGE_BYTES` | `truncated` on the page |
| A person pressing Stop | — | `POST .../stop` | `stopped_by_user` |

**`max_pages = 0` means no page limit.** Zero is the only special value; every
other number is a literal ceiling, and a negative is floored to zero rather than
becoming unlimited by accident. With no page limit the deadline and the depth
limit are the only bounds left, which is why neither can be switched off from the
API. `CrawlLimits.page_budget` returns `None` for the unlimited case so callers
cannot accidentally compare against `0`.

Requests are additionally capped at the edge, in `schemas/site_crawl.py`:
`MAX_REQUESTABLE_PAGES = 5000`, `MAX_REQUESTABLE_DEPTH = 10`. Those numbers
decide how much traffic we send to somebody else, so they are rejected with a 422
rather than clamped silently three layers in.

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
- **`HostGate`** serialises request *starts* rather than whole requests, so the
  rate ceiling holds while `concurrency` workers stay in flight. Default floor
  for a site crawl is 0.1s; the catalog crawl uses 0.06s because a `<head>` read
  is lighter than a whole page.
- **SSRF.** The seed host is checked with `is_public_host` (rejects loopback,
  private, link-local, reserved, multicast). Every URL entering the frontier must
  be same-site, so a document the target controls cannot aim the crawler at a new
  host. httpx still follows redirects anywhere, so the same-site rule is
  re-applied to the *final* URL before its links are followed.

---

## 5. Rate limiting: wait, do not give up

A 429 means "too fast", not "go away". This was the single biggest source of
truncated crawls and the handling is deliberately patient.

1. The page is **retried**, up to `MAX_THROTTLE_RETRIES = 4` attempts.
2. `Retry-After` is honoured when the host sends a numeric one, capped at
   `MAX_RETRY_AFTER_S = 60`. A date-formatted header is ignored rather than
   guessed at.
3. `HostGate.back_off()` quadruples the interval between requests, up to
   `THROTTLED_MAX_INTERVAL_S = 30s` for a site crawl. The catalog crawl keeps its
   own 2s ceiling: the ceiling is per gate, not global.
4. `HostGate.recover()` eases the interval back toward the floor (factor 0.8) on
   every response the host was happy with, so one 429 early in a long crawl does
   not slow every later page.
5. If a page still cannot be read, **that page** is recorded as unreachable
   (`gave_up_throttled`) and the crawl continues.

`503` is treated as throttling too, not as a server error, because that is what
it usually means in practice from a rate limiter.

Counters: `stats.throttled` (responses received) and `stats.gave_up_throttled`
(pages finally abandoned). Both reach the API, so a report can say "we got 280 of
your 300 pages, the rest were rate-limited".

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
`MAX_SITEMAP_FETCHES = 10` documents.

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
25000` when there is no page budget to derive it from. The note *"sitemap seeding
stopped at N URLs"* means that cap was reached; it is information, not an error.

---

## 9. Page analysis

Analyzers run **inside** the crawl, through the `page_hook` seam, because the DOM
exists only while a page is being reduced to facts. Scoring afterwards would mean
fetching the whole site twice.

Only analyzers that read the `PageData` they are handed are safe in bulk:

| Analyzer | Bulk-safe | Why |
|---|---|---|
| `on_page` | yes | titles, descriptions, headings, images, links — ~1 ms |
| `technical` | yes | robots, canonical, viewport, status, timing — ~1 ms |
| `pagespeed` | no | a Lighthouse run per page |
| `content_ai` | no | an LLM completion per page |
| `search_console` | no | a Google API query per page |

`services/site_crawl_analysis.py::BULK_SAFE` enforces this: a name configured
outside the set is refused with a log line rather than quietly running up an API
bill. Configure with `SITE_CRAWL_ANALYZERS`; an empty value turns scoring off,
which is different from every page scoring zero (`avg_score` is then `null`).

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
between pages; the crawl then exits through its normal completion path. Cancelling
the task instead would lose pages already fetched but not yet written. A page
already in flight still finishes, so a stop lands within a page or two rather than
instantly.

**Heartbeats.** The worker writes `heartbeat_at` on every progress flush.
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
(up to 2 MiB each) where the catalog crawl reads only `<head>`s, and it shares
`http_client.DEFAULT_LIMITS` (24 connections) with 2 catalog crawls and 4 audits.
One crawl at 6 workers is what is left without starving the endpoints a user is
watching.

Progress flushes every 25 pages or 2 seconds, whichever comes first. The same
boundary batches the MongoDB bulk write, so a 1,000-page crawl does about 40
`UPDATE`s against SQLite's single writer instead of 1,000.

---

## 14. Tests

| File | Tests | Covers |
|---|---|---|
| `tests/test_site_crawl.py` | 64 | the engine: traversal, bounds, robots, throttling, page types, stop |
| `tests/test_crawler_urls.py` | 19 | normalisation and link extraction, no I/O |
| `tests/test_site_crawl_worker.py` | 26 | status transitions, orphans, scheduling, heartbeats, reconciliation |
| `tests/test_site_crawl_routes.py` | 31 | HTTP contract, validation, reaping, stop |
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
