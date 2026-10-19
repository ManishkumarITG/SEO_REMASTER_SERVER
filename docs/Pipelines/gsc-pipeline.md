# Google Search Console pipeline

`/auth/*` · `/api/sites` · `/api/search-analytics` · `/api/gsc/*`

Code flow: [../Flows/05-google-gsc-flow.md](../Flows/05-google-gsc-flow.md)

---

## 1. OAuth endpoints — `/auth`

| Method | Path | Request | Response | Errors |
|---|---|---|---|---|
| GET | `/login` | — | **307** → Google consent | **503** jab `GOOGLE_CLIENT_ID`/`SECRET` blank |
| GET | `/callback` | `?state=&code=` | **307** → `FRONTEND_URL/?connected=1` ya `?connected=0&reason=…` | **400** state mismatch ("possible CSRF"), missing code, token exchange fail |
| GET | `/status` | — | `{connected: bool}` | — |
| POST | `/disconnect` | — | `{connected: false}` | — |

**Details:**

- Scopes: `https://www.googleapis.com/auth/webmasters.readonly` — **read-only**
- PKCE: **haan** (`session["pkce_code_verifier"]`)
- `access_type=offline`, `prompt=consent` — refresh token ke liye
- `/status` **koi Google call nahi karta** — bas SQL mein row hai ya nahi
- `/disconnect` row delete karta hai; **Google ko revoke call nahi** jaati; session apna
  `user_id` rakhta hai (dobara connect karne par wahi id)

---

## 2. Har Google call ka common gate

`integrations/google/oauth.py::execute_google_call(user_id, creds, request)`:

| Situation | Result |
|---|---|
| Credential hi nahi | **401** "Google Search Console is not connected" |
| Access token expired | google-auth refresh karta hai, naya token **persist** hota hai |
| `RefreshError` | **401** "Google access expired or was revoked - please reconnect" **+ credential DELETE** |
| Google 4xx | Same status pass-through |
| Google 5xx | **502** |

> `RefreshError` par credential delete karna deliberate hai: ek revoked refresh token
> kabhi wapas kaam nahi karega.

---

## 3. Live endpoints (Google ko call karte hain)

### `GET /api/sites` → **200**

```json
{ "sites": [ { "siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner" } ] }
```

- `sites.list` call
- `siteUrl` khaali ya `permissionLevel == "siteUnverifiedUser"` wali entries **drop**
- List Mongo `gsc_sites` mein mirror hoti hai (**best-effort** — fail hone par bhi 200)

### `GET /api/search-analytics` → **200**

| Param | Bounds | Default |
|---|---|---|
| `siteUrl` | min 1 char, **exactly** jaisa `/api/sites` deta hai | required |
| `startDate` | `YYYY-MM-DD` | required |
| `endDate` | `YYYY-MM-DD`, start <= end | required |
| `dimensions` | comma list ⊆ `query`, `page`, `country`, `device`, `date`, `searchAppearance` | `query,page` |
| `rowLimit` | 1–5000 | 500 |

```json
{ "rows": [ { "keys": ["blue widget", "https://..."], "clicks": 12,
             "impressions": 340, "ctr": 0.035, "position": 14.2 } ],
  "rowCount": 187, "stored": 187 }
```

- **Ek** `searchanalytics.query` — no paging, no filters
- Rows Mongo `gsc_search_analytics` mein mirror hoti hain
- Har call par ek `gsc_syncs` document likha jaata hai
- `stored` = kitni rows Mongo mein likhi gayin

> **Dono endpoints mein se koi cached nahi hai.** Sirf audit ka `search_console` analyzer
> 1 h cache karta hai.

Errors: **401** connected nahi · **422** date/dimension validation · Google 4xx jaisa ka
taisa · **502** Google 5xx.

---

## 4. Stored endpoints (sirf Mongo, Google ko koi call nahi)

Ye isliye hain taaki past pulls queryable rahein aur API quota bache.

### `GET /api/gsc/stored/sites`

```json
{ "sites": [ { "site_url": "...", "permission_level": "siteOwner",
              "first_seen_at": "...", "last_seen_at": "..." } ] }
```

### `GET /api/gsc/stored/search-analytics`

| Param | Note |
|---|---|
| `siteUrl` | required |
| `startDate` / `endDate` | optional — omit karo to **saari** stored ranges |
| `dimensions` | optional. Valid list honi chahiye, par stored string se **exactly match** karti hai (jaise `query,page`) |
| `limit` | 1–5000 (default 500) |

Rows **clicks desc** se sorted. **Exact-match filters** — koi range query nahi.

### `GET /api/gsc/syncs?limit=`

`limit` 1–200 (default 25). Newest first.

```json
{ "syncs": [ { "site_url": "...", "start_date": "...", "end_date": "...",
              "dimensions": "query,page", "row_count": 187,
              "rows_written": 187, "requested_at": "..." } ] }
```

**Teeno:** session ke bina **401**, kisi bhi Mongo error par **503 "MongoDB unavailable: …"**.

---

## 5. Mongo identities

| Collection | Unique index | Sorted by |
|---|---|---|
| `gsc_sites` | `(user_id, site_url)` | — |
| `gsc_search_analytics` | `(user_id, site_url, start_date, end_date, dimensions, keys_key)` | clicks desc |
| `gsc_syncs` | — | `(user_id, requested_at desc)` |

`keys_key` = `keys` array ko `\x1f` se join kiya hua.

---

## 6. Doosre do consumers

GSC data teen jagah use hota hai. Unka behaviour alag hai:

| | Yahan ke endpoints | Audit analyzer | Analysis GSC phase |
|---|---|---|---|
| Kahan | `services/search_console_service.py` | `services/analyzers/search_console.py` | `services/site_analysis_gsc.py` |
| Property choice | User chunta hai | Sabse **lamba matching URL-prefix**, warna `sc-domain:` | `sc-domain:<host>` **prefer** |
| Window | User chunta hai | 28 din, 2-din lag | 28 din, 2-din lag |
| Rows | `rowLimit` (500) | 25 | `ANALYTICS_ROW_LIMIT = 100` ×2 |
| Cached | **Nahi** | **Haan, 1 h** | **Nahi** |
| Mongo mirror | **Haan** | Haan | **Nahi** |
| URL Inspection | Nahi | Nahi | **Haan** — `MAX_INSPECTIONS = 20`, `INSPECT_CONCURRENCY = 3` |

### Audit analyzer ke finding ids

Frontend `SearchPerformancePanel` inhe **naam se** padhta hai:

| Finding id | Kya |
|---|---|
| `search_console.not_connected` / `.no_session` / `.unavailable` | "Connect Google" prompt |
| `search_console.no_property_match` | Property match nahi hui |
| `search_console.no_data` | Property mili, data nahi |
| `search_console.summary` | `evidence`: matched_property, page, start_date, end_date, clicks, impressions, ctr, position |
| `search_console.top_queries` | `evidence.queries[]` |
| `search_console.striking_distance` | pos 10.5–20.5, impr >=50 |
| `search_console.low_ctr` | impr >=100, CTR <2%, pos <=20.5 |

> Finding id badalne par frontend ka panel **chup ho jaayega**. Dono taraf badlo.

---

## 7. Configuration

| Setting | Default | Zaroori? |
|---|---|---|
| `GOOGLE_CLIENT_ID` | `""` | **Haan** — blank → 503 |
| `GOOGLE_CLIENT_SECRET` | `""` | **Haan** |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/auth/callback` | Google Cloud Console mein **exactly** register hona chahiye |
| `SESSION_SECRET` | `""` | Blank → ephemeral, connections restart par toot jaate hain |
| `FRONTEND_URL` | `http://localhost:5173` | Callback redirect target |
| `MONGODB_URL` / `MONGODB_DB` | `mongodb://localhost:27017` / `seo_remaster` | Mirror aur stored endpoints ke liye |

> `GOOGLE_REDIRECT_URI` mismatch par Google ka `redirect_uri_mismatch` page aata hai jo
> `.env` ka naam tak nahi leta. Isliye `placeholder_env_values()` startup par warn karta
> hai jab usme `yourdomain.com` ho.
