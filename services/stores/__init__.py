"""Store catalog crawling: discover every product and collection, enrich meta.

Layering inside this package:

    store_crawl_service  (HTTP mapping, creates the job row, schedules the worker)
        └─ discovery      (tier 1 Shopify JSON, tier 2 sitemap)   ┐ both use
        └─ enrichment     (head-only meta fetch, bounded, polite) ┘ services.crawler.http_client
        └─ seo_gaps       (pure functions: flags + the Mongo record)

Only `store_crawl_service` may import from `schemas`. The rest import nothing
from `schemas`, `db`, `routes` or `controller`, which is what keeps the existing
`schemas -> services` edge from becoming a cycle.
"""
