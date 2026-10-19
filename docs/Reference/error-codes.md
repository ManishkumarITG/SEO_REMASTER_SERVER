# Error codes reference

Koi custom exception handler registered nahi hai. **Services directly `HTTPException`
raise karti hain**, aur routes kabhi raw integration errors nahi dekhte.

---

## Status code → matlab

| Status | Matlab | Client ko kya karna chahiye |
|---|---|---|
| **400** | User ke connections par ek precondition fail hui | User ko batao kya connect karna hai |
| **401** | Google/Reddit connected nahi, ya access revoke ho gaya | Reconnect prompt dikhao |
| **403** | Target site ka robots.txt crawler ko disallow karta hai | Ye retry karne layak nahi — site ne mana kiya hai |
| **404** | Unknown id | — |
| **409** | Job abhi terminal nahi hua, ya resource already published | Job poll karte raho, phir retry |
| **422** | Validation fail | Input theek karo |
| **429** | Rate limit (local guardrail ya Reddit) | **Retry mat karo** — message mein wait time hota hai |
| **502** | Upstream live fail hui | Retry kar sakte ho |
| **503** | Feature configured hi nahi, ya MongoDB down | Config fix karo — retry se kuch nahi hoga |

---

## Har endpoint ke error codes

### Audits

| Status | Kab |
|---|---|
| 422 | `url` 1–2048 nahi; scheme http/https nahi; host mein `.` nahi aur `localhost` bhi nahi |
| 404 | Unknown `audit_id` — "Audit not found" |

> Fetch fail hone par **404/422 nahi** aata. Audit `failed` status ke saath complete
> hota hai aur `error` field mein wajah hoti hai.

### Site crawls

| Status | Kab | Exception |
|---|---|---|
| 403 | Site ka robots.txt disallow karta hai | `RobotsDisallowed` |
| 404 | Unknown `crawl_id`; `/page?url=` ka URL is crawl mein nahi | — |
| 409 | Crawl terminal hone se pehle export/report maanga | — |
| 422 | `start_url` mein host nahi; body validation; probe site tak nahi pahuncha | `FetchError` |
| 503 | Page-data read par MongoDB unreachable | `PyMongoError` |

### Full analyses

| Status | Kab |
|---|---|
| **400** | **"Connect Google Search Console before running a full store analysis."** |
| 403 / 422 | Crawl probe jaise hi (robots / unreachable) |
| 404 | Unknown `analysis_id`; ya analysis ka crawl row gayab |
| 409 | Terminal se pehle export/report; ya analysis ne koi crawl produce nahi kiya |
| 502 / 503 | Report engine |

### Store catalog

| Status | Kab | Exception |
|---|---|---|
| 403 | robots.txt blanket-disallow | `RobotsDisallowed` |
| 404 | Unknown crawl id ya item — "Item not found…" | — |
| 422 | Host nahi; "No product or collection catalog found … tried /products.json, /collections.json, /sitemap.xml"; unreachable / non-public host | `CatalogUnavailable`, `FetchError` |
| 503 | Catalog read par MongoDB down | `PyMongoError` |

> Kabhi crawl na hua store ek **empty list** deta hai, 404 nahi.

### Google / Search Console

| Status | Kab |
|---|---|
| 401 | Credential nahi ("Google Search Console is not connected"); ya `RefreshError` ("Google access expired or was revoked - please reconnect") **+ credential delete ho jaata hai** |
| 400 | OAuth callback: state mismatch ("possible CSRF"), missing code, token exchange fail |
| 422 | Date / dimension validation |
| 502 | Google 5xx |
| 503 | `GOOGLE_CLIENT_ID`/`SECRET` blank; ya stored endpoints par MongoDB down |
| Google 4xx | **Jaisa ka taisa pass-through** |

### Reddit

| Status | Kab | Exception |
|---|---|---|
| 503 | Reddit ya Gemini configured nahi | `RedditNotConfigured`, `GeminiNotConfigured` |
| 401 | Connected nahi, ya auth error | `RedditNotConnected`, `RedditAuthError` |
| 400 | OAuth callback: bad state / missing code / expired session | — |
| 404 | Unknown ya kisi aur ka draft | — |
| 409 | Already published; ya dedupe guardrail ("…already posted to r/… ({permalink})") | `GuardrailError` |
| 422 | Store unreachable; draft validation (title 1–300, body 1–40,000, self post bina body) | `FetchError`, `DraftValidationError` |
| **429** | Daily cap ("Daily limit reached…"); min gap ("Too soon…"); **ya Reddit ka apna RATELIMIT — kabhi retry nahi hota** | `GuardrailError`, `RedditRateLimited` |
| 502 | Gemini ya Reddit failure | `GeminiError`, `RedditAPIError` |

### Reports aur exports

| Status | Kab |
|---|---|
| 404 | Unknown crawl/analysis id |
| 409 | Terminal nahi; ya analysis ne crawl produce nahi kiya |
| 422 | `engine` `gemini\|ollama\|claude` mein nahi; `fmt` `csv\|json` nahi |
| 502 | Engine live fail hui — "{label} could not generate the report: …" |
| 503 | Engine configured hi nahi |

**503 messages** (exact):

```
Report generation is unavailable: GEMINI_API_KEY is not set.
Report generation is unavailable: OLLAMA_BASE_URL is not set.
Report generation is unavailable: the Claude Code CLI is not installed on the server.
  Install it, run `claude` once to log in, or set CLAUDE_CLI_PATH.
```

---

## Domain exceptions (`exceptions/`)

Sab flat re-export hoti hain — `from exceptions import FetchError` kaam karta hai.

| Module | Exceptions | HTTP mapping |
|---|---|---|
| `crawler.py` | `FetchError` | 422 |
| `stores.py` | `RobotsDisallowed` · `CatalogUnavailable` | 403 · 422 |
| `gemini.py` | `GeminiError` · `GeminiNotConfigured` · `GeminiResponseError` | 502 · 503 · 502 |
| `ollama.py` | `OllamaError` · `OllamaNotConfigured` · `OllamaResponseError` | 502 · 503 · 502 |
| `claude_cli.py` | `ClaudeCliError` · `ClaudeCliNotConfigured` · `ClaudeCliResponseError` | 502 · 503 · 502 |
| `llm.py` | `LLMError` · `LLMJSONError` · `LLMNotConfiguredError` | analyzer ke andar handle (skip) |
| `reddit.py` | `RedditError` · `RedditNotConfigured` · `RedditNotConnected` · `RedditAuthError` · `RedditRateLimited` · `RedditAPIError` | — · 503 · 401 · 401 · **429** · 502 |
| `backlinks.py` | `DraftValidationError` · `GuardrailError` | 422 · 409/429 |

---

## Error response ka shape

FastAPI ka standard:

```json
{ "detail": "Audit not found" }
```

Validation errors (422) par `detail` ek **list** hota hai:

```json
{ "detail": [ { "type": "...", "loc": ["body", "url"], "msg": "...", "input": "..." } ] }
```

Frontend `lib/api.ts` dono handle karta hai:

```js
const raw = body.detail;
detail = typeof raw === "string" ? raw : JSON.stringify(raw);
```

---

## Aise errors jo HTTP errors nahi hain

Kuch failures **200** ke saath aati hain, kyunki job chal gaya tha:

| Situation | Kaise dikhta hai |
|---|---|
| Audit ka fetch fail hua | `status: "failed"` + `error` field |
| Crawl ne kuch pages fetch kiye phir rukk gaya | `status: "partial"` + `aborted_reason` / `deadline_hit` / `hit_page_budget` |
| User ne crawl stop kiya | `status: "stopped"` — **failure nahi** |
| MongoDB down tha crawl ke dauran | `persistence_ok: false` + `error_message` |
| Analysis ka GSC phase fail hua | `gsc_error` set, `status` phir bhi complete/partial |
| Analysis ka schema phase fail hua | `notes[]` mein ek line |
| Ek analyzer crash hua | Us analyzer ka `score: null` + `error: "Type: msg"` |
| `auto_publish` draft publish nahi ho paaya | HTTP **200**, par `publish.status: "failed"` + `publish.error_message` |

> **Lesson:** sirf HTTP status se "sab theek hai" mat maano. Job responses ka `status`,
> `error`, `notes` aur `persistence_ok` padho.

---

## Troubleshooting lookup

| Symptom | Dekho |
|---|---|
| "Feature works locally, nothing happens in prod" | Startup logs — `unknown_env_keys()` / `placeholder_env_values()` warnings |
| Google `redirect_uri_mismatch` page | `GOOGLE_REDIRECT_URI` vs Google Cloud Console |
| Restart ke baad connections gayab | `SESSION_SECRET` blank hai (startup warning) |
| "no such column" runtime par | Naya column NOT NULL add hua — nullable karo (koi Alembic nahi) |
| Crawl `failed` with `pages_fetched: 0` | robots disallow, ya probe fail |
| Pages save nahi hue | `persistence_ok: false` → MongoDB |
| Report button par kuch nahi hota | Browser console — 502/503 detail dekho |
| Reddit 429 | Guardrail message batata hai kitna wait karna hai |
| "database is locked" | Kisi worker ne network phase ke across session hold kiya |
