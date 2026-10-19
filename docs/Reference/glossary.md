# Glossary — is codebase ke terms

Shabd jo yahan ek **specific** matlab rakhte hain, general meaning se alag.

---

## Job aur state

| Term | Matlab |
|---|---|
| **Job row** | SQL mein ek row jo ek long-running kaam ki state rakhti hai (`audits`, `store_crawls`, `site_crawls`, `site_analyses`) |
| **Model A** | Detached asyncio task. `schedule_*()` se banta hai, module dict mein rehta hai, client disconnect par bhi zinda, heartbeat karta hai, stop support karta hai. **Site crawl aur analysis** |
| **Model B** | FastAPI `BackgroundTasks`. Response ke baad web process ke andar chalta hai. Na heartbeat, na stop. **Audit aur store catalog crawl** |
| **Terminal status** | `complete` \| `partial` \| `failed` \| `stopped`. Client ki polling yahan ruk jaati hai |
| **`partial`** | Kuch mila, par ek cap/deadline/failure ne use adhoora chhoda. **Data valid hai** |
| **`stopped`** | User ne stop kaha. **Failure nahi** — jo mila wo rakha gaya |
| **Heartbeat** | `heartbeat_at` column jo Model A job periodically update karta hai |
| **Reconcile** | Startup par atki hui rows ko `failed` mark karna (`reconcile_orphaned_*`) |
| **Reap-on-read** | Model A job row ke `GET` par: terminal nahi + koi live task nahi + heartbeat stale → **read path mein hi** failed mark karna |
| **Stale threshold** | Kitna purana heartbeat "ye mar chuka hai" maana jaaye. `SITE_CRAWL_STALE_AFTER_S = 180`, `ANALYSIS_STALE_AFTER_S = 300` |
| **Cooperative stop** | Task cancel **nahi** hota. Id ek `_stop_requested` set mein jaati hai; worker units of work ke beech `should_stop()` check karta hai, current unit finish karta hai, persist karta hai, `stopped` par jaata hai |
| **`reused`** | Us host/domain ka ek job pehle se in-flight tha, to naya nahi bana — purana id wapas mila |

---

## Crawl

| Term | Matlab |
|---|---|
| **Frontier** | URLs ki queue jo abhi crawl honi baaki hain. Site crawl ka frontier **chalte-chalte badhta hai** — yahi use baaki do crawl shapes se alag banata hai |
| **Teen crawl shapes** | (1) Single page — `fetcher.py::fetch_page`, koi frontier nahi. (2) Store catalog — `services/stores/`, frontier pehle se pata. (3) Site-wide — `services/crawler/site_crawl.py`, **badhta hua** frontier |
| **Target page** | Wo page jo `page_types` filter se match karta hai — jo crawl ne asal mein maanga tha |
| **Navigation page** | Wo page jo sirf targets tak pahunchne ke liye fetch hua. `is_target=false`. Listings/exports se chupa rehta hai jab tak `include_navigation=true` na ho |
| **Orphan** | Jis page par kisi doosre crawled page se koi inbound link nahi hai. **Sirf poora graph pata hone ke baad** compute hota hai |
| **Template** | Kis layout ne page render kiya: `product`, `collection`, `home`, `blog`, `article`, `page`, `search`, `cart`, `account`, `policy`, `other`. Detection: `<body data-template>` → body class `template-<name>` → URL shape |
| **Representative page** | Ek template group ka **median page by word count** — Lighthouse isi par chalti hai, taaki ek asaamanya lamba/khaali page poore template ko describe na kare |
| **`HostGate`** | Per-host pacing object (`services/crawler/throttle.py`). Minimum interval enforce karta hai, 429/503 par ×4 back off, success par ×0.8 recover |
| **Throttle loop** | 429/503 par `Retry-After` honour karte hue retry. `MAX_THROTTLE_RETRIES = 4`. **Crawl kabhi abort nahi hota** |
| **Skip record** | Ek page jo deliberately fetch nahi hua (robots, asset extension, off-site, non-HTML). `MAX_SKIP_RECORDS = 200` store hote hain, baaki sirf count |
| **Seeding** | Frontier ko `{origin}/sitemap.xml` aur robots `Sitemap:` URLs se bharna, crawl shuru hone se pehle |
| **Pre-flight probe** | `POST` par 2 requests (robots.txt + start page) — taaki ek reachable-nahi site 202 ke bajaye turant 422 de |
| **Bulk-safe analyzer** | Wo analyzer jo apni koi outbound call nahi karta — sirf jo DOM use handed kiya gaya hai wo padhta hai. Sirf `on_page` aur `technical` |

---

## Store catalog

| Term | Matlab |
|---|---|
| **Discovery tier** | Catalog kahan se aaya: **JSON tier** (`/products.json`, `/collections.json`) ya **sitemap tier** |
| **`source`** | `products_json` \| `sitemap` \| `mixed` |
| **Enrichment** | Har item ke page ka `<head>` fetch karke meta title/description/canonical nikalna |
| **Phase-1 persist** | Discovery ke turant baad items Mongo mein likhna, **flags ke bina** (`enriched=false`). Isliye `only_gaps` mid-crawl under-report karta hai |
| **Shop suffix** | Wo trailing `" \| X"` / `" – X"` segment jo **>=60% enriched titles** mein shared ho |
| **`is_default` flag** | Title mein se shop suffix hataane par jo bacha wo item name ke barabar hai (description ke liye: pehle 120 chars) |
| **`content_hash`** | SEO-relevant fields ka SHA-256. **Sirf enrichment ke baad set hota hai.** Badalne par ek revision likhi jaati hai |
| **Revision** | `store_catalog_revisions` mein ek snapshot — re-crawl se banne wali history |
| **Abort** | Enrichment ka jald khatma: **5 consecutive 429/503** |

---

## Analysis

| Term | Matlab |
|---|---|
| **Phase** | Analysis pipeline ka step: `pending` → `crawl` → `gsc` → `schema` → `done`. `status` se **alag** |
| **Best-effort phase** | Jo phase fail hone par analysis ko fail **nahi** karta — GSC aur schema. Failure `gsc_error` ya `notes[]` mein jaati hai |
| **GSC gate** | `POST /api/analyses` tab tak **400** deta hai jab tak Google connected na ho |
| **URL Inspection sample** | `MAX_INSPECTIONS = 20` URLs, templates ke across **round-robin**. Poori site nahi — GSC API bulk mein unindexed pages list nahi kar sakti |
| **Stop cascade** | Analysis ka stop uske child crawl tak forward hota hai — turant, ya ek side watcher (`_forward_stop_to_crawl`) ke through |
| **`derived` block** | Analysis report payload ka cross-check section (duplicate titles, thin pages, canonical groups). **Abhi sirf <=8 example rows se banta hai** — dekho [report pipeline §9](../Pipelines/report-engine-pipeline.md) |

---

## Scoring

| Term | Matlab |
|---|---|
| **Finding** | Ek check ka result: `id`, `severity`, `title`, `description`, `recommendation`, `impact` (1–5), `effort` (1–5), `evidence` |
| **Severity** | `critical` \| `warning` \| `info` \| `passed` |
| **Deduction** | `score_from_findings` mein: critical = 20, warning = 8, info/passed = 0 |
| **Weighted score** | Analyzer scores ka weighted mean. **`score=None` wale EXCLUDE hote hain** — zero nahi gine jaate. Har analyzer ka weight 1.0 |
| **Grade** | A >= 90, B >= 80, C >= 70, D >= 60, warna F |
| **Prioritized fixes** | Har non-passed finding; sort: severity, phir impact DESC, phir effort ASC; cap 10 |
| **Skipped analyzer** | `score=None` + ek explanatory finding. Category se exclude ho jaata hai |

---

## Report engines

| Term | Matlab |
|---|---|
| **Engine** | Kaun report ka Markdown likhta hai: `gemini` (hosted) \| `ollama` (local model server) \| `claude` (server par logged-in Claude Code CLI) |
| **`TextGenerator`** | Protocol jo har engine satisfy karta hai: `async generate_text(*, prompt, system_instruction) -> str` |
| **Fallback chain** | `GEMINI_FALLBACK_MODELS` — primary persistently overloaded hone par in par fall back |
| **`readyReport`** | Frontend ka state: generate hui report jo page plain Download/Open links ke roop mein rakhta hai, kyunki minutes-later download browser prompt se mil sakta hai ya notice hi na ho |
| **`num_ctx` grow** | Ollama client prompt fit karne ke liye per-call context window raise karta hai. Default 4096 window prompt ko **shuru se** truncate karti hai |

---

## Infra

| Term | Matlab |
|---|---|
| **Layering rule** | `route → controller → service`. **Sirf services** `integrations/` aur `db/` touch karti hain |
| **Best-effort write** | Mongo write jo error par log karke 0 return karti hai, raise nahi. Job phir `persistence_ok=false` set karta hai |
| **`persistence_ok`** | `false` = job chala par data save nahi hua (Mongo down tha) |
| **SSRF guard** | `is_public_host()` — private/internal addresses ko crawl hone se rokta hai |
| **Literal-before-parameter** | Route declaration rule: `/api/stores/crawls` ko `/api/stores/{store_domain}` se **pehle** declare karna, warna `"crawls"` ek store domain ban jaata hai |
| **Ephemeral session secret** | `SESSION_SECRET` blank hone par generate hota hai. App boot hota hai, par connections restart par toot jaate hain |
| **`extra="ignore"`** | pydantic-settings ki setting jiski wajah se ek typo `.env` key chupchaap ignore hoti hai — isliye `unknown_env_keys()` exist karta hai |
