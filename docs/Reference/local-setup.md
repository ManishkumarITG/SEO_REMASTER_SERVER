# Local setup aur commands

---

## 1. Pehli baar setup

```bash
cd server

# Virtualenv (project apna .venv use karta hai)
python -m venv .venv
.venv/Scripts/activate            # Windows (Git Bash)
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt

# Settings
cp .env.example .env
# .env kholo aur jo features chahiye unki keys bharo
```

> **Hamesha project venv use karo** — `server/.venv/Scripts/python.exe`. System Python
> mein dependencies nahi hain.

Kis feature ke liye kaun si key chahiye:
[configuration.md § Minimum `.env`](configuration.md).

---

## 2. Server chalana

### Production / "bas chal jaaye"

```bash
python main.py
```

Kisi bhi cwd se chalta hai. Settings se `HOST:PORT` bind karta hai. App **object**
directly run hota hai (string `"main:app"` nahi), to koi reloader/worker subprocess
alag cwd ke saath import-by-name nahi karta.

### Dev, auto-reload ke saath

```bash
cd server
uvicorn main:app --reload
```

| Caveat | Matlab |
|---|---|
| **`server/` se hi chalao** | Har import top-level hai (`from routes...`) |
| `HOST`/`PORT` **ignore** hote hain | `--host`/`--port` flags do |
| Har file save **worker restart** karta hai | **Har in-process background job mar jaata hai** |

Reload ke baad: Model A rows (site crawl, analysis) reap-on-read se close hoti hain;
Model B rows (audit, store crawl) agle startup par.

### Explicit app-dir ke saath

```bash
uvicorn --app-dir /path/to/server main:app --host 0.0.0.0 --port 8000
```

---

## 3. Startup logs padhna

Healthy boot aisa dikhta hai:

```
INFO  Added missing column site_crawls.heartbeat_at to the dev database   # pehli baar
INFO  Application startup complete.
```

**Ye warnings ignore mat karo:**

```
WARNING Ignoring 2 unrecognised key(s) in server/.env: GIMINI_API_KEY, REDDIT_SECERT -
        these match no setting in config/settings.py and have no effect.

WARNING 1 setting(s) in server/.env still hold template placeholders:
        GOOGLE_REDIRECT_URI - the feature each one configures will fail until it is
        filled in.

WARNING SESSION_SECRET is not set - using an ephemeral session secret.
        Google Search Console connections will not survive a server restart.
```

---

## 4. Tests

```bash
cd server
pytest
```

`pytest` **`server/` se hi** chalana hai — `conftest.py` `server/` ko `sys.path` par
daalta hai, aur suite ke saare imports top-level hain (`from services...`, `import main`).

Config `pyproject.toml` mein:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"                            # async tests ko marker nahi chahiye
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

### Kuch useful invocations

```bash
pytest tests/test_site_crawl.py              # ek file
pytest -k "orphan"                           # naam se match
pytest -x                                    # pehli failure par ruko
pytest -q                                    # quiet
pytest --lf                                  # sirf last-failed
```

### Test suite kaise organised hai

| Pattern | Kya |
|---|---|
| `test_*_routes.py` | HTTP layer (`TestClient`) |
| `test_*_worker.py` | Background jobs |
| `test_crawler_*.py`, `test_site_crawl*.py`, `test_robots.py`, `test_fetcher_limits.py` | Crawler engine |
| `test_on_page.py`, `test_technical.py`, `test_scoring.py`, `test_registry_and_content_ai.py`, `test_search_console_analyzer.py` | Analyzers + scoring |
| `test_store_*.py` | Store catalog |
| `test_reddit_*.py` | Reddit client + draft generator |
| `test_ollama_client.py`, `test_claude_cli_client.py` | Report engines |
| `test_mongo_store.py`, `test_store_catalog_mongo.py` | Mongo layer |
| `factories.py` | Test data builders |

HTTP mocking `respx` se hoti hai.

> **Tests mein koi `user_id` nahi hota** (`TestClient` ke paas session nahi hota),
> isliye credential-dependent seams (jaise `gsc_connected(user_id)`) monkeypatch kiye
> jaate hain.

---

## 5. Lint

```bash
cd server
ruff check .
ruff check --fix .
ruff format .
```

Config `pyproject.toml`:

```toml
[tool.ruff]
line-length = 110
target-version = "py312"

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "UP", "B"]
ignore = ["B008"]   # FastAPI DI defaults mein function calls use karta hai
```

---

## 6. Optional dependencies

| Chahiye | Install |
|---|---|
| Redis cache (`REDIS_URL` set karne par) | `pip install redis>=5.0` |
| Store crawler ke liye HTTP/2 (`CRAWL_HTTP2=true`) | `pip install "httpx[http2]"` |

Dono optional hain — `requirements.txt` mein commented out hain. `h2` na ho to crawler
chupchaap HTTP/1.1 par fall back karta hai.

---

## 7. Optional services

### MongoDB

```bash
# Docker
docker run -d -p 27017:27017 --name seo-mongo mongo:7
```

Mongo **optional** hai. Uske bina API chalta hai par crawl pages, template reports,
catalog items aur GSC mirror save nahi honge (`persistence_ok: false`).

Check: `GET /api/health` → `{"mongodb": "connected"}`.

### Ollama (local report engine)

```bash
ollama pull qwen3:4b
ollama serve          # default http://localhost:11434
```

> CPU-only par qwen3:4b ~9 tok/s deta hai: crawl report ~6 min, full-analysis report
> 10–15 min. Isliye `OLLAMA_TIMEOUT_S = 1800`.

### Claude CLI (report engine)

```bash
claude            # ek baar chalao aur log in karo
```

Koi API key nahi chahiye — report us account ki subscription par likhi jaati hai jo
server par logged in hai.

> `CLAUDE_CLI_EFFORT=max` **mat** set karo — ek crawl report 15 minute mein finish nahi
> hua. `high` use karo ya blank chhod do.

---

## 8. Database reset (dev)

```bash
cd server
rm seo.db          # server band hone par
python main.py     # init_db() tables dobara bana dega
```

Mongo data alag se:

```bash
mongosh seo_remaster --eval "db.dropDatabase()"
```

> **Koi Alembic nahi hai.** Existing table mein naya column add karo to use **nullable**
> rakho — `db/session.py::_add_missing_columns` sirf nullable/server-default columns add
> kar sakta hai (aur sirf SQLite par).

---

## 9. Quick health check

```bash
curl -s http://localhost:8000/api/health | python -m json.tool
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "mongodb": "connected",
  "reddit": "configured",
  "gemini": "configured"
}
```

**Hamesha 200.** `mongodb` ek live ping hai (~1.5 s agar unreachable). `reddit`/`gemini`
sirf key presence batate hain.

> Ye SQL, Redis/cache, PageSpeed, Anthropic, Google OAuth config, Ollama ya Claude CLI
> **check nahi karta**.

---

## 10. Frontend ke saath chalana

```bash
# Terminal 1
cd server && python main.py            # :8000

# Terminal 2
cd frontend-design && npm run dev      # :5173
```

Frontend ka `.env`:

```
VITE_API_URL=http://localhost:8000
```

CORS pehle se allow karta hai: `CORS_ORIGINS` (default `http://localhost:5173`) +
`FRONTEND_URL` + `CORS_ORIGIN_REGEX` (koi bhi localhost/LAN origin, koi bhi port).

Frontend setup: [../../../frontend-design/docs/Reference/local-setup.md](../../../frontend-design/docs/Reference/local-setup.md)

---

## 11. Common problems

| Problem | Wajah / fix |
|---|---|
| `ModuleNotFoundError: No module named 'routes'` | `server/` se nahi chala rahe. `cd server` karo, ya `--app-dir` do |
| `ImportError` pytest par | `pytest` ko `server/` se chalao |
| "unable to open database file" | `DATABASE_URL` ka parent directory exist nahi karta. Error message exact path batata hai. **Directory deliberately auto-create nahi hoti** |
| "no such column" | Naya column NOT NULL add hua. Nullable karo, ya dev DB recreate karo |
| Restart par connections gayab | `SESSION_SECRET` set karo |
| Google `redirect_uri_mismatch` | `GOOGLE_REDIRECT_URI` Google Cloud Console se exactly match hona chahiye |
| Crawl `failed`, `pages_fetched: 0` | robots disallow ya probe fail — `error` field padho |
| `persistence_ok: false` | MongoDB down tha |
| Reload ke baad crawl ruk gaya | `--reload` har save par worker kill karta hai. Lambe crawls ke liye `python main.py` use karo |
| "database is locked" | Kisi worker ne network phase ke across SQL session hold kiya |
| Reddit calls throttle ho rahi hain | `REDDIT_USER_AGENT` mein asli username daalo |
