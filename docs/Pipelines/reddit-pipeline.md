# Reddit backlink pipeline — **beta**

`/auth/reddit/*` · `/api/backlinks/reddit/*`

Code flow: [../Flows/06-reddit-backlink-flow.md](../Flows/06-reddit-backlink-flow.md)

> **Is domain mein koi MongoDB nahi hai.** `reddit_posts` table hi draft store bhi hai
> aur guardrail ledger bhi.

---

## 1. OAuth — `/auth/reddit`

Authorization-code grant, HTTP Basic client auth, `duration=permanent`, session mein
CSRF `state`. **PKCE nahi.**

Scopes: `identity submit read mysubreddits flair`

| Method | Path | Response | Errors |
|---|---|---|---|
| GET | `/login` | **307** → `reddit.com/api/v1/authorize` | **503** jab `REDDIT_CLIENT_ID`/`SECRET` blank |
| GET | `/callback` | **307** → `FRONTEND_URL/?tab=backlink&reddit=connected&username=…` | **400** bad state / missing code / expired session. Koi bhi Reddit error → `reddit=error&reason=<ExceptionName>` |
| GET | `/status` | `{configured, connected, username, link_karma, comment_karma, account_age_days}` — **SQL se, koi Reddit call nahi** | — |
| POST | `/disconnect` | row delete (koi revoke nahi) | — |

> Ye **same** session aur **same** `users` row use karta hai jo Google karta hai — ek
> browser session ek saath dono se connected ho sakta hai.

---

## 2. Client behaviour — `integrations/reddit/client.py`

| Concern | Behaviour |
|---|---|
| Token refresh | Proactive (60 s skew) + 401 par ek baar aur |
| User-Agent | `REDDIT_USER_AGENT` = `web:seo-analyzer:v0.1.0 (by /u/…)`. **Generic UA throttle ho jaata hai** |
| Rate limit | `X-Ratelimit-*` headers capture, `ratelimit_remaining` mein surface |
| Timeout | 20 s per call |

### Error mapping (`_http_for`)

| Exception | HTTP |
|---|---|
| `RedditNotConfigured` | 503 |
| `RedditNotConnected` / `RedditAuthError` | 401 |
| `RedditRateLimited` | **429, Reddit ke apne message ke saath — KABHI retry nahi** |
| Baaki `RedditAPIError` | 502 |

429 par retry **deliberately nahi** hota: Reddit ka rate limit account-level signal hai,
aur usse ladna ban karwa sakta hai.

---

## 3. `GET /api/backlinks/reddit/subreddits` → **200**

```json
{ "moderated": [ { "name": "mysubreddit", "title": "...",
                   "subscribers": 1200, "you_moderate": true } ],
  "subscribed": [ ... ] }
```

- `GET /subreddits/mine/moderator?limit=100` + `/subreddits/mine/subscriber?limit=100`
- Har ek ka **ek** page (no paging)
- `subscribed` = subscribed **minus** moderated
- Subscribers desc se sorted

---

## 4. `GET /api/backlinks/reddit/subreddits/{name}/rules` → **200**

```json
{ "subreddit": "mysubreddit",
  "rules": [ { "short_name": "No self-promo", "description": "..." } ],
  "flairs": [ { "id": "...", "text": "Discussion" } ],
  "self_promo_warning": true,
  "public_description": "..." }
```

| Source | Cached? |
|---|---|
| `/r/{name}/about/rules` + `/r/{name}/about` | **1 h**, key `reddit:rules:{name}` |
| `/r/{name}/api/link_flair_v2` | **Nahi**. 403 → `[]` |

**`self_promo_warning`** tab true hota hai jab kisi rule text mein marker mile:
*self-promo*, *advertis*, *no spam*, *affiliate*, *vendor*, *no selling*, …

---

## 5. `POST /api/backlinks/reddit/drafts` → **200**

**Synchronous on purpose** — user wait kar raha hai aur result review karega.

```json
{
  "store_url": "https://example-store.com",
  "subreddit": "mysubreddit",
  "angle": "intro",
  "kind": "self",
  "flair_id": null,
  "extra_notes": null,
  "auto_publish": false
}
```

| Field | Rules |
|---|---|
| `store_url` | required |
| `subreddit` | `r/` strip hota hai, phir `[A-Za-z0-9_]{1,40}` |
| `angle` | `intro` \| `behind_the_brand` \| `feedback` \| `announcement` \| `helpful` |
| `kind` | `self` (text post) \| `link` |
| `flair_id` | optional |
| `extra_notes` | optional, <=2000 chars |
| `auto_publish` | default `false` |

### Kya hota hai

```
require_user (401)
  → Gemini configured? (503)
  → subreddit rules (cached) + flairs
  → build_store_profile: fetch_page (15 s, 5 MiB) + /products.json?limit=12 (2 MiB)
     → name, tagline, excerpt(1500), is_shopify, 12 products, categories
     [1 h cached: store_profile:{url}]
  → Gemini generate_structured(temperature 0.7, 2048 tokens, 2 attempts on invalid JSON)
  → validate_draft(): title 1-300, body 1-40,000
  → INSERT reddit_posts(status="draft")
  → [auto_publish ? _publish(auto=true)]
```

Response:

```json
{
  "draft_id": "...", "status": "draft", "subreddit": "mysubreddit",
  "angle": "intro", "kind": "self",
  "title": "...", "body_markdown": "...",
  "suggested_flair_text": "Discussion", "flair_id": null,
  "disclosure_included": true,
  "fit_score": 4,
  "rule_check_notes": [ "..." ],
  "gemini_model": "gemini-3.5-flash",
  "created_at": "...",
  "store_profile": { "store_url": "...", "store_name": "...", "tagline": "...",
                     "is_shopify": true, "product_count": 12,
                     "categories": [...], "products_source": "products.json" },
  "publish": null
}
```

`fit_score` 1–5 — Gemini ka apna assessment ki ye post us subreddit ke liye kitni fit
hai. Frontend 1–2 ko red dots dikhata hai.

> `auto_publish=true` par bhi HTTP **200** aata hai chahe publish fail ho — failure
> `publish` object ke andar hoti hai aur row `failed` mark ho jaati hai.

Errors: **401** · **503** Gemini/Reddit unconfigured · **422** store unreachable ya
`DraftValidationError` · **429** Reddit rate limit · **502** Gemini ya Reddit failure.

---

## 6. `POST /api/backlinks/reddit/drafts/{draft_id}/publish` → **200**

```json
{ "title": "...", "body_markdown": "...", "flair_id": null, "kind": "self" }
```

Saare fields optional — **edits validation se pehle apply hote hain**.

### Guardrails (`services/backlinks/guardrails.py`)

Sirf **is user ki `published` rows** count karte hain.

| Rule | Setting | Default | Result |
|---|---|---|---|
| Daily cap | `REDDIT_MAX_POSTS_PER_DAY` | 5 | **429** "Daily limit reached…" |
| Minimum gap aakhri publish se | `REDDIT_MIN_MINUTES_BETWEEN_POSTS` | 10 | **429** "Too soon…" |
| Same store + same subreddit recently | `REDDIT_DEDUPE_DAYS` | 30 | **409** "…already posted to r/… ({permalink})" |

> Ye Reddit ke apne rate limit se **pehle** chalte hain — kyunki Reddit ka limit hit
> karna account ke liye mehnga hai.

### Phir Reddit submit

`POST /api/submit` with `api_type=json`, `sr`, `kind`, `title`, `text`\|`url`,
`flair_id`, `sendreplies=true`.

| Outcome | Result |
|---|---|
| Reddit `RATELIMIT` error | Row `failed` + **429** Reddit ki wording ke saath |
| Doosra Reddit error | Row `failed` + 401/502/503 |
| Success | `status="published"`, `reddit_post_id`, `permalink`, `published_at` |

Response `PublishResult`:

```json
{ "draft_id": "...", "status": "published", "subreddit": "mysubreddit",
  "permalink": "https://reddit.com/r/.../comments/.../",
  "reddit_post_id": "t3_...", "published_at": "...",
  "auto_published": false, "error_message": null,
  "ratelimit_remaining": 58 }
```

Errors: **404** unknown/foreign draft · **409** already published · **422** khaali title,
title >300, self post bina body, body >40,000 · **429** guardrail ya Reddit · **502**.

---

## 7. `GET /api/backlinks/reddit/posts?limit=` → **200**

`limit` 1–200 (default 50). User ke drafts, published aur failed rows **newest first**.
**Koi Reddit call nahi.**

```json
{ "posts": [ { "draft_id": "...", "created_at": "...", "published_at": "...",
               "store_url": "...", "store_name": "...", "subreddit": "...",
               "angle": "intro", "kind": "self", "title": "...",
               "status": "published", "fit_score": 4,
               "permalink": "...", "error_message": null,
               "auto_published": false } ] }
```

---

## 8. Configuration

| Setting | Default | Note |
|---|---|---|
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | `""` | Blank → `/auth/reddit/*` 503 |
| `REDDIT_REDIRECT_URI` | `http://localhost:8000/auth/reddit/callback` | Reddit app mein **exactly** register |
| `REDDIT_USER_AGENT` | `web:seo-analyzer:v0.1.0 (by /u/CHANGE_ME)` | **Asli username daalo** — generic UA throttle hota hai; startup warning deta hai |
| `REDDIT_SCOPES` | `identity submit read mysubreddits flair` | |
| `REDDIT_MAX_POSTS_PER_DAY` | 5 | Guardrail |
| `REDDIT_MIN_MINUTES_BETWEEN_POSTS` | 10 | Guardrail |
| `REDDIT_DEDUPE_DAYS` | 30 | Guardrail |
| `GEMINI_API_KEY` | `""` | Draft generation ke liye **zaroori** — blank → 503 |

Reddit app **"web app"** type ka hona chahiye (reddit.com/prefs/apps).

---

## 9. Data model — `reddit_posts`

Ek hi table teen kaam karta hai: draft store, publish log, guardrail ledger.

| Group | Columns |
|---|---|
| Identity | `id` (= draft id), `user_id` |
| Input | `store_url`, `subreddit`, `angle`, `kind` |
| Content | `title`, `body_markdown`, `flair_id`, `flair_text` |
| State | `status` ∈ `draft`/`published`/`failed`, `error_message` |
| Gemini | `fit_score`, `rule_check_notes`, `gemini_model` |
| Result | `reddit_post_id`, `permalink`, `published_at`, `auto_published` |
