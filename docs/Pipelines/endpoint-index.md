# Endpoint index — poora API surface

Saare endpoints, router ke hisaab se grouped. `§` column us pipeline page par le jaata
hai jahan poora contract hai.

Base URL dev mein: `http://localhost:8000`.
Har request **session cookie** ke saath jaani chahiye (`credentials: "include"`).

---

## Health

| Method | Path | Maqsad | Status codes |
|---|---|---|---|
| GET | `/api/health` | Liveness + dependency status | **Hamesha 200** |

Response: `{status: "ok", version: "0.1.0", mongodb: "connected"|"unavailable",
reddit: "configured"|"unconfigured", gemini: "configured"|"unconfigured"}`

`mongodb` ek **live ping** hai (`admin.command("ping")`,
`serverSelectionTimeoutMS = connectTimeoutMS = MONGODB_TIMEOUT_MS = 1500`), isliye ek
unreachable Mongo ~1.5 s cost karta hai. `reddit`/`gemini` sirf key presence batate hain.

> Ye SQL, Redis/cache, PageSpeed, Anthropic, Google OAuth config, Ollama ya Claude CLI
> **check nahi karta**.

---

## Audits — `/api/audits` → [audit-pipeline.md](audit-pipeline.md)

| Method | Path | Maqsad | Status |
|---|---|---|---|
| POST | `` | Single-page audit shuru karo | 202, 422 |
| GET | `/{audit_id}` | Audit poll karo | 200, 404 |

---

## Site crawls — `/api/site-crawls` → [site-crawl-pipeline.md](site-crawl-pipeline.md)

| Method | Path | Maqsad | Status |
|---|---|---|---|
| POST | `` | Site-wide crawl shuru karo | 202, 403, 422 |
| GET | `` | Recent crawls (`?host=&limit=`) | 200, 422 |
| GET | `/{crawl_id}` | Crawl poll karo | 200, 404 |
| POST | `/{crawl_id}/stop` | Cooperative stop | 200, 404 |
| GET | `/{crawl_id}/pages` | Page list (filter/sort/paginate) | 200, 404, 503 |
| GET | `/{crawl_id}/page?url=` | Ek page + in/out links | 200, 404, 503 |
| GET | `/{crawl_id}/issues` | Har issue ke counts + orphans | 200, 404, 503 |
| GET | `/{crawl_id}/templates` | Per-template Lighthouse | 200, 404, 503 |
| GET | `/{crawl_id}/export.{csv,json}` | Streamed page data | 200, 404, 409, 422 |
| GET | `/{crawl_id}/report.html?engine=` | AI audit report (HTML download) | 200, 404, 409, 422, 502, 503 |

Report/export detail: [report-engine-pipeline.md](report-engine-pipeline.md).

---

## Full analyses — `/api/analyses` → [site-analysis-pipeline.md](site-analysis-pipeline.md)

| Method | Path | Maqsad | Status |
|---|---|---|---|
| POST | `` | Full store analysis shuru karo | 202, **400 (GSC connect karo)**, 403, 422 |
| GET | `` | Recent analyses (`?host=&limit=`) | 200, 422 |
| GET | `/{analysis_id}` | Pipeline poll karo | 200, 404 |
| POST | `/{analysis_id}/stop` | Analysis + uska crawl stop karo | 200, 404 |
| GET | `/{analysis_id}/export.{csv,json}` | Spawn hue crawl ka export | 200, 404, 409, 422 |
| GET | `/{analysis_id}/report.html?engine=` | Analysis report (crawl + GSC + schema) | 200, 404, 409, 422, 502, 503 |

---

## Store catalog — `/api/stores` → [store-catalog-pipeline.md](store-catalog-pipeline.md)

> **Route order:** literal paths (`/crawl`, `/crawls`, `/crawl/{id}`) **pehle**,
> `/{store_domain}/...` **baad mein**.

| Method | Path | Maqsad | Status |
|---|---|---|---|
| POST | `/crawl` | Catalog crawl shuru karo | 202, 403, 422 |
| GET | `/crawls` | Recent catalog crawls (`?store_domain=&limit=`) | 200, 422 |
| GET | `/crawl/{crawl_id}` | Catalog crawl poll karo | 200, 404 |
| GET | `/{store_domain}/flags` | Flag counts (`?kind=`) | 200, 422, 503 |
| GET | `/{store_domain}/products` | Product catalog | 200, 422, 503 |
| GET | `/{store_domain}/collections` | Collection catalog | 200, 422, 503 |
| GET | `/{store_domain}/items/{kind}/{handle}` | Ek item + revisions | 200, 404, 422, 503 |

---

## Google auth — `/auth` → [gsc-pipeline.md](gsc-pipeline.md)

| Method | Path | Maqsad | Status |
|---|---|---|---|
| GET | `/login` | Google OAuth shuru karo | 307, 503 |
| GET | `/callback` | OAuth return, tokens store | 307, 400 |
| GET | `/status` | Kya Search Console connected hai? | 200 |
| POST | `/disconnect` | Credential hata do | 200 |

## Search Console — `/api` → [gsc-pipeline.md](gsc-pipeline.md)

| Method | Path | Maqsad | Status |
|---|---|---|---|
| GET | `/sites` | Verified GSC properties (live) | 200, 401, 502 |
| GET | `/search-analytics` | Live Search Analytics query | 200, 401, 422, 502 |
| GET | `/gsc/stored/sites` | Jin properties ka data stored hai | 200, 401, 503 |
| GET | `/gsc/stored/search-analytics` | Mongo se stored rows | 200, 401, 503 |
| GET | `/gsc/syncs` | Sync log | 200, 401, 503 |

---

## Reddit auth — `/auth/reddit` → [reddit-pipeline.md](reddit-pipeline.md)

| Method | Path | Maqsad | Status |
|---|---|---|---|
| GET | `/login` | Reddit OAuth shuru karo | 307, 503 |
| GET | `/callback` | OAuth return, tokens store | 307, 400 |
| GET | `/status` | Kya Reddit connected hai? | 200 |
| POST | `/disconnect` | Credential hata do | 200 |

## Backlinks — `/api/backlinks/reddit` → [reddit-pipeline.md](reddit-pipeline.md)

| Method | Path | Maqsad | Status |
|---|---|---|---|
| GET | `/subreddits` | Subreddit discovery | 200, 401, 429, 502, 503 |
| GET | `/subreddits/{name}/rules` | Rules + flairs | 200, 401, 429, 502, 503 |
| POST | `/drafts` | Gemini ka likha post draft | 200, 401, 422, 429, 502, 503 |
| POST | `/drafts/{draft_id}/publish` | Guardrails ke saath publish | 200, 401, 404, 409, 422, 429, 502 |
| GET | `/posts` | Drafts + published + failed rows | 200, 401 |

---


## Shopify — `/auth/shopify`, `/api/shopify` → [Flows/09](../Flows/09-shopify-connection-flow.md)

| Method | Path | Kya | Status |
|---|---|---|---|
| GET | `/auth/shopify/app` | App URL (Dev Dashboard): frontend ke Shopify section par redirect | 302 |
| GET | `/auth/shopify/install?shop=&reconnect=` | Shopify permission prompt par redirect | 302 |
| GET | `/auth/shopify/callback` | HMAC + state + shop check, code exchange, encrypted store | 302 |
| GET | `/api/shopify/status` | configured? + is session ke connections (token kabhi nahi) | 200 |
| POST | `/api/shopify/connections/{id}/check` | abhi Admin API call karke status | 200, 404, 429, 502, 503 |
| GET | `/api/shopify/connections/{id}/products?first=&after=&q=` | products, cursor paging | 200, 401, 403, 404, 429, 502, 503 |
| POST | `/api/shopify/connections/{id}/disconnect` | credentials delete | 200, 404 |

## Query parameter cheat-sheet

| Param | Endpoints | Bounds / values |
|---|---|---|
| `limit` | list endpoints | 1–200 (default 25) |
| `limit` (pages/catalog) | `/pages`, `/products`, `/collections` | 1–200 (default 50) |
| `offset` | `/pages`, `/products`, `/collections` | >= 0 |
| `sort` (pages) | `/{crawl_id}/pages` | `depth` (default) \| `url` \| `status` \| `words` \| `links` \| `score` |
| `sort` (catalog) | `/products`, `/collections` | `gaps` (default) \| `name` \| `handle` |
| `issue` | `/{crawl_id}/pages` | `broken` \| `redirected` \| `noindex` \| `missing_title` \| `missing_description` \| `missing_canonical` \| `multiple_h1` \| `no_h1` \| `thin` \| `images_missing_alt` \| `blocked_by_robots` \| `failed` \| `orphan` \| `critical` \| `warning` \| `product` \| `collection` \| `heading_skips` |
| `include_navigation` | `/pages`, `/export.{fmt}` | bool, default `false` |
| `engine` | `report.html` | `gemini` (default) \| `ollama` \| `claude` |
| `fmt` (path) | `export.{fmt}` | regex `^(csv\|json)$` |
| `host` | `/api/site-crawls`, `/api/analyses` | optional filter |
| `store_domain` | `/api/stores/crawls` | optional filter |
| `kind` | `/{store_domain}/flags`, `/items/{kind}/...` | `product` \| `collection` |
| `only_gaps` | `/products`, `/collections` | bool |
| `flag` | `/products`, `/collections` | koi bhi SEO gap flag name |
| `siteUrl` | GSC endpoints | property URL **exactly** jaisa `/api/sites` deta hai |
| `startDate` / `endDate` | GSC | `YYYY-MM-DD`, start <= end |
| `dimensions` | GSC | comma list ⊆ `query,page,country,device,date,searchAppearance` |
| `rowLimit` | `/api/search-analytics` | 1–5000 (default 500) |

---

## Request body cheat-sheet

| Endpoint | Body |
|---|---|
| `POST /api/audits` | `{url}` (1–2048, http/https, host mein `.` ya `localhost`) |
| `POST /api/site-crawls` | `{start_url, max_pages?, max_depth?, include_subdomains?, follow_nofollow?, seed_from_sitemap?, page_types?, max_navigation_pages?}` |
| `POST /api/analyses` | `{start_url, max_pages?, max_depth?, include_subdomains?, follow_nofollow?, seed_from_sitemap?, page_types?}` |
| `POST /api/stores/crawl` | `{store_url}` |
| `POST /api/backlinks/reddit/drafts` | `{store_url, subreddit, angle, kind, flair_id?, extra_notes?, auto_publish}` |
| `POST /api/backlinks/reddit/drafts/{id}/publish` | `{title?, body_markdown?, flair_id?, kind?}` |

`max_pages = 0` ka matlab **koi page cap nahi** (site crawl aur analysis dono mein).
Depth aur deadline phir bhi apply hote hain.
