# Backend docs — SEO Remaster server

Ye `server/` ka documentation root hai. Sab kuch **Hinglish** mein hai aur **code ke
against** likha gaya hai, yaad se nahi — har number, constant aur function name source
se uthaya gaya hai, aur uski file+symbol ka naam diya gaya hai taaki aap is doc par
bharosa karne ke bajaye seedha grep kar sakein.

Stack: **FastAPI + SQLAlchemy (async) + MongoDB + asyncio background jobs**.

---

## Folder ka naksha

| Folder | Kya milega | Kab padhein |
|---|---|---|
| [`Flows/`](Flows/) | **Code flow**: jab aap ek API call karte ho to kaun si file ka kaun sa function, kis order mein chalta hai | "Ye endpoint andar se kya karta hai?" |
| [`Pipelines/`](Pipelines/) | **API pipeline**: request shape, phases, states, limits, error codes — har domain ka ek page | "Is API ka contract aur budget kya hai?" |
| [`Reference/`](Reference/) | File map, data model, configuration, error codes, setup, glossary | "Ye cheez kahan rehti hai / kaise chalaun?" |
| [`crawler.md`](crawler.md) | Site-wide crawler ka deep-dive behaviour | Crawler engine chhedne se pehle |

Bahar ke do companion documents:

- [`../../docs/api-pipelines.md`](../../docs/api-pipelines.md) — poore backend ka ek-file
  master reference (972 lines). `Pipelines/` uske hi topics ko chhote, focused pages mein
  todta hai; jab doubt ho, master wala hi final hai.
- [`../../frontend-design/docs/`](../../frontend-design/docs/) — frontend ki docs. Har
  backend endpoint ka consumer wahan `Reference/api-contract-map.md` mein mapped hai.

---

## Shuruaat kahan se karein

**Agar aap naye ho** — is order mein:

1. [`Flows/00-request-lifecycle.md`](Flows/00-request-lifecycle.md) — ek request ka poora safar
2. [`Pipelines/endpoint-index.md`](Pipelines/endpoint-index.md) — poora endpoint surface ek table mein
3. [`Reference/file-map.md`](Reference/file-map.md) — har folder/file kya karti hai
4. Jis feature par kaam karna hai, uska `Flows/` page

**Agar ek specific bug dhoond rahe ho** — endpoint index se apna endpoint dhoondo, uska
`Flows/` page padho (wo exact call chain deta hai), phir code mein jao.

**Agar ek naya endpoint add kar rahe ho** — [`Flows/00-request-lifecycle.md`](Flows/00-request-lifecycle.md)
ka "Naya endpoint add karne ka checklist" section.

---

## Ek line mein architecture

```
Browser ──► middleware ──► routes/ ──► controller/ ──► services/ ──► integrations/  ──► Bahar ki duniya
                                                            │                             (Google, Reddit, Gemini,
                                                            ├──► db/ (SQL)                  Ollama, Claude CLI, sites)
                                                            ├──► db/ (MongoDB)
                                                            └──► workers/tasks.py (background jobs)
```

**Layering rule (non-negotiable):** har request `route → controller → service` se hi
guzarti hai. Sirf **services** hi `integrations/` aur `db/` ko touch karti hain.
Controllers mein koi business logic nahi hoti — wo sirf pass-through hain.

**Do stores, jaan-boojh kar:**

- **SQL** (dev mein SQLite `server/seo.db`) — jo bhi *pollable aur consistent* hona
  chahiye: job rows, credentials, counters.
- **MongoDB** — jo *bulk* data ek job produce karta hai: crawled pages, Lighthouse
  template reports, GSC rows, catalog items. Mongo **writes best-effort** hain (log
  karti hain, raise nahi), **reads** 503 par propagate hoti hain.

---

## Doc likhne ke rules (taaki ye sadte nahi)

1. Har claim ke saath **file + symbol** ka naam do (`workers/tasks.py::run_site_crawl`).
2. Numbers hamesha **constant ke naam ke saath** likho (`MAX_CONCURRENT_AUDITS = 4`),
   kyunki number badal sakta hai par naam grep-able rehta hai.
3. Diagrams **Mermaid** mein (GitHub, VS Code aur zyadatar viewers render karte hain).
4. Kuch verify na ho paaye to likh do ki verify nahi hua — galat certainty sabse mehnga
   documentation bug hai.
