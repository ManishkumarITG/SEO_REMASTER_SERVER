# Flows — code ka flow, file by file

Ye folder ek hi sawaal ka jawab deta hai: **"jab main ye API call karta hoon, to andar
kya hota hai?"** — kaun si file, kaun sa function, kis order mein, aur kya read/write
hota hai.

`Pipelines/` contract aur budget batata hai (kya bhejo, kya milega, limits kya hain).
`Flows/` **call chain** batata hai (kaun kisko call karta hai).

---

## Index

| # | File | Kis endpoint ka flow |
|---|---|---|
| 00 | [request-lifecycle](00-request-lifecycle.md) | **Har** request ka common safar — middleware se service tak. Pehle yahi padho. |
| 01 | [audit-flow](01-audit-flow.md) | `POST /api/audits`, `GET /api/audits/{id}` |
| 02 | [site-crawl-flow](02-site-crawl-flow.md) | `/api/site-crawls/*` |
| 03 | [site-analysis-flow](03-site-analysis-flow.md) | `/api/analyses/*` |
| 04 | [store-catalog-flow](04-store-catalog-flow.md) | `/api/stores/*` |
| 05 | [google-gsc-flow](05-google-gsc-flow.md) | `/auth/*`, `/api/sites`, `/api/search-analytics`, `/api/gsc/*` |
| 06 | [reddit-backlink-flow](06-reddit-backlink-flow.md) | `/auth/reddit/*`, `/api/backlinks/reddit/*` |
| 07 | [report-and-export-flow](07-report-and-export-flow.md) | `report.html?engine=`, `export.{csv,json}` |
| 08 | [background-jobs-flow](08-background-jobs-flow.md) | `workers/tasks.py` ke chaaron jobs, reconcile aur reap |

---

## Har flow page ka same structure

Taaki aap ek page padhne ke baad baaki sab mein navigate kar sakein, har page ye order
follow karta hai:

1. **Ek line mein** — endpoint kya karta hai
2. **Call chain** — file-by-file, function-by-function (table + Mermaid)
3. **Kya read / write hota hai** — SQL, Mongo, bahar ki calls
4. **Background mein kya hota hai** (agar job hai)
5. **Terminal states aur error codes**
6. **Chhedne se pehle** — gotchas jo code mein likhe hain

---

## Pattern jo har flow mein repeat hota hai

### Thin route, thinner controller

```
routes/<domain>.py        ← sirf path, status code, Pydantic validation, Depends
controller/<domain>.py    ← sirf pass-through (ek line ka function)
services/<domain>.py      ← saara business logic, HTTPException yahin se uthta hai
```

Controller deliberately khaali hai. Agar aap controller mein `if` likhne ja rahe ho,
wo logic service mein jaani chahiye.

### Long job = job row + background task

Jo kaam ek second se zyada le, wo yeh shape follow karta hai:

```
POST  → validate → SQL INSERT (status="pending") → schedule background task → 202 {id}
GET   → poll {id} tab tak jab tak status terminal na ho
```

Do generations saath-saath chalti hain — detail [`08-background-jobs-flow.md`](08-background-jobs-flow.md) mein:

| Model | Kaun | Kaise |
|---|---|---|
| **A. Detached asyncio task** | site crawl, full analysis | `schedule_*(id)` — client disconnect hone par bhi zinda; heartbeat; stop support |
| **B. FastAPI `BackgroundTasks`** | page audit, store catalog crawl | response ke baad web process ke andar; semaphore; na heartbeat, na stop |

### Downloads on demand

`export.csv|json` aur `report.html` **request hone par** banti hain, kabhi store nahi
hoti. Dono `Content-Disposition` + `Access-Control-Expose-Headers` set karti hain taaki
cross-origin frontend filename padh sake.
