# The crawler

Site-wide crawler kaise behave karta hai, use kya cheezein bound karti hain, aur har
decision code mein kahan rehta hai. Ye document code ke against likha gaya hai,
yaad se nahi: neeche ka har number likhte waqt source mein jo value thi wahi hai,
aur har ek ka naam diya gaya hai taaki aap is file par bharosa karne ke bajaye use
grep kar sakein.

(English original: `server/docs/en/crawler.md`)

---

## 1. Teen crawl shapes, jaan-boojh kar alag rakhe gaye

Service layer mein teen alag cheezein hain jo sab "crawl" karti hain, aur ye ek
doosre ke variants nahi hain. Aap kaunsi wali dekh rahe hain ye jaanna hi zyadatar
confusion rok deta hai.

| Shape | Entry point | Kya walk karta hai | Frontier |
|---|---|---|---|
| Single page | `services/crawler/fetcher.py::fetch_page` | Ek URL jo user ne type kiya | Koi nahi |
| Store catalog | `services/stores/` | Ek Shopify catalog, `products.json` ya sitemap se | Pehle se pata |
| **Site-wide** | `services/crawler/site_crawl.py::crawl_site` | Poori site, links follow karte hue | **Chalte-chalte badhta hai** |

Ye document teesre wale ke baare mein hai. Badhta hua frontier hi ise ek alag
problem banata hai: workers ko un URLs ka wait karna padta hai jo doosre workers ne
abhi produce hi nahi kiye, aur crawl tabhi khatam hota hai jab frontier khaali ho
*aur* koi worker page ke beech mein na ho. Yahi `asyncio.Queue.join()` hai, isliye
site crawl `task_done()` wali queue use karta hai, na ki
`services/stores/enrichment.py` wala fixed worker-pool-plus-sentinels pattern.

Crawl shape ke neeche ki har cheez jaan-boojh kar shared hai, taaki politeness ka
ek fix ek saath sab jagah lage:

```
services/crawler/
  site_crawl.py    the site-wide engine, the pre-flight probe, page facts
  urls.py          URL normalisation, link extraction, catalog classification
  models.py        CrawledPage, CrawlLimits, CrawlStats, SiteCrawlResult
  http_client.py   pooled client, capped reads, SSRF guard, robots fetch
  throttle.py      HostGate: per-host pacing, back-off, recovery
  robots.py        robots.txt parsing and wildcard path matching
  sitemap.py       <loc> extraction from sitemap documents
  parser.py        PageData and shared DOM lookups
  fetcher.py       the single-page fetcher the other two are built beside
```

---

## 2. Request ka lifecycle

```
POST /api/site-crawls
  │
  ├─ validate + normalise the URL            schemas/site_crawl.py
  ├─ reuse an in-flight crawl of this host?  services/site_crawl_service.py
  ├─ probe_site: is it reachable and allowed? (2 requests)
  │     unreachable      -> 422
  │     robots.txt Disallow: /  -> 403
  ├─ INSERT site_crawls row, COMMIT
  └─ schedule_site_crawl(row.id)             workers/tasks.py
        │
        └─ asyncio task, detached from this request
              ├─ probe again (idempotent; only the id crossed the boundary)
              ├─ crawl_site(...) yields CrawledPage objects
              │     each page: fetch -> parse -> facts -> links -> analyzers
              ├─ every 25 pages / 2s: flush counters + heartbeat to SQL,
              │                        bulk-write pages to MongoDB
              └─ on completion: mark orphans, write the final row

GET /api/site-crawls/{id}          poll status and progress
GET /api/site-crawls/{id}/issues   per-issue counts + orphan list
GET /api/site-crawls/{id}/pages    the page list, filtered and sorted
GET /api/site-crawls/{id}/page     one page, with inlinks and outlinks
POST /api/site-crawls/{id}/stop    ask it to finish now
```

**Ye task FastAPI ka `BackgroundTask` nahi hai.** Wo response ke andar chalte hain
aur client connection jaate hi cancel ho jaate hain, jisse lambe crawls chupchaap
truncate ho jaate the. `schedule_site_crawl` `asyncio.create_task` use karta hai
aur `_running_site_crawls` mein ek strong reference rakhta hai, kyunki asyncio sirf
weak reference rakhta hai aur warna ek crawl beech run mein garbage collect ho
sakta tha.

---

## 3. Crawl ko kya bound karta hai

Har exit explicit hai aur `CrawlStats` mein record hota hai. Kisi bound tak
pahunchna ek normal outcome hai, exception kabhi nahi: imaandaar reason ke saath
ek partial crawl, raised error se zyada kaam ka hai.

| Bound | Default | Setting / field | Kis naam se record hota hai |
|---|---|---|---|
| Fetch kiye gaye pages | 500 | `SITE_CRAWL_MAX_PAGES`, `CrawlLimits.max_pages` | `hit_page_budget` |
| Seed se link depth | 5 | `SITE_CRAWL_MAX_DEPTH` | `beyond_depth` |
| Wall clock | 1800s | `SITE_CRAWL_DEADLINE_S` | `deadline_hit` |
| Navigation detour | 100 pages | `CrawlLimits.max_navigation_pages` | `hit_navigation_budget` |
| Bytes per page | 2 MiB | `MAX_PAGE_BYTES` | page par `truncated` |
| Koi insaan Stop dabaye | — | `POST .../stop` | `stopped_by_user` |

**`max_pages = 0` ka matlab hai koi page limit nahi.** Zero hi ek-matr special
value hai; baaki har number ek literal ceiling hai, aur negative value galti se
unlimited banne ke bajaye zero par floor ho jaati hai. Bina page limit ke sirf
deadline aur depth limit bache rehte hain, isliye in dono ko API se off nahi kiya
ja sakta. Unlimited case ke liye `CrawlLimits.page_budget` `None` return karta hai
taaki callers galti se `0` ke against compare na kar sakein.

Requests ko edge par bhi cap kiya jaata hai, `schemas/site_crawl.py` mein:
`MAX_REQUESTABLE_PAGES = 5000`, `MAX_REQUESTABLE_DEPTH = 10`. Ye numbers tay karte
hain ki hum kisi aur ko kitna traffic bhejenge, isliye inhe teen layer andar
chupchaap clamp karne ke bajaye 422 ke saath reject kiya jaata hai.

**Rate limiting koi bound NAHI hai.** Section 5 dekhein.

---

## 4. Politeness

Crawl ek third party ko sainkdon unattended requests bhejta hai. Yahan ka har
control isliye hai kyunki use ignore karne se hi ek IP block list par pahunchta hai.

- **Identity.** `SEOAnalyzerBot/0.1 (+https://github.com/seo-remaster/bot)`. Ek
  imaandaar bot UA jisme contact URL ho, kabhi spoofed browser nahi. Kai stores
  named SEO bots ko khaas taur par throttle karte hain; use dodge karne ke liye
  browser ka roop dharna terms breach hai.
- **robots.txt** har crawl mein ek baar fetch hota hai aur honour kiya jaata hai,
  wildcards aur `$` anchors samet (`robots.py::path_allowed`, longest match jeetta
  hai, tie par `Allow` `Disallow` ko haraata hai). Hamare apne token ka naam lene
  wala group `*` ko haraata hai. Blanket `Disallow: /` ko POST par hi 403 ke saath
  refuse kar diya jaata hai.
- **Matched path mein query string shaamil hai** (`robots_target`). Shopify
  `Disallow: /*?*oseid=*` ship karta hai, aur sirf bare path match karne se wo har
  URL allow ho jaata jise exclude karne ke liye aisa rule bana hai.
- **`Crawl-delay`** requests ke beech floor ke roop mein use hota hai, 2s par
  clamped, taaki koi hostile value ek job ko ghanton tak atka na sake.
- **`HostGate`** poori requests ke bajaye request *starts* ko serialise karta hai,
  isliye `concurrency` workers in-flight rehte hue bhi rate ceiling bani rehti hai.
  Site crawl ke liye default floor 0.1s hai; catalog crawl 0.06s use karta hai
  kyunki ek `<head>` read poore page se halka hota hai.
- **SSRF.** Seed host ko `is_public_host` se check kiya jaata hai (loopback,
  private, link-local, reserved, multicast reject). Frontier mein aane wala har URL
  same-site hona chahiye, taaki target ke control wala koi document crawler ko
  kisi naye host par na mod sake. httpx redirects ko phir bhi kahin bhi follow
  karta hai, isliye links follow karne se pehle *final* URL par same-site rule
  dobara lagaya jaata hai.

---

## 5. Rate limiting: wait karo, give up mat karo

429 ka matlab hai "bahut tez", "chale jao" nahi. Truncated crawls ka ye sabse bada
single source tha, aur handling jaan-boojh kar patient rakhi gayi hai.

1. Page ko **retry** kiya jaata hai, `MAX_THROTTLE_RETRIES = 4` attempts tak.
2. Jab host numeric `Retry-After` bhejta hai to use honour kiya jaata hai,
   `MAX_RETRY_AFTER_S = 60` par capped. Date-format wala header guess karne ke
   bajaye ignore kiya jaata hai.
3. `HostGate.back_off()` requests ke beech ka interval chaar guna kar deta hai,
   site crawl ke liye `THROTTLED_MAX_INTERVAL_S = 30s` tak. Catalog crawl apni 2s
   ceiling rakhta hai: ceiling per gate hai, global nahi.
4. `HostGate.recover()` har us response par interval ko wapas floor ki taraf
   (factor 0.8) laata hai jisse host khush tha, taaki lambe crawl mein shuru ka ek
   429 baad ke har page ko slow na kare.
5. Agar page phir bhi nahi padha ja sakta, to **wo page** unreachable record hota
   hai (`gave_up_throttled`) aur crawl chalta rehta hai.

`503` ko bhi throttling maana jaata hai, server error nahi, kyunki practice mein
rate limiter se aksar yahi matlab hota hai.

Counters: `stats.throttled` (mile hue responses) aur `stats.gave_up_throttled`
(aakhir mein chhode gaye pages). Dono API tak pahunchte hain, taaki report keh
sake "aapke 300 mein se 280 pages mile, baaki rate-limited the".

---

## 6. URL rules

Dedupe decisions hi crawl ko terminate karwate hain, isliye ye **enqueue** time par,
`seen` se pehle lagte hain, report time par kabhi nahi.

**Normalisation** (`urls.py::normalize_url`):

- Fragments drop. `#anything` response body nahi badal sakta.
- 33 jaane-maane tracking parameters drop (`utm_*`, `gclid`, `fbclid`, `msclkid`,
  `mc_cid`, …). Iske bina ek marketing site apna poora page budget ek hi page par
  chaalis campaign tags pehne hue kharch kar deti hai.
- Bache hue query parameters **sort** hote hain, taaki `?a=1&b=2` aur `?b=2&a=1`
  `seen` mein ek hi entry hon.
- Scheme aur host lower-case; default ports hata diye, non-default rakhe.
- Khaali path `/` ban jaata hai.
- **Trailing slashes preserve hote hain.** `/a` aur `/a/` sach mein alag documents
  ho sakte hain, isliye unhe collapse karna under-report karega.
- Non-http(s) schemes, malformed URLs aur invalid characters wale hosts `None`
  return karte hain, jise callers "ignore" maante hain, error kabhi nahi.

`normalize_seed_url` us URL ke liye alag variant hai jo *insaan ne type kiya*:
scheme bilkul na ho to ye `https` maan leta hai. Ise jaan-boojh kar link extraction
ke liye use nahi kiya jaata, jise kisi `href` ke liye kabhi scheme invent nahi
karna chahiye.

**Bilkul fetch nahi hote:** 51 asset extensions (images, video, archives, fonts,
scripts, documents). Content-type check inhe waise bhi pakad leta, lekin 40 MB ka
video ek bekaar request hai chahe body discard hi kyun na ho.

**Link extraction** `<base href>` ko honour karta hai, internal ko external se
alag karta hai, `rel="nofollow"` alag se ginta hai, aur rakhe gaye links ko
`MAX_LINKS_PER_PAGE = 300` par cap karta hai — nav-heavy template har page par
wahi header aur footer list karta hai, aur tail ki koi graph value nahi hoti.

---

## 7. Page types: products, collections, ya dono

`CrawlLimits.page_types` crawl ko catalog page types tak restrict karta hai. Khaali
ka matlab har page, jo default aur original behaviour hai.

Classification **sirf URL path se** hoti hai (`urls.py::classify_catalog_url`),
isi wajah se filter sasta hai: crawl request kharch karne se *pehle* tay kar leta
hai ki use page chahiye ya nahi. `/products/<handle>` product hai,
`/collections/<handle>` collection hai, aur Shopify ke virtual `/collections/all`
aur `/collections/frontpage` dono mein se kuch nahi. Rule us convention wale kisi
bhi store par kaam karta hai, sirf Shopify par nahi.

Filtered crawl ko phir bhi apne chahe pages tak *pahunchna* padta hai — store ka
home page aur collection listings hi product pages tak ka ek-matr raasta hain.
Isliye:

- Filter se match hone wala URL **target** hai: fetch, analyze, store hota hai aur
  `max_pages` mein gina jaata hai.
- Filter ke bahar wala URL **navigation** hai: sirf uske links ke liye fetch, analyze
  nahi hota, aur alag `max_navigation_pages` budget mein charge hota hai taaki wo
  page budget kabhi na khaaye.
- Navigation budget khatam hona *detour* khatam karta hai, crawl nahi.

`CrawledPage.is_target` record karta hai ki page in dono mein se kya tha. Reads
default mein navigation pages exclude karte hain (`include_navigation=true` se opt
in), aur filter `{"is_target": {"$ne": False}}` use karta hai taaki field ke aane se
pehle likhe gaye documents ab bhi targets ginein, jo wo the hi.

Skip kiya gaya URL bhi classify hota hai. Robots-blocked *product* page phir bhi
products-only report ka hissa hai, kyunki humne use maanga tha aur mil nahi saka;
robots-blocked account page nahi hai.

---

## 8. Sitemap se seeding

Default mein on (`CrawlLimits.seed_from_sitemap`). `/sitemap.xml` plus robots.txt
ki har `Sitemap:` line, nested indexes ek level tak follow karte hue, zyada se
zyada `MAX_SITEMAP_FETCHES = 10` documents.

Yahi cheez **orphan detection** ko possible banati hai: sitemap mein listed jo page
kisi crawled page se link nahi hota, us tak koi internal raasta nahi hai.

Do baatein jaanne layak:

- Sitemap XML ko `<loc>` par **bounded regex** se parse kiya jaata hai, XML parser
  se kabhi nahi. Document user-supplied host se aata hai, aur CPython 3.12 par XXE
  reachable nahi hai lekin entity-expansion DoS hai. Humein bilkul ek cheez chahiye,
  `<loc>` ka text, isliye ye untrusted document se token extraction hai, parsing
  nahi.
- `looks_like_sitemap` body check karta hai, kyunki kuch hosts `/sitemap.xml` ka
  jawab HTTP 200 aur apne themed not-found page se dete hain. python.org bilkul
  yahi karta hai.

**Seed cap.** Page budget ka do guna, 100 par floored — ya `MAX_SITEMAP_SEEDS =
25000` jab derive karne ke liye koi page budget hi na ho. Note *"sitemap seeding
stopped at N URLs"* ka matlab hai wo cap pahunch gaya; ye information hai, error
nahi.

---

## 9. Page analysis

Analyzers crawl ke **andar** chalte hain, `page_hook` seam ke through, kyunki DOM
sirf tab tak exist karta hai jab tak page facts mein reduce ho raha hota hai. Baad
mein score karne ka matlab hota poori site do baar fetch karna.

Sirf wahi analyzers bulk mein safe hain jo unhe di gayi `PageData` padhte hain:

| Analyzer | Bulk-safe | Kyun |
|---|---|---|
| `on_page` | haan | titles, descriptions, headings, images, links — ~1 ms |
| `technical` | haan | robots, canonical, viewport, status, timing — ~1 ms |
| `pagespeed` | nahi | har page par ek Lighthouse run |
| `content_ai` | nahi | har page par ek LLM completion |
| `search_console` | nahi | har page par ek Google API query |

`services/site_crawl_analysis.py::BULK_SAFE` ise enforce karta hai: set ke bahar
configure kiya gaya naam chupchaap API bill badhaane ke bajaye ek log line ke saath
refuse hota hai. `SITE_CRAWL_ANALYZERS` se configure karein; khaali value scoring
off kar deti hai, jo har page ke zero score karne se alag hai (`avg_score` tab
`null` hota hai).

Har page ko ek weighted `score`, ek `grade`, per-analyzer scores, aur zyada se
zyada `MAX_FINDINGS_PER_PAGE = 25` non-passed findings milti hain, sabse urgent
pehle. Passed checks store nahi hote — score wo khabar pehle hi carry karta hai.
Navigation pages kabhi score nahi hote.

---

## 10. Kya store hota hai

**SQL (`site_crawls`)** job state rakhta hai, taaki client hamesha use poll kar
sake. Har counter ek hi row mein aata hai: progress, category-wise skip reasons,
kyun ruka, headline findings, aur analysis roll-up.

> Table ke pehli baar banne ke baad add kiye gaye columns **nullable** hain aur
> `row.x or 0` ki tarah padhe jaate hain. `db/session.py::_add_missing_columns`
> sirf wahi column `ALTER TABLE ADD` kar sakta hai jo nullable ho ya server default
> carry karta ho, aur `Integer, nullable=False, default=0` ek Python-side default
> hai jo qualify nahi karta. Ye do baar kaat chuka hai; counter add karne se pehle
> ise check karein.

**MongoDB (`site_crawl_pages`)** har `(crawl_id, url)` ke liye ek document rakhta
hai. Jaan-boojh kar host ke bajaye crawl se keyed: site crawl ek snapshot hai, aur do
rakhne ki value unhe compare karne mein hai. Writes best-effort hain — failure log
hota hai aur `persistence_ok` ke roop mein surface hota hai, client tak kabhi raise
nahi hota. Crawl phir bhi chala aur uske counters SQL mein hain.

HTML **kabhi** retain nahi hota. `services/stores/enrichment.py` ne ye mehenga
sabak seekha: 5,000 pages x 90 KiB markup = 450 MB. Page parse hote hi facts mein
reduce ho jaata hai aur markup drop kar diya jaata hai.

Reads 17 issue filters (`broken`, `orphan`, `missing_title`, `thin`, `critical`,
`product`, …) aur 6 sort orders (`depth`, `url`, `status`, `words`, `links`,
`score`) support karte hain. Dono `db/site_pages.py` mein ek baar define hain taaki
route, counters aur export ek doosre se alag na ho jaayein.

---

## 11. Orphan detection

Aakhir mein ek baar compute hota hai, kyunki pehle crawl hua page aakhir mein crawl
hue page se linked ho sakta hai.

Worker dekhe gaye har outlink ka ek set aur fetch kiye gaye har page ki ek list
rakhta hai; orphans in dono ka difference hain, minus seed. Memory unique URLs ke
do sets hai, edges ki list nahi — edge count pages x links hota hai, URL count site
hoti hai. Result `mark_orphans` se wapas likha jaata hai, har query mein 500 ke
chunks mein, kyunki das hazaar URLs wala `$in` megabytes chauda query document
hota hai.

Seed kabhi orphan nahi hota. Skip kiya gaya URL bhi kabhi orphan nahi hota: wo kabhi
fetch hi nahi hua, isliye "usse koi link nahi karta" uske baare mein kuch nahi kehta.

---

## 12. Statuses, stop karna, aur interruption

| Status | Matlab |
|---|---|
| `pending` | row ban gayi, worker abhi shuru nahi hua |
| `crawling` | chal raha hai |
| `complete` | har bound ke andar site ke end tak pahunch gaya |
| `partial` | koi bound pahunch gaya, ya kuch pages fail hue — ek imaandaar jawab, failure nahi |
| `stopped` | kisi insaan ne Stop dabaya |
| `failed` | kuch bhi fetch nahi hua, ya crawl shuru hi nahi ho saka |

**Stop cooperative hai.** `POST .../stop` ek flag set karta hai jise `crawl_site`
pages ke beech check karta hai; crawl phir apne normal completion path se exit
karta hai. Iske bajaye task cancel karne se wo pages kho jaate jo fetch to ho gaye
lekin abhi likhe nahi gaye. Jo page pehle se in flight hai wo phir bhi poora hota
hai, isliye stop turant nahi, ek-do page ke andar lagta hai.

**Heartbeats.** Worker har progress flush par `heartbeat_at` likhta hai. Startup
reconciliation sirf un rows ko fail karta hai jinka heartbeat
`SITE_CRAWL_STALE_AFTER_S = 180` se chup hai. Pehle ye *har* non-terminal row ko
fail kar deta tha, jiska matlab tha ki ek doosra process shuru hona — chahe wo port
clash par turant mar bhi jaaye — healthy running crawls ko failed mark kar deta tha.

**Read par reaping.** Sirf startup reconciliation kaafi nahi: beech run mein maara
gaya crawl ek `crawling` kehne wali row chhod jaata hai jo kabhi nahi badalti, aur
client use hamesha poll karta rehta hai. `_reap_if_abandoned` chup row ko usi pal
close kar deta hai jab koi use padhta hai. Doosre process mein chal raha crawl abhi
bhi heartbeat likh raha hota hai, isliye ye wala use kabhi reap nahi karta.

### Operational note: `uvicorn --reload`

Reload process restart karta hai, jo in-flight kisi bhi crawl ko maar deta hai.
Application ki koi cheez ise survive nahi kar sakti. **Asli crawls ke liye backend
ko bina `--reload` ke chalayein** (`python main.py`). `--reload` ke under koi bhi
file save karna running crawl khatam kar deta hai aur row interrupted ke roop mein
close ho jaati hai.

Crawls restart ke paar **resumable nahi** hain. MongoDB mein pehle se likhe pages
survive karte hain aur readable rehte hain, lekin crawl khud aage nahi badhta.

---

## 13. Concurrency aur resource budget

`MAX_CONCURRENT_SITE_CRAWLS = 1`. Ek, do nahi: site crawl poore pages fetch karta
hai (har ek 2 MiB tak) jabki catalog crawl sirf `<head>` padhta hai, aur ye
`http_client.DEFAULT_LIMITS` (24 connections) ko 2 catalog crawls aur 4 audits ke
saath share karta hai. 6 workers wala ek crawl hi wo hai jo un endpoints ko bhookha
rakhe bina bachta hai jinhe user dekh raha hai.

Progress har 25 pages ya 2 seconds par flush hota hai, jo pehle aaye. Wahi boundary
MongoDB bulk write ko bhi batch karti hai, isliye 1,000-page crawl SQLite ke single
writer ke against 1,000 ke bajaye lagbhag 40 `UPDATE` karta hai.

---

## 14. Tests

| File | Tests | Kya cover karta hai |
|---|---|---|
| `tests/test_site_crawl.py` | 64 | engine: traversal, bounds, robots, throttling, page types, stop |
| `tests/test_crawler_urls.py` | 19 | normalisation aur link extraction, bina I/O |
| `tests/test_site_crawl_worker.py` | 26 | status transitions, orphans, scheduling, heartbeats, reconciliation |
| `tests/test_site_crawl_routes.py` | 31 | HTTP contract, validation, reaping, stop |
| `tests/test_site_crawl_integration.py` | 15 | POST se worker se storage tak, sirf HTTP aur Mongo stubbed |

Integration tests sirf network (respx) aur MongoDB (ek dict) ko stub karte hain. Ye
`run_site_crawl` ko scheduler ke bajaye seedha drive karte hain, kyunki detached
task TestClient ke apne event loop par utarta hai, jo response bhejte hi ruk jaata
hai.

---

## 15. Ise badalne se pehle jaanne layak baatein

- **Bounds advisory nahi hain.** Agar aap koi aisa loop add karte hain jo fetch kar
  sakta hai, to use ceiling aur counter dein, aur wahan pahunchne ko exception ke
  bajaye reported outcome banayein.
- **Naya counter column nullable hona chahiye.** Section 10 dekhein.
- **Upar ki taraf import mat karein.** `services/crawler/` ko `services/stores/` se
  import nahi karna chahiye; shared helpers isi wajah se neeche crawler layer mein
  laaye gaye (`throttle.py`, `sitemap.py`, URL classifier). `config/settings.py` ko
  bhi `services/` se import nahi karna chahiye, isi liye `CrawlLimits.from_settings`
  crawler side se `getattr` ke saath settings padhta hai.
- **Analyzer seam ek hook hai, import nahi.** `site_crawl.py` `services.analyzers`
  se anjaan rehta hai, jo khud `services.crawler.parser` import karta hai.
- **Ek page kabhi poora crawl nahi giraana chahiye.** Har per-page failure path
  `error` ya `skipped` set wale `CrawledPage` par khatam hota hai.
