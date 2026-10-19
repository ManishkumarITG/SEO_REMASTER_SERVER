# Configuration reference

Sab kuch `config/settings.py` ke `Settings` class se aata hai.
**Env key = field name ka UPPER_CASE.** `.env` file `server/` mein rehti hai.

`.env.example` canonical list hai — wahan har key ka comment bhi hai.

---

## Settings kaise load hoti hain

```python
SERVER_DIR = Path(__file__).resolve().parent.parent   # server/

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SERVER_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
```

**Teen cheezein jo yaad rakhni hain:**

1. **`SERVER_DIR` `__file__` se resolve hota hai, cwd se kabhi nahi.** Production process
   managers (systemd, Docker, pm2) cwd = `server/` se start nahi karte, aur ek
   cwd-relative `.env` wahan chupchaap kuch load nahi karta — har key apne default par
   chali jaati aur app "successfully" boot ho jaata bina credentials ke.

2. **`.parent.parent` critical hai.** Ye module `server/config/settings.py` hai — ek
   `.parent` = `server/config`, do = `server/`. Galat karne par koi error nahi aata;
   chupchaap koi `.env` load nahi hoti aur `DATABASE_URL` ek fresh empty database par
   point karta hai.

3. **`extra="ignore"`** — ek typo key chupchaap ignore hoti hai. Isiliye
   `unknown_env_keys()` startup par warn karta hai.

4. **`get_settings()` `lru_cache`'d hai** — `.env` sirf **ek baar** padhti hai. `.env`
   badalne ke baad server restart karo.

---

## Startup warnings

| Function | Kya pakadta hai |
|---|---|
| `unknown_env_keys()` | Key ka **naam** kisi setting se match nahi karta (`GIMINI_API_KEY`) |
| `placeholder_env_values()` | Key sahi hai par **value** abhi bhi template hai |

`PLACEHOLDER_MARKERS`: `REPLACE_ME`, `yourdomain.com`, `USER:PASSWORD`,
`xxxxx.mongodb.net` — plus `REDDIT_USER_AGENT` mein `CHANGE_ME` (sirf jab Reddit
configured ho).

> **Values kabhi log nahi hoti — sirf key names.** Mongo marker full placeholder host
> hai (`xxxxx.mongodb.net`), bare `xxxxx` nahi, taaki ek asli key jisme paanch x ho use
> template na samjha jaaye.

---

## Server

| Key | Default | Matlab |
|---|---|---|
| `HOST` | `127.0.0.1` | `python main.py` ka bind address. Reverse proxy ke peeche `127.0.0.1` rakho; direct traffic ke liye `0.0.0.0` |
| `PORT` | `8000` | |
| `DATABASE_URL` | `sqlite+aiosqlite:///{SERVER_DIR}/seo.db` | **Absolute by default** — cwd-relative sqlite path ek doosra khaali database bana deta |
| `SESSION_SECRET` | `""` | **Blank → ephemeral secret + warning.** Google/Reddit connections restart par toot jaayenge |
| `FRONTEND_URL` | `http://localhost:5173` | OAuth callback redirect target; CORS list mein bhi add hota hai |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated |
| `CORS_ORIGIN_REGEX` | localhost / 127.0.0.1 / 192.168.x / 10.x / 172.16-31.x, koi bhi port | Dev convenience. Khaali karo to sirf explicit list |
| `CACHE_TTL_SECONDS` | `3600` | Default cache TTL |
| `REDIS_URL` | `""` | Set karo to `RedisCache`, warna in-process `InMemoryTTLCache` |

---

## MongoDB

| Key | Default | Matlab |
|---|---|---|
| `MONGODB_URL` | `mongodb://localhost:27017` | |
| `MONGODB_DB` | `seo_remaster` | |
| `MONGODB_TIMEOUT_MS` | `1500` | `serverSelectionTimeoutMS` **aur** `connectTimeoutMS` — fail fast. Ek unreachable Mongo `/api/health` ko ~1.5 s cost karta hai |

Mongo **optional** hai: API iske bina chalta hai, par crawl pages, template reports,
catalog items aur GSC mirror save nahi honge.

---

## Google Search Console

| Key | Default | Matlab |
|---|---|---|
| `GOOGLE_CLIENT_ID` | `""` | **Blank → `/auth/login` 503** |
| `GOOGLE_CLIENT_SECRET` | `""` | **Blank → 503** |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/auth/callback` | Google Cloud Console mein **exactly** register hona chahiye |
| `PAGESPEED_API_KEY` | `""` | PSI ke liye. Blank → `pagespeed` analyzer skip |

Scopes hardcoded hain: `webmasters.readonly` (read-only).

---

## LLM / report engines

### Anthropic (audit ka `content_ai` analyzer)

| Key | Default |
|---|---|
| `ANTHROPIC_API_KEY` | `""` — blank → `content_ai` skip |
| `LLM_MODEL` | `claude-sonnet-5` |

### Gemini (Reddit drafts + default report engine)

| Key | Default | Matlab |
|---|---|---|
| `GEMINI_API_KEY` | `""` | **Blank → drafts aur gemini reports 503** |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Primary |
| `GEMINI_FALLBACK_MODELS` | `gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash-lite` | Primary persistently overloaded (5xx after retries) hone par in par fall back. Alag flash deployments, isliye capacity spike sab par rare hai; aakhri ek lite model hai. **Blank → fallback off** |

### Ollama (local report engine)

| Key | Default | Matlab |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | **Blank → `?engine=ollama` 503.** Gemini unaffected |
| `OLLAMA_MODEL` | `qwen3:4b` | Pehle se pulled hona chahiye: `ollama pull qwen3:4b` |
| `OLLAMA_TIMEOUT_S` | `1800.0` | CPU par 4B model ~9 tok/s: crawl report ~6 min, full-analysis report 10–15 min. GPU par kaafi tez. **Generous on purpose — timeout kaam phenk deta hai** |
| `OLLAMA_NUM_CTX` | `16384` | Floor. Client prompt fit karne ke liye per-call **raise** karta hai. Ollama ki default 4096 window lambe prompt ko **shuru se** truncate karti hai — yaani system instruction |

### Claude CLI (koi API key nahi)

| Key | Default | Matlab |
|---|---|---|
| `CLAUDE_CLI_PATH` | `claude` | PATH ka command name ya full path. npm `claude.cmd` shim `claude.exe` par resolve hota hai |
| `CLAUDE_CLI_MODEL` | `""` | Blank = CLI ka apna default. Aliases: `sonnet`, `opus`, `fable` |
| `CLAUDE_CLI_EFFORT` | `""` | Blank = CLI default. **`max` mat use karo** — ek crawl report 15 min mein finish nahi hua. `high` use karo |
| `CLAUDE_CLI_TIMEOUT_S` | `900.0` | Measured: crawl report ~198 s, full-analysis ~462 s (default `claude-fable-5-1`, effort `xhigh`). Timeout par process tree kill hota hai aur report chali jaati hai |

> Report us account ki **subscription** par likhi jaati hai jo server par `claude` se
> logged in hai. Har live run quota kharch karta hai.

---

## Reddit

| Key | Default | Matlab |
|---|---|---|
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | `""` | **Blank → `/auth/reddit/*` 503** |
| `REDDIT_REDIRECT_URI` | `http://localhost:8000/auth/reddit/callback` | Reddit app mein **exactly** register |
| `REDDIT_USER_AGENT` | `web:seo-analyzer:v0.1.0 (by /u/CHANGE_ME)` | Reddit `<platform>:<app id>:<version> (by /u/<username>)` maangta hai. **Generic UA throttle hota hai.** `CHANGE_ME` par startup warning |
| `REDDIT_SCOPES` | `identity submit read mysubreddits flair` | Space-separated |
| `REDDIT_MAX_POSTS_PER_DAY` | `5` | Guardrail → 429 |
| `REDDIT_MIN_MINUTES_BETWEEN_POSTS` | `10` | Guardrail → 429 |
| `REDDIT_DEDUPE_DAYS` | `30` | Same store + same subreddit → 409 |

---

## Store catalog crawler

| Key | Default | Matlab |
|---|---|---|
| `CRAWL_MAX_ENRICH_ITEMS` | `2000` | Discovery uncapped hai; enrichment (ek `<head>` fetch per item, ~0.2 s) capped — taaki ek 10,000-product store aadha ghanta slot hold na kare |
| `CRAWL_ENRICH_CONCURRENCY` | `8` | Blocked hone par 4 |
| `CRAWLER_USER_AGENT` | `SEOAnalyzerBot/0.1 (+https://github.com/seo-remaster/bot)` | **Honest bot identity with contact URL. Browser UA spoof karna ToS breach hai** |
| `CRAWL_HTTP2` | `false` | Opt-in. Measured 23-item crawl par koi fark nahi (6.88 s vs 6.95 s). `pip install "httpx[http2]"` chahiye; `h2` na ho to chupchaap HTTP/1.1 |

---

## Site-wide crawl

Store catalog se **alag knobs** — ye poore pages fetch karta hai aur apna frontier khud
discover karta hai, isliye pages **aur** depth **aur** wall clock teeno se bounded hai.

| Key | Default | Matlab |
|---|---|---|
| `SITE_CRAWL_MAX_PAGES` | `500` | Default page cap (request override kar sakti hai; 0 = unlimited) |
| `SITE_CRAWL_MAX_DEPTH` | `5` | |
| `SITE_CRAWL_CONCURRENCY` | `6` | |
| `SITE_CRAWL_DEADLINE_S` | `1800` (30 min) | **10 min nahi.** Ek measured 298-page crawl 537 s le gaya, to 600 s binding limit banne wala tha — aur ab jab 429 ka jawab wait karna hai (give up nahi), rate-limited crawl ko legitimately zyada chahiye. **Stop button hi asli interruption hai; ye sirf backstop hai** |
| `SITE_CRAWL_MIN_INTERVAL_S` | `0.1` | Request starts ke beech floor. **robots `Crawl-delay` ise badha sakta hai, ghata nahi** |
| `SITE_CRAWL_INCLUDE_SUBDOMAINS` | `false` | `example.com` se seed hua crawl jo `shop.example.com` mein ghus jaaye wo usually surprise hai, feature nahi |
| `SITE_CRAWL_ANALYZERS` | `on_page,technical` | **Sirf bulk-safe analyzers.** `pagespeed`, `content_ai`, `search_console` har ek apni outbound call karte hain → 500 pages = 500 Lighthouse runs ya 500 LLM completions. `services/site_crawl_analysis.py` is set ke bahar ka naam **refuse** karta hai. Khaali string = page scoring off |
| `SITE_CRAWL_TEMPLATE_LIGHTHOUSE` | `true` | Page walk ke baad **ek PSI run per TEMPLATE**, per page nahi. `false` = crawl se koi PageSpeed call nahi |
| `SITE_CRAWL_MAX_TEMPLATES` | `8` | |
| `SITE_CRAWL_LIGHTHOUSE_STRATEGY` | `mobile` | |
| `SITE_CRAWL_TEXT_CHARS` | `8000` | Per page kitna main body text store karna (chrome stripped). `0` = kuch nahi. **Raw markup kabhi store nahi hota** |

---

## Computed properties

| Property | Kya |
|---|---|
| `cors_origin_list` | `CORS_ORIGINS` + `FRONTEND_URL` (trailing slash strip, dedupe, sorted) |
| `gemini_configured` | `bool(GEMINI_API_KEY)` |
| `reddit_configured` | `bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)` |
| `reddit_scope_list` | `REDDIT_SCOPES` ko whitespace par split |

---

## Minimum `.env` — kya kiske liye chahiye

| Feature chahiye | Kya set karo |
|---|---|
| **Kuch nahi** (app boot hota hai) | — |
| Page audit (basic) | — |
| Page audit + PageSpeed | `PAGESPEED_API_KEY` |
| Page audit + AI content | `ANTHROPIC_API_KEY` |
| Site crawl | — (par pages save karne ke liye Mongo) |
| Crawl pages persist | `MONGODB_URL`, `MONGODB_DB` |
| Search Console tab | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `SESSION_SECRET` |
| Full analysis | Upar wale sab (GSC gate hai) |
| Gemini report | `GEMINI_API_KEY` |
| Ollama report | `OLLAMA_BASE_URL` + model pulled |
| Claude report | Server par `claude` installed + logged in |
| Reddit backlink | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `GEMINI_API_KEY` |

---

## Naya setting add karna

```python
# config/settings.py
class Settings(BaseSettings):
    ...
    my_new_setting: str = "default"      # env key: MY_NEW_SETTING
```

Phir `.env.example` mein ek commented entry add karo. **Dono karo** — warna ya to key
chupchaap ignore hogi (`extra="ignore"`), ya kisi ko pata hi nahi chalega ki wo exist
karti hai.
