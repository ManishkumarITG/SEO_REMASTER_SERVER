# File map — har file kya karti hai

`server/` ka poora naksha. Isse use karo jab aapko pata ho ki *kya* badalna hai par ye
nahi pata ki *kahan*.

---

## Top level

| Path | Kya |
|---|---|
| `main.py` | ASGI entry point. **Sirf bootstrap** — lifespan, middleware register, routes register. Koi business logic nahi |
| `conftest.py` | Pytest bootstrap: `server/` ko `sys.path` par daalta hai. **pytest `server/` se hi chalana hai** |
| `pyproject.toml` | Sirf tooling (pytest asyncio_mode=auto, ruff line-length 110, py312) |
| `requirements.txt` | Dependencies |
| `requirements.lock.txt` | Pinned versions |
| `.env` / `.env.example` | Settings. `.env.example` hi canonical list hai |
| `seo.db` | Dev SQLite database |

---

## `config/` — settings

| File | Kya |
|---|---|
| `settings.py` | `Settings` (pydantic-settings), `get_settings()` (lru_cache), `unknown_env_keys()`, `placeholder_env_values()`, `SERVER_DIR` |

**Key points:**

- `SERVER_DIR` `__file__` se resolve hota hai, **cwd se kabhi nahi** — production process
  managers cwd = `server/` se start nahi karte
- `.parent.parent` note karo: `server/config/settings.py` → ek parent `server/config`,
  do parent `server/`
- `extra="ignore"` — isliye `unknown_env_keys()` exist karta hai
- **`config/` kabhi `services/` import nahi karta** — settings sab se pehle load hoti hain

---

## `routes/` — HTTP surface

| File | Prefix | Endpoints |
|---|---|---|
| `__init__.py` | — | `register_routes()`. **Order load-bearing hai** |
| `health.py` | `/api` | `/health` |
| `audits.py` | `/api/audits` | POST, GET `/{id}` |
| `site_crawl.py` | `/api/site-crawls` | POST, GET, GET `/{id}`, POST `/{id}/stop`, `/pages`, `/page`, `/issues`, `/templates`, `/export.{fmt}`, `/report.html` |
| `site_analysis.py` | `/api/analyses` | POST, GET, GET `/{id}`, POST `/{id}/stop`, `/export.{fmt}`, `/report.html` |
| `stores.py` | `/api/stores` | POST `/crawl`, GET `/crawls`, GET `/crawl/{id}`, `/{domain}/flags`, `/products`, `/collections`, `/items/{kind}/{handle}` |
| `google_auth.py` | `/auth` | `/login`, `/callback`, `/status`, `/disconnect` |
| `search_console.py` | `/api` | `/sites`, `/search-analytics`, `/gsc/stored/*`, `/gsc/syncs` |
| `reddit_auth.py` | `/auth/reddit` | `/login`, `/callback`, `/status`, `/disconnect` |
| `backlinks.py` | `/api/backlinks/reddit` | `/subreddits`, `/subreddits/{name}/rules`, `/drafts`, `/drafts/{id}/publish`, `/posts` |

Routes mein **sirf** path, status code, Pydantic validation aur `Depends` hote hain.

---

## `controller/` — orchestration

| File | Kya |
|---|---|
| `__init__.py` | Docstring: controllers ke paas apne koi business rules nahi hain |
| `audits.py`, `backlinks.py`, `google_auth.py`, `health.py`, `reddit_auth.py`, `search_console.py`, `site_analysis.py`, `site_crawl.py`, `stores.py` | **Pure pass-through.** Har function ek line ka hai |

> Agar aap controller mein `if` likhne ja rahe ho — wo logic service mein jaani chahiye.

---

## `services/` — business logic

### Top-level services

| File | Kya |
|---|---|
| `audit_service.py` | Audit create/read |
| `health_service.py` | Health response |
| `google_auth_service.py` | Google OAuth flow |
| `reddit_auth_service.py` | Reddit OAuth flow |
| `search_console_service.py` | GSC live + stored endpoints |
| `site_crawl_service.py` | Site crawl create/read/stop/export/report gates, **reap-on-read** |
| `site_crawl_analysis.py` | Crawl ke dauran page scoring (bulk-safe analyzer set enforce karta hai) |
| `site_crawl_export.py` | CSV/JSON streaming (`iter_pages`) |
| `site_crawl_report.py` | Crawl report payload + prompt |
| `site_crawl_templates.py` | Per-template Lighthouse grouping aur representative selection |
| `site_analysis_service.py` | Analysis create/read/stop/export/report gates |
| `site_analysis_gsc.py` | GSC phase (`collect_gsc`, `choose_property`) |
| `site_analysis_schema.py` | Schema phase (`sample_schema`, `choose_schema_sample`) |
| `site_analysis_report.py` | Analysis report payload (+ `derived` cross-checks) |
| `report_engines.py` | **Engine selection + HTTP error mapping.** Ek hi jagah jo engine names jaanti hai |
| `markdown_html.py` | `render_markdown()` — **raw HTML escape karta hai**, aur `_document()` standalone HTML page |

### `services/analyzers/` — page analyzers

| File | Kya |
|---|---|
| `base.py` | `BaseAnalyzer`, `AnalyzerResult`, `Finding`, `Severity`, `SEVERITY_ORDER`, `score_from_findings` |
| `registry.py` | `@register_analyzer` decorator + **automatic module discovery** |
| `on_page.py` | Title, meta, H1, headings, alt, links, canonical, OG, words, lang |
| `technical.py` | Status, redirects, HTTPS, response time, robots, noindex, viewport, charset, favicon |
| `pagespeed.py` | PSI v5 performance + Core Web Vitals |
| `content_ai.py` | Anthropic LLM content analysis |
| `search_console.py` | GSC performance findings |

**Naya analyzer add karna:** bas file drop karo with `@register_analyzer` — koi list
update nahi karni.

### `services/crawler/` — crawl engine

| File | Kya |
|---|---|
| `site_crawl.py` | Site-wide engine, pre-flight probe, page facts. Sabse bada crawler module |
| `urls.py` | URL normalise, link extract, catalog classify |
| `models.py` | `CrawledPage`, `CrawlLimits`, `CrawlStats`, `SiteCrawlResult` |
| `http_client.py` | Pooled client, capped reads, SSRF guard, robots fetch |
| `fetcher.py` | Single-page fetch (**audit bhi ye use karta hai**) |
| `robots.py` | robots.txt parse + Crawl-delay |
| `sitemap.py` | Sitemap discovery |
| `templates.py` | Template detection (`<body data-template>` → body class → URL shape) |
| `throttle.py` | `HostGate` — per-host pacing |
| `parser.py` | HTML parse (selectolax) |

Deep-dive: [../crawler.md](../crawler.md)

### `services/stores/` — store catalog

| File | Kya |
|---|---|
| `store_crawl_service.py` | Create/read endpoints, HTTPException mapping |
| `discovery.py` | JSON tier + sitemap tier |
| `enrichment.py` | Per-item `<head>` fetch + parse |
| `seo_gaps.py` | `item_gaps()`, `catalog_gaps()`, shop suffix inference |
| `models.py` | `CatalogItem` + related |
| `store_key.py` | `(store_domain, kind, handle)` identity |

### `services/backlinks/` — Reddit

| File | Kya |
|---|---|
| `backlink_service.py` | **Feature ke do rules yahan rehte hain:** review before publish, local guardrails before Reddit |
| `guardrails.py` | Daily cap, min gap, dedupe |
| `reddit_post_generator.py` | Gemini prompt building + schema |
| `store_profile.py` | `build_store_profile()` — store fetch + products.json |

### `services/reports/`

| File | Kya |
|---|---|
| `scoring.py` | `weighted_score()`, `grade_for()`, `prioritized_fixes()`, `build_report()` |

---

## `integrations/` — bahar ki duniya

| Path | Kya |
|---|---|
| `google/oauth.py` | **`execute_google_call()`** — saari Google calls ka single gate |
| `google/pagespeed.py` | PSI v5 client |
| `google/gemini.py` | `GeminiService` — retries + model fallback |
| `claude/llm.py` | Anthropic `LLMService.complete_json()` |
| `claude_cli/client.py` | `ClaudeCliService` — `claude` subprocess driver |
| `ollama/client.py` | `OllamaService` — local model server |
| `reddit/client.py` | Reddit API client — token refresh, rate limit, error mapping |
| `reply_text.py` | Reply text helpers |

> **Sirf services hi integrations ko import karti hain.** Routes/controllers kabhi nahi.

---

## `db/` — persistence

| File | Kya |
|---|---|
| `session.py` | Async engine, `get_session()`, `ensure_user()`, `init_db()`, `_add_missing_columns()` |
| `mongo.py` | Per-process `AsyncMongoClient`, `ping()`, lazy index creation, `close_client()` |
| `site_pages.py` | `site_crawl_pages` + `site_crawl_templates` reads/writes, `iter_pages`, `ISSUE_FILTERS` |
| `store_catalog.py` | `store_catalog_items` + `store_catalog_revisions` |

---

## `models/`

| File | Kya |
|---|---|
| `db.py` | SQLAlchemy models: `User`, `Audit`, `GoogleCredential`, `RedditCredential`, `RedditPost`, `StoreCrawl`, `SiteCrawl`, `SiteAnalysis` |

---

## `schemas/` — Pydantic request/response

| File | Kya |
|---|---|
| `__init__.py` | **Flat re-export.** Naya schema yahan bhi add karo |
| `audit.py` | `AuditCreateRequest` (URL normalisation), `AuditResponse`, `AnalyzerReport`, `CategoryReport` |
| `site_crawl.py` | `MAX_REQUESTABLE_PAGES = 5000`, `MAX_REQUESTABLE_DEPTH = 10`, request + ~30-field status model |
| `site_analysis.py` | Analysis request/status/summary |
| `stores.py` | Store crawl + catalog models |
| `backlinks.py` | Draft/publish models |
| `search_console.py` | GSC models |
| `health.py` | `HealthResponse` |
| `report.py` | `ReportEngine` literal |

---

## `exceptions/` — domain exceptions

`__init__.py` sab ko **flat re-export** karta hai.

| File | Exceptions |
|---|---|
| `crawler.py` | `FetchError` |
| `stores.py` | `RobotsDisallowed`, `CatalogUnavailable` |
| `gemini.py` | `GeminiError`, `GeminiNotConfigured`, `GeminiResponseError` |
| `ollama.py` | `OllamaError`, `OllamaNotConfigured`, `OllamaResponseError` |
| `claude_cli.py` | `ClaudeCliError`, `ClaudeCliNotConfigured`, `ClaudeCliResponseError` |
| `llm.py` | `LLMError`, `LLMJSONError`, `LLMNotConfiguredError` |
| `reddit.py` | `RedditError`, `RedditNotConfigured`, `RedditNotConnected`, `RedditAuthError`, `RedditRateLimited`, `RedditAPIError` |
| `backlinks.py` | `DraftValidationError`, `GuardrailError` |

---

## `workers/`

| File | Kya |
|---|---|
| `tasks.py` | **Chaaron background jobs** (~1,280 lines): `run_audit`, `run_store_crawl`, `run_site_crawl`, `run_site_analysis`, plus schedule/stop/reconcile helpers aur saare concurrency constants |

---

## `middleware/`

| File | Kya |
|---|---|
| `__init__.py` | `register_middleware()`. **Order load-bearing:** Session add, phir CORS add (CORS outermost chalta hai) |

---

## `utils/`

| File | Kya |
|---|---|
| `cache.py` | `get_cache()` → `RedisCache` (jab `REDIS_URL` set) ya `InMemoryTTLCache` (`MAX_ENTRIES = 2000`) |

**Cache users:** PSI results (`psi:{strategy}:{categories}:{url}`), audit ke GSC
sites/rows, Reddit subreddit rules (`reddit:rules:{name}`), store profiles
(`store_profile:{url}`). **Failures kabhi cache nahi hote.**

---

## `tests/`

| File | Kya test karta hai |
|---|---|
| `factories.py` | Test data builders |
| `test_*_routes.py` | HTTP layer (`TestClient`) |
| `test_*_worker.py` | Background jobs |
| `test_crawler_*.py`, `test_robots.py`, `test_fetcher_limits.py`, `test_site_crawl*.py` | Crawler engine |
| `test_on_page.py`, `test_technical.py`, `test_registry_and_content_ai.py`, `test_search_console_analyzer.py`, `test_scoring.py` | Analyzers + scoring |
| `test_store_*.py` | Store catalog |
| `test_reddit_*.py` | Reddit client + draft generator |
| `test_ollama_client.py`, `test_claude_cli_client.py` | Report engines |
| `test_mongo_store.py`, `test_store_catalog_mongo.py` | Mongo layer |
| `test_cache.py`, `test_config.py`, `test_markdown_html.py` | Utilities |

---

## Kahan kya badlein — quick lookup

| Mujhe ye badalna hai | File |
|---|---|
| Naya endpoint | `routes/` + `controller/` + `services/` + `schemas/` |
| Naya analyzer | `services/analyzers/<name>.py` (`@register_analyzer`) |
| Crawler ka behaviour | `services/crawler/site_crawl.py` |
| Crawl ki limits | `config/settings.py` (`SITE_CRAWL_*`) |
| Job concurrency | `workers/tasks.py` (`MAX_CONCURRENT_*`) |
| Naya report engine | `services/report_engines.py` + `schemas/report.py` |
| Scoring formula | `services/reports/scoring.py` |
| SEO gap flags | `services/stores/seo_gaps.py` |
| Reddit guardrails | `services/backlinks/guardrails.py` |
| Issue filter values | `db/site_pages.py::ISSUE_FILTERS` |
| SQL schema | `models/db.py` (naya column **nullable** rakho) |
| Mongo indexes | `db/mongo.py` |
| CORS / session | `middleware/__init__.py` |
| Naya `.env` key | `config/settings.py` ka `Settings` + `.env.example` |
