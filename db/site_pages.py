"""MongoDB persistence for site-wide crawl pages.

One doc per (crawl_id, url). Keyed by CRAWL rather than by host on purpose: a
site crawl is a snapshot, and the value of keeping two of them is comparing
them. `store_catalog.py` keys by store_domain instead because a catalog is a
living inventory whose items have their own revision history; a crawl is an
event. Same module shape, different identity, deliberately.

Writes are BEST-EFFORT, as everywhere else Mongo is used here: a failure is
logged and reported through `SiteCrawl.persistence_ok`, never raised to the
client. The crawl itself still ran, and its counters are in SQL.

`outlinks` is stored because it IS the site graph - orphan detection needs it -
but it is projected out of list reads: 500 pages x 300 links is a response no
UI wants.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, UpdateOne
from pymongo.errors import PyMongoError

from db.mongo import get_client, get_db

logger = logging.getLogger("seo.mongo.site_pages")

PAGES_COLLECTION = "site_crawl_pages"
#: One doc per (crawl_id, template). A handful per crawl, not thousands.
TEMPLATES_COLLECTION = "site_crawl_templates"

#: Fields never returned by list reads: the link graph is far too large, and the
#: raw robots directive string is detail only one page needs.
#: `findings` is excluded too: 50 pages x 25 findings is a list response
#: nobody reads. So are `text_content` and `headings` - a page of body text
#: times 50 rows is megabytes nobody scrolls. The per-page counts and score
#: survive; the detail endpoint has all of it.
_LIST_PROJECTION = {
    "_id": 0, "outlinks": 0, "robots_meta": 0, "h1": 0, "findings": 0,
    "text_content": 0, "headings": 0,
}
_DETAIL_PROJECTION = {"_id": 0}

_SORTS: dict[str, list[tuple[str, int]]] = {
    # Shallowest first is reading order: the pages nearest the home page matter
    # most. `url` makes every order total, so skip/limit can neither duplicate
    # nor miss a page across pages of results.
    "depth": [("depth", ASCENDING), ("url", ASCENDING)],
    "url": [("url", ASCENDING)],
    "status": [("status_code", DESCENDING), ("url", ASCENDING)],
    "words": [("word_count", ASCENDING), ("url", ASCENDING)],
    "links": [("internal_links", ASCENDING), ("url", ASCENDING)],
    # Worst first - finding problems is the point of the list.
    "score": [("score", ASCENDING), ("url", ASCENDING)],
}

_indexed_client: AsyncMongoClient | None = None


async def ensure_indexes() -> None:
    """Create indexes once per client. Safe to call repeatedly."""
    global _indexed_client
    client = get_client()
    if _indexed_client is client:
        return
    pages = get_db()[PAGES_COLLECTION]
    await pages.create_index(
        [("crawl_id", ASCENDING), ("url", ASCENDING)], unique=True, name="uniq_crawl_url"
    )
    await pages.create_index(
        [("crawl_id", ASCENDING), ("depth", ASCENDING), ("url", ASCENDING)], name="by_crawl_depth"
    )
    # Serves "show me the problems": the issue filters below all narrow on these.
    await pages.create_index(
        [("crawl_id", ASCENDING), ("status_code", ASCENDING)], name="by_crawl_status"
    )
    # Serves "show me the worst pages", the default report ordering.
    await pages.create_index(
        [("crawl_id", ASCENDING), ("score", ASCENDING), ("url", ASCENDING)], name="by_crawl_score"
    )
    await pages.create_index([("host", ASCENDING), ("crawled_at", DESCENDING)], name="by_host_recent")
    # Grouping pages by template is how the per-template reports are read back.
    await pages.create_index(
        [("crawl_id", ASCENDING), ("template", ASCENDING)], name="by_crawl_template"
    )
    await get_db()[TEMPLATES_COLLECTION].create_index(
        [("crawl_id", ASCENDING), ("template", ASCENDING)], unique=True, name="uniq_crawl_template"
    )
    _indexed_client = client


# --- writes (best-effort) -------------------------------------------------------


async def save_pages(records: list[dict[str, Any]]) -> int:
    """Upsert a batch of crawled pages. Returns how many docs the write addressed.

    `upserted + matched`, not `upserted + modified`: re-running a crawl over
    unchanged pages modifies nothing, and the worker would otherwise read 0 as
    "MongoDB failed" and flip persistence_ok. `store_catalog.save_catalog_items`
    documents the same trap.
    """
    if not records:
        return 0
    now = datetime.now(UTC)
    try:
        await ensure_indexes()
        operations = []
        for record in records:
            doc = dict(record)
            doc["crawled_at"] = now
            operations.append(
                UpdateOne(
                    {"crawl_id": record["crawl_id"], "url": record["url"]},
                    {"$set": doc},
                    upsert=True,
                )
            )
        result = await get_db()[PAGES_COLLECTION].bulk_write(operations, ordered=False)
        return (result.upserted_count or 0) + (result.matched_count or 0)
    except PyMongoError as exc:
        logger.warning("MongoDB save_pages failed (%s): %s", type(exc).__name__, exc)
        return 0


# --- reads ------------------------------------------------------------------------

#: Named page filters. Each is a finding a user would actually go looking for,
#: expressed once here so the route, the counters and the export cannot drift.
ISSUE_FILTERS: dict[str, dict[str, Any]] = {
    "broken": {"status_code": {"$gte": 400}},
    "redirected": {"redirect_chain.0": {"$exists": True}},
    "noindex": {"noindex": True},
    "missing_title": {"title": "", "skipped": "", "error": ""},
    "missing_description": {"meta_description": "", "skipped": "", "error": ""},
    "missing_canonical": {"canonical_url": "", "skipped": "", "error": ""},
    "multiple_h1": {"h1_count": {"$gt": 1}},
    "no_h1": {"h1_count": 0, "skipped": "", "error": ""},
    "thin": {"word_count": {"$lt": 200}, "skipped": "", "error": ""},
    "images_missing_alt": {"images_missing_alt": {"$gt": 0}},
    "blocked_by_robots": {"skipped": "robots"},
    "failed": {"error": {"$ne": ""}},
    "orphan": {"orphan": True},
    "critical": {"critical_count": {"$gt": 0}},
    "warning": {"warning_count": {"$gt": 0}},
    "product": {"page_type": "product"},
    "collection": {"page_type": "collection"},
    "heading_skips": {"heading_skips": {"$gt": 0}},
}

#: Pages fetched only to reach the ones that were asked for. Excluded from
#: every read by default: they are real pages, but nobody asked about them,
#: and mixing them into the answer makes the counts wrong. `$ne: False` rather
#: than `True` so documents written before this field existed still count as
#: targets, which they were.
_TARGETS_ONLY: dict[str, Any] = {"is_target": {"$ne": False}}

#: `$in` with 10,000 URLs is a query document megabytes wide; Mongo caps the BSON
#: document at 16 MB and the server plans it as one enormous OR. Chunked instead.
_MARK_CHUNK = 500


async def load_pages(
    crawl_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = "depth",
    issue: str | None = None,
    include_navigation: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """(pages, total matching the filter). `total` is for paging - NOT len(pages)."""
    await ensure_indexes()
    query: dict[str, Any] = {"crawl_id": crawl_id}
    if not include_navigation:
        query.update(_TARGETS_ONLY)
    if issue and issue in ISSUE_FILTERS:
        query.update(ISSUE_FILTERS[issue])
    collection = get_db()[PAGES_COLLECTION]
    total = await collection.count_documents(query)
    cursor = (
        collection.find(query, _LIST_PROJECTION)
        .sort(_SORTS.get(sort, _SORTS["depth"]))
        .skip(offset)
        .limit(limit)
    )
    return await cursor.to_list(limit), total


async def load_page(crawl_id: str, url: str) -> dict[str, Any] | None:
    await ensure_indexes()
    return await get_db()[PAGES_COLLECTION].find_one({"crawl_id": crawl_id, "url": url}, _DETAIL_PROJECTION)


async def issue_counts(crawl_id: str) -> dict[str, int]:
    """How many pages match each named filter - the numbers a report leads with.

    One count_documents per filter rather than one aggregation: the filters are
    not disjoint (a page can be thin AND have no H1), so a `$group` would have to
    fan each doc out anyway, and every one of these is an indexed count.
    """
    await ensure_indexes()
    collection = get_db()[PAGES_COLLECTION]
    counts: dict[str, int] = {}
    for name, condition in ISSUE_FILTERS.items():
        counts[name] = await collection.count_documents(
            {"crawl_id": crawl_id, **_TARGETS_ONLY, **condition}
        )
    return counts


async def load_link_graph(crawl_id: str, limit: int = 10_000) -> list[dict[str, Any]]:
    """Every fetched page with its outlinks. The input to orphan detection."""
    await ensure_indexes()
    cursor = get_db()[PAGES_COLLECTION].find(
        {"crawl_id": crawl_id}, {"_id": 0, "url": 1, "outlinks": 1, "skipped": 1, "error": 1, "status_code": 1}
    )
    return await cursor.to_list(limit)


async def load_inlinks(crawl_id: str, url: str, limit: int = 100) -> list[str]:
    """Crawled pages whose outlinks contain `url`. The reverse edge.

    Served by scanning `outlinks` as a multikey array, which is what makes "what
    links to this page" answerable at all - the forward edges are all we store.
    """
    await ensure_indexes()
    cursor = (
        get_db()[PAGES_COLLECTION]
        .find({"crawl_id": crawl_id, "outlinks": url}, {"_id": 0, "url": 1})
        .limit(limit)
    )
    return [doc["url"] for doc in await cursor.to_list(limit)]


async def load_orphans(crawl_id: str, limit: int = 200) -> tuple[list[str], int]:
    """(orphan urls, total). The list is capped; the count never is."""
    await ensure_indexes()
    collection = get_db()[PAGES_COLLECTION]
    query = {"crawl_id": crawl_id, "orphan": True, **_TARGETS_ONLY}
    total = await collection.count_documents(query)
    cursor = collection.find(query, {"_id": 0, "url": 1}).sort("url", ASCENDING).limit(limit)
    return [doc["url"] for doc in await cursor.to_list(limit)], total


async def delete_crawl(crawl_id: str) -> int:
    """Test/admin helper: remove every page doc for one crawl."""
    await ensure_indexes()
    result = await get_db()[PAGES_COLLECTION].delete_many({"crawl_id": crawl_id})
    return result.deleted_count


async def mark_orphans(crawl_id: str, urls: list[str]) -> int:
    """Flag the pages nothing links to. Best-effort, like every write here.

    Run once at the end rather than per page, because "orphan" is not knowable
    until the whole graph is in: a page crawled first may be linked from a page
    crawled last.
    """
    if not urls:
        return 0
    try:
        await ensure_indexes()
        collection = get_db()[PAGES_COLLECTION]
        marked = 0
        for start in range(0, len(urls), _MARK_CHUNK):
            chunk = urls[start:start + _MARK_CHUNK]
            result = await collection.update_many(
                {"crawl_id": crawl_id, "url": {"$in": chunk}}, {"$set": {"orphan": True}}
            )
            marked += result.modified_count or 0
        return marked
    except PyMongoError as exc:
        logger.warning("MongoDB mark_orphans failed (%s): %s", type(exc).__name__, exc)
        return 0


async def save_template_reports(crawl_id: str, records: list[dict[str, Any]]) -> int:
    """Upsert one record per template. Best-effort, like every write here."""
    if not records:
        return 0
    now = datetime.now(UTC)
    try:
        await ensure_indexes()
        operations = [
            UpdateOne(
                {"crawl_id": crawl_id, "template": record["template"]},
                {"$set": {**record, "crawl_id": crawl_id, "measured_at": now}},
                upsert=True,
            )
            for record in records
        ]
        result = await get_db()[TEMPLATES_COLLECTION].bulk_write(operations, ordered=False)
        return (result.upserted_count or 0) + (result.matched_count or 0)
    except PyMongoError as exc:
        logger.warning("MongoDB save_template_reports failed (%s): %s", type(exc).__name__, exc)
        return 0


async def load_template_reports(crawl_id: str) -> list[dict[str, Any]]:
    """Every template record for one crawl, most pages first."""
    await ensure_indexes()
    cursor = (
        get_db()[TEMPLATES_COLLECTION]
        .find({"crawl_id": crawl_id}, {"_id": 0})
        .sort("page_count", DESCENDING)
    )
    return await cursor.to_list(100)


#: Docs pulled per round trip when exporting. Big enough that a 5,000-page crawl
#: is a handful of queries, small enough that no single batch is a memory event.
EXPORT_BATCH = 200


async def iter_pages(
    crawl_id: str, *, include_navigation: bool = False, batch_size: int = EXPORT_BATCH
) -> AsyncIterator[list[dict[str, Any]]]:
    """Every page of a crawl, in batches, sorted stably.

    Batched rather than `to_list()` because an export is the one read with no
    natural limit: a 5,000-page crawl with its text and link graph is tens of
    megabytes, and materialising all of it to serialise it would double that.
    The caller streams each batch out and lets it go.

    Sorted by url so an export is reproducible - a diff between two exports of
    the same crawl should show data changes, not row shuffling.
    """
    await ensure_indexes()
    query: dict[str, Any] = {"crawl_id": crawl_id}
    if not include_navigation:
        query.update(_TARGETS_ONLY)
    cursor = get_db()[PAGES_COLLECTION].find(query, _DETAIL_PROJECTION).sort("url", ASCENDING)
    batch: list[dict[str, Any]] = []
    async for doc in cursor:
        batch.append(doc)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch
