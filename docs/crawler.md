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
| Wall clock | 1800s, site ke size ke hisaab se badhta hai (neeche) | `SITE_CRAWL_DEADLINE_S`, `..._PER_TARGET_S`, `..._MAX_S` | `deadline_hit`, `deadline_s` |
| Navigation detour | 100 pages | `CrawlLimits.max_navigation_pages` | `hit_navigation_budget` |
| Bytes per page | 2 MiB | `MAX_PAGE_BYTES` | page par `truncated` |
| Koi insaan Stop dabaye | — | `POST .../stop` | `stopped_by_user` |

**`max_pages = 0` ka matlab hai koi page limit nahi.** Zero hi ek-matr special
value hai; baaki har number ek literal ceiling hai, aur negative value galti se
unlimited banne ke bajaye zero par floor ho jaati hai. Bina page limit ke sirf
deadline aur depth limit bache rehte hain, isliye in dono ko API se off nahi kiya
ja sakta. Unlimited case ke liye `CrawlLimits.page_budget` `None` return karta hai
taaki callers galti se `0` ke against compare na kar sakein.

**Deadline site ke size ke hisaab se set hoti hai.** 500-page crawl ke liye bani
fixed 30 minute ki deadline ne ek 8,000-page store ko 701 pages par hi rok diya tha.
Ab sitemap padhne ke baad, jab frontier ka size pata chal jaata hai, deadline
`max(SITE_CRAWL_DEADLINE_S, expected pages x SITE_CRAWL_DEADLINE_PER_TARGET_S)`
ban jaati hai, `SITE_CRAWL_DEADLINE_MAX_S` (6 h) par capped. Expected pages =
frontier size, ya page budget agar wo chhota hai. Crawl ek note jodta hai ("time
limit raised to 2h 13m for 8,001 pages") aur lagayi gayi value row par `deadline_s`
mein hoti hai. `SITE_CRAWL_DEADLINE_PER_TARGET_S = 0` purani fixed deadline wapas
laata hai.

Requests ko edge par bhi cap kiya jaata hai, `schemas/site_crawl.py` mein:
`MAX_REQUESTABLE_PAGES = 25000`, `MAX_REQUESTABLE_DEPTH = 10`. Inhe teen layer andar
chupchaap clamp karne ke bajaye 422 ke saath reject kiya jaata hai. Page ceiling ye
tay karti hai ki crawl *kitni der* chalega, ye nahi ki host par kitna zor padega:
rate limiter (section 5) request rate ko host ki bardasht ke neeche rakhta hai,
chahe kitne bhi pages maange jaayein.

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
- **Rate ceiling.** Site crawl ka adaptive limiter (section 5) kabhi
  `1 / max(min_interval_s, Crawl-delay)` se tez nahi bhejta: default 0.1s floor par
  10 req/s. Ye poori requests ke bajaye request *starts* book karta hai, isliye
  `concurrency` workers in-flight rehte hue bhi ceiling bani rehti hai. Catalog
  crawl ab bhi fixed `HostGate` (0.06s floor) use karta hai, kyunki ek `<head>` read
  poore page se halka hota hai.
- **Bot challenge ek signal hai, rukawat nahi.** Jo response challenge
  interstitial ho ("Verifying your connection...", Cloudflare ka
  `cf-mitigated: challenge`), us par limiter zor se back off karta hai. Use kabhi
  solve, bypass ya retry karke ghooma nahi jaata. `services/crawler/outcomes.py`
  dekhein.
- **SSRF.** Seed host ko `is_public_host` se check kiya jaata hai (loopback,
  private, link-local, reserved, multicast reject). Frontier mein aane wala har URL
  same-site hona chahiye, taaki target ke control wala koi document crawler ko
  kisi naye host par na mod sake. httpx redirects ko phir bhi kahin bhi follow
  karta hai, isliye links follow karne se pehle *final* URL par same-site rule
  dobara lagaya jaata hai.

---

## 5. Rate limiting: host ka rate seekho, give up mat karo

429 ka matlab hai "bahut tez", "chale jao" nahi. Page ko retry kiya jaata hai, aur
crawl seekhta hai ki ye host asal mein kitni tezi bardasht karta hai.

### Purana back-off kyun hataya gaya

Site crawl `HostGate.back_off()` use karta tha, jo **har 429 response par**
requests ke beech ka gap 4 guna kar deta tha. 6 requests in-flight hone par ek hi
rate-limit burst paanch-chhe 429 ke roop mein aata hai, isliye ek hi event se gap
0.1 → 0.4 → 1.6 → 6.4 → 25.6 → 30s tak compound ho jaata tha, phir har page par ×0.8
se ghatta aur wapas aane mein ~150s lagte. go2marine.com ke asli crawl mein (1,821s
mein 701 pages, deadline par ruka) ~1,500s isi recovery mein gaye: crawler ka
lagbhag 85% waqt apni hi over-reaction ka intezaar karne mein gaya. Stalls ke beech
fetching khud ~4.5 pages/s chal rahi thi.

### Ab ye kaise kaam karta hai (`services/crawler/throttle.py`)

`LocalRateLimiter` `AimdState` ko wrap karta hai, jo ek pure state machine hai (use
`now` diya jaata hai, wo khud clock nahi padhta) aur interval ke bajaye **rate**
control karta hai:

- **Episode coalescing.** Har request us epoch ke saath jaati hai jisme wo bheji
  gayi. Sirf current epoch ka 429 rate kaat-ta hai; usi burst ke baaki gine jaate
  hain par ignore hote hain. Ek saath aaye chhe 429 ek hi signal hain.
- **AIMD.** Throttle rate ko ×0.6 kaat-ta hai (bot challenge par ×0.36) aur host ko
  naye rate par 4 send slots ke liye rok deta hai (0.25-30s; 60s ke andar episodes
  dohraaye to ×2 phir ×4; challenge par kam se kam 60s). Success rate ko *knee* (jis
  rate par pichhla 429 aaya) se kaafi neeche hone par har second ke traffic par
  +0.5 req/s badhata hai, aur knee ke paas knee ka 2%.
- **Lockout ka intezaar hota hai, usme rate nahi kaata jaata.** go2marine par naapa
  gaya: 429 ke baad store ~20s tak *sab kuch* refuse karta raha, hamara rate chahe
  jo ho. Isliye pichhle 429 ke baad bina kisi success ke aaya 429 wahi lockout hai,
  is baat ka naya saboot nahi ki hum bahut tez hain: rate ko chhoda jaata hai aur
  pause double hota hai. Purana logic har refused retry par rate kaat-ta tha aur ek
  hi lockout par use 1.46 → 0.88 → 0.53 req/s tak gira deta tha.
- **Lockout ki lambai seekhi jaati hai**, *send* times se: ye pehli refused request
  bhejne par shuru hua aur aakhri refused send aur pehle successful send ke beech
  khatam hua (midpoint estimate hai). Baad ke episodes poora lockout ek hi pause mein
  (10% margin ke saath) intezaar karke nikaalte hain, use dobara dhoondhne mein
  retries kharch karne ke bajaye.
- **Cruise.** Episode ke baad rate 60s ke liye `cruise_fraction x knee` par rakha
  jaata hai, knee stable rehne par (25% se kam hile) 600s tak double hota hai, phir
  dobara upar probe karta hai. Band 25% hai, 10% nahi: sirf probing ka shor hi knee
  ko 10-20% hila deta hai, aur lockout store par har bekaar probe 15-20s ka lockout
  padta hai. Jis knee ke upar rate poora ek second bina refusal ke tika rahe, wo rate
  ke saath upar uthta hai, taaki koi purana ya niraashavaadi starting point growth ko
  atka na de.
- **Seekha hua cruise fraction.** Burst bucket wala host humein uske sustainable
  rate se upar tab tak jaane deta hai jab tak bucket khaali na ho, isliye knee uski
  asli bardasht ko badha-chadha kar dikhata hai. Cruise *ke dauraan* aaya 429
  fraction ko ×0.9 ghataata hai (floor 0.5); saaf cruise 0.02 wapas deta hai. Kaat
  kabhi cruise cap ke upar nahi girta, isliye cruise ke dauraan 429 ka matlab sach
  mein yahi hai ki cap bahut tez tha.
- **Retry-After** dono form mein honour hota hai. Date ko response ke apne `Date`
  header se naapa jaata hai, isliye local clock drift (WSL2, sleep) use bigaad nahi
  sakta. Ye poore host ko rokta hai, sabse door wala pause jeetta hai,
  `MAX_RETRY_AFTER_S = 60` par capped.
- **Lock ke andar koi sleep nahi.** Worker ek slot book karta hai, 0.5s ke tukdon
  mein us tak sleep karta hai, phir check karta hai ki booking ab bhi valid hai (beech
  mein throttle aaya to dobara book karta hai). Stop aur deadline ek tukde ke andar
  wait tod dete hain.

**Shuruaati rate.** Pehli baar crawl ho raha host `SITE_CRAWL_INITIAL_RATE_RPS =
4.0` se shuru hota hai aur pehli pushback tak har second ×1.25 badhta hai. Pichhle
30 din mein crawl hua host apne pichhle crawl par store hue **learned rate**
(`learned_rate_rps`) se shuru hota hai, taaki re-crawl 429 kha-kha kar limit dobara
na dhoondhe.

**Simulation mein naapa gaya** (`tests/test_rate_limiter.py`, chhe workers, 30 min):

| Store | Pehla crawl (slow start) | Re-crawl (learned rate) |
|---|---|---|
| 429 ke baad 10-20s lockout, 1.0-2.0 req/s (go2marine jaisa) | uski raftaar ka 80-88%, 2.0-3.4% throttled | 84-91%, 0.8-1.1% throttled |
| simple token bucket, 1.5-6.0 req/s | 84-93%, 0.1-1.0% | 84-97%, 0.1-0.6% |

Asli go2marine crawl par purana gate 6.7% throttled par chal raha tha. Kisi store
ka pehla crawl ek baar ki discovery keemat chukata hai (lockout store par pehle teen
minute mein lagbhag 40-50 refused requests), kyunki anjaan limit dhoondhne ka ek hi
tareeka hai use chhoona; learned rate us store ke har agle crawl ko informed shuru
karata hai.

**Asli store par naapa gaya** (go2marine, 2026-10-20, purane crawl wali hi settings,
15 minute ka window):

| | Purana gate | Naya limiter |
|---|---|---|
| 15 min mein pages | 350 | **815** |
| Throughput ka pattern | ~70 ke bursts, phir 2-3 min band | har minute lagbhag 54 pages, barabar |
| Ruka hua samay (≥10s gaps) | 96% | **2%** |
| 429s, requests ka hissa | 6.7% | **0.7%** (har episode mein ek) |
| 4xx / 5xx / bot challenges | 0 | 0 |

Uska sustainable rate lagbhag **0.9 req/s** hai. Purana crawl stalls ke beech jo ~4.5
pages/s dikhata tha wo burst capacity thi, aur store 429 ke baad client ko ~20s ke
liye lock out karta hai. Aise store ka poora HTML crawl isi rate se bandha hai,
hardware chahe jo ho: 5,000 pages ≈ 1.5 ghante, 20,000 ≈ 6 ghante. Aise store par
tez hone ka tareeka kam pages maangna hai (catalog JSON, incremental re-crawls),
unhe tez maangna nahi.

Page ab bhi `MAX_THROTTLE_RETRIES = 4` baar tak retry hota hai; har retry bas limiter
se apne agle slot ka intezaar karta hai (upar se koi doosra sleep nahi). Agar page
phir bhi nahi padha ja sakta, to **wo page** unreachable record hota hai
(`gave_up_throttled`) aur crawl chalta rehta hai. `503` ko bhi throttling maana
jaata hai.

Counters, sab row aur API par: `throttled` (429/503 responses), `throttle_episodes`
(alag-alag pushbacks, host ne kitni baar asal mein aapatti ki iska imaandaar naap),
`throttle_pause_s`, `current_rate_rps`, `learned_rate_rps`, `gave_up_throttled`.
Live progress flush ab throttle counters bhi likhta hai, isliye "rate limiting us"
banner tab dikhta hai jab ye sach ho, sirf aakhir mein nahi.

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
zyada `MAX_SITEMAP_FETCHES = 50` documents (index files aur failed fetches bhi gine
jaate hain). Pehle ye 10 tha, aur ek bada Shopify store, jo apna catalog kai child
sitemaps mein baant-ta hai, 8,001 URLs par hi seeding rok deta tha.

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
50000` jab derive karne ke liye koi page budget hi na ho. Note *"sitemap seeding
stopped at N URLs"* ka matlab hai wo cap pahunch gaya; ye information hai, error
nahi.

---

## 8a. Shopify stores par catalog-first

10,000 products wala store matlab 10,000 page fetches, aur ek Shopify storefront
lagbhag ek request per second hi sustain karta hai (section 5), isliye us store ka
HTML crawl kareeb teen ghante leta hai. Uska public `/products.json` ek request mein
250 products deta hai, toh wahi catalog JSON ke roop mein ~40 requests hai. Jab
probe homepage par Shopify ke markers dekhta hai (`SiteProbe.is_shopify`, probe ke
apne GET se padha jaata hai, isliye koi extra request nahi lagti), worker koi bhi
page fetch karne se **pehle** poora catalog padh leta hai
(`workers/tasks.py::_run_catalog_phase`, `services/site_crawl_catalog.py`).

Kareeb ek minute mein ye milta hai:

- **Catalog ke content ke findings**: `description_missing`, `image_missing`,
  `image_alt_missing`, `collection_empty`, aur `duplicate_name` (do products
  jinke naam sirf case ya spacing mein alag hain). Definitions store crawl ki apni
  hain (`services/stores/seo_gaps.py`), isliye dono features gap ke matlab par kabhi
  alag nahi bol sakte.
- **Har item ka ek document** MongoDB `site_crawl_catalog` mein, item ke
  `updated_at` ke saath: incremental re-crawl isi se compare karega.
- **Seeds**: har product aur collection URL `extra_seeds` ke through page crawl
  ke frontier mein jaata hai, `seeded_from_catalog` mein gina jaata hai.

JSON kya nahi bata sakta: meta title, meta description aur canonical rendered
`<head>` mein hote hain, isliye wo abhi bhi page crawl se aate hain, jo turant baad
chalta hai. Dono ek doosre ke complement hain.

Iske rules:

- **Ek hi paced client.** Catalog read wahi `LocalRateLimiter` use karta hai jo
  baad mein page crawl karta hai (`limiter_for` ise dono phases se pehle banata
  hai). Store ko ek hi client dikhta hai, aur page crawl wahan se shuru hota hai
  jo catalog read ne seekha.
- **429 par dobara poocho, chupchaap ruko mat.** Paced walk
  (`services/stores/discovery.py::_walk` ek `pacer` ke saath) 429/503 ya transport
  error ko `MAX_JSON_THROTTLE_RETRIES = 4` baar tak retry karta hai. Store crawl
  jo unpaced walk use karta hai, wo nahi badla.
- **Imaandaar completeness.** `complete` ka matlab hai dono endpoints short ya
  empty page tak padhe gaye. Cap, refusal, Stop ya time limit use `partial` chhodte
  hain, note ke saath, taaki "humne 4,000 products padhe" kabhi "store mein 4,000
  hain" ki tarah report na ho.
- **Har endpoint par robots.txt.** Jo store `/products.json` disallow karta hai,
  uska JSON nahi padha jaata; uske products sitemap se aate hain.
- **401/403 ek deewar hai.** `blocked` report hota hai, limiter ko severe signal
  jaata hai, retry nahi hota, aur agla endpoint try nahi hota.
- **Ye crawl ko kabhi fail nahi karta.** Koi bhi exception `catalog_status =
  unavailable` aur ek note chhodta hai, aur crawl sitemap se chalta rehta hai.

| Setting | Default | Matlab |
|---|---|---|
| `site_crawl_catalog_first` | `True` | Shopify stores par ye phase chalao |
| `site_crawl_catalog_max_products` | 50,000 | `/products.json` ka item cap |
| `site_crawl_catalog_max_collections` | 5,000 | `/collections.json` ka item cap |
| `site_crawl_catalog_timeout_s` | 600 | phase yahan rukta hai aur page crawl shuru hota hai |

Row columns: `catalog_status` (`reading`, `complete`, `partial`, `unavailable`,
ya null jab phase chala hi nahi), `catalog_products`, `catalog_collections`,
`catalog_requests`, `catalog_elapsed_s`, `catalog_gaps` (gap -> count). Items:
`GET /api/site-crawls/{id}/catalog?kind=&gap=&offset=&limit=`, sabse zyada gaps
pehle. Site crawl page ye panel crawl chalte hue hi dikhata hai.

---

## 9. Page analysis

Analyzers crawl ke **andar** chalte hain, `page_hook` seam ke through, kyunki DOM
sirf tab tak exist karta hai jab tak page facts mein reduce ho raha hota hai. Baad
mein score karne ka matlab hota poori site do baar fetch karna.

Sirf wahi analyzers bulk mein safe hain jo unhe di gayi `PageData` padhte hain:

| Analyzer | Bulk-safe | Kyun |
|---|---|---|
| `on_page` | haan | titles, descriptions, headings, images, links |
| `technical` | haan | robots, canonical, viewport, status, timing |
| `pagespeed` | nahi | har page par ek Lighthouse run |
| `content_ai` | nahi | har page par ek LLM completion |
| `search_console` | nahi | har page par ek Google API query |

`services/site_crawl_analysis.py::BULK_SAFE` ise enforce karta hai: set ke bahar
configure kiya gaya naam chupchaap API bill badhaane ke bajaye ek log line ke saath
refuse hota hai. `SITE_CRAWL_ANALYZERS` se configure karein; khaali value scoring
off kar deti hai, jo har page ke zero score karne se alag hai (`avg_score` tab
`null` hota hai).

**Parsing ki keemat.** Ye sab API ke event loop par chalta hai, isliye ye wo CPU hai
jiske peeche baaki har worker intezaar karta hai. Har page do baar parse hota hai:
ek baar DOM mein, ek baar uske text ke liye scratch tree mein
(`parser.extract_texts`, jo ek hi tree se visible text aur chrome-stripped main
text dono deta hai). Pehle ye chaar baar parse hota tha, teen poore
re-serializations ke saath, kyunki `visible_text` aur `extract_main_text` dono DOM
ko wapas string bana kar dobara parse karte the, aur `on_page` word count ke liye
yahi phir karta tha. Ek asli 744 KB Shopify page par naapa gaya: 99.9 ms → 37.6 ms
per page (62% kam), facts bilkul same. `PageData.visible_text` text ko analyzer tak
le jaata hai taaki wo ise dobara na kare.

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
har page se pehle *aur* rate limiter se slot ke har intezaar ke andar check karta
hai; crawl phir apne normal completion path se exit karta hai. Iske bajaye task
cancel karne se wo pages kho jaate jo fetch to ho gaye lekin abhi likhe nahi gaye.
Wire par pehle se gayi request phir bhi poori hoti hai, lekin throttle pause ka
intezaar kar raha page 0.5s ke andar chhod diya jaata hai, isliye stop ab back-off
khatam hone ka intezaar nahi karta. Deadline bhi waits ko isi tarah todti hai. Page
budget khatam hona intezaar kar rahe page ko **nahi** todta: jisne budget khatam
hone se pehle apna slot reserve kar liya, wo budget ka hissa hai.

**Heartbeats.** Worker har progress flush par **aur 10s ke timer par**
(`SITE_HEARTBEAT_EVERY_S`) `heartbeat_at` likhta hai, jab tak crawl chal raha hai,
Lighthouse template phase samet. Pehle progress sirf page aane par flush hota tha,
isliye throttle pause ka intezaar kar raha ya minute bhar templates naap raha crawl
`heartbeat_at` padhne wale kisi bhi doosre process ko mara hua lagta tha. Startup
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
hai (har ek 2 MiB tak) jabki catalog crawl sirf `<head>` padhta hai, aur unhe parse
karna API ke apne event loop par chalta hai. Har crawl apna **alag** connection pool
kholta hai (`http_client.DEFAULT_LIMITS` se sized, 24 connections); pools catalog
crawls ya audits ke saath share nahi hote, isliye ye limit connections ke baare mein
nahi, event-loop CPU ke baare mein hai.

SQL progress row har 25 pages ya 2 seconds par update hoti hai, jo pehle aaye, plus
10s heartbeat. MongoDB sirf **har 25 pages** par likha jaata hai (2s timer SQL par
lagta hai, Mongo par nahi).

---

## 14. Tests

| File | Tests | Kya cover karta hai |
|---|---|---|
| `tests/test_site_crawl.py` | 64 | engine: traversal, bounds, robots, throttling, page types, stop |
| `tests/test_rate_limiter.py` | 42 | fake clock par adaptive limiter, plus token-bucket host ke against simulation |
| `tests/test_crawler_urls.py` | 19 | normalisation aur link extraction, bina I/O |
| `tests/test_site_crawl_worker.py` | 30 | status transitions, orphans, scheduling, heartbeats, reconciliation, catalog phase |
| `tests/test_site_crawl_routes.py` | 61 | HTTP contract, validation, reaping, stop, catalog endpoint |
| `tests/test_site_crawl_catalog.py` | 10 | paced JSON walk (429 retry, refusal, robots, caps, Stop), catalog findings aur records |
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
- **Har request limiter se hokar jaati hai aur wapas report karti hai.** Naya fetch
  path ticket `acquire` kare aur response `report` kare, warna limiter usse kuch
  nahi seekhta aur host ko bina pacing ki request dikhti hai. `AimdState` ko I/O aur
  clocks se mukt rakhein taaki simulation tests deterministic rahein.
- **Challenge detection narrow rehna chahiye.** Cloudflare kuch sites par *aam*
  pages mein bhi `/cdn-cgi/challenge-platform/` scripts inject karta hai; sirf us
  path ko match karna har page ko challenge maan leta aur crawl ko uske sabse dheeme
  rate par la deta. Sirf wahi markers gine jaate hain jo khud interstitial par hote
  hain.
- **Catalog phase ek head start hai, requirement nahi.** `_run_catalog_phase` se
  bahar koi exception nahi jaana chahiye, aur har catalog request crawl ke limiter
  se hokar jaati hai. Adhoora read khud bataye ki wo adhoora hai.
