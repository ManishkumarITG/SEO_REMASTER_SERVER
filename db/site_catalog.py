"""MongoDB persistence for a site crawl's catalog phase (Shopify JSON).

One document per product or collection, per crawl. Keyed by crawl rather than by
store, unlike `db.store_catalog`: a site crawl is a snapshot, and the next crawl
of the same store compares itself against THIS one (incremental re-crawls read
`updated_at` back from here), so overwriting it in place would erase the very
thing the comparison needs.

Conventions are `db.site_pages`'s: writes are best-effort and return 0 on failure
(the crawl carries on and says so), reads let PyMongoError propagate for the
service layer to turn into a 503, and indexes are created once per client.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, UpdateOne
from pymongo.errors import PyMongoError

from db.mongo import get_client, get_db

logger = logging.getLogger("seo.mongo.site_catalog")

CATALOG_COLLECTION = "site_crawl_catalog"

#: Identity is (crawl, kind, handle): `/products/sale` and `/collections/sale`
#: are different things that share a handle.
_KEY_FIELDS = ("crawl_id", "kind", "handle")
_LIST_PROJECTION = {"_id": 0}

_indexed_client: AsyncMongoClient | None = None


async def ensure_indexes() -> None:
    """Create indexes once per client. Safe to call repeatedly."""
    global _indexed_client
    client = get_client()
    if _indexed_client is client:
        return
    items = get_db()[CATALOG_COLLECTION]
    await items.create_index(
        [("crawl_id", ASCENDING), ("kind", ASCENDING), ("handle", ASCENDING)],
        unique=True, name="uniq_crawl_kind_handle",
    )
    # "Show me the products missing alt text": a multikey index on the gap list.
    await items.create_index([("crawl_id", ASCENDING), ("gaps", ASCENDING)], name="by_crawl_gap")
    # The default list order: worst first, stable.
    await items.create_index(
        [("crawl_id", ASCENDING), ("gap_count", DESCENDING), ("handle", ASCENDING)],
        name="by_crawl_gap_count",
    )
    # Incremental re-crawls look an item up by URL in the previous crawl.
    await items.create_index([("crawl_id", ASCENDING), ("url", ASCENDING)], name="by_crawl_url")
    _indexed_client = client


async def save_catalog(records: list[dict[str, Any]]) -> int:
    """Upsert a crawl's catalog. Returns how many docs the write addressed; 0 on failure."""
    if not records:
        return 0
    try:
        await ensure_indexes()
        operations = [
            UpdateOne({key: record[key] for key in _KEY_FIELDS}, {"$set": record}, upsert=True)
            for record in records
        ]
        result = await get_db()[CATALOG_COLLECTION].bulk_write(operations, ordered=False)
        return result.upserted_count + result.matched_count
    except PyMongoError as exc:
        logger.warning("site catalog write failed (%d records): %s", len(records), exc)
        return 0


async def list_catalog(
    crawl_id: str,
    *,
    kind: str | None = None,
    gap: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """A page of a crawl's catalog, worst first. -> (items, total matching)."""
    await ensure_indexes()
    query: dict[str, Any] = {"crawl_id": crawl_id}
    if kind:
        query["kind"] = kind
    if gap:
        query["gaps"] = gap
    collection = get_db()[CATALOG_COLLECTION]
    total = await collection.count_documents(query)
    cursor = (
        collection.find(query, _LIST_PROJECTION)
        .sort([("gap_count", DESCENDING), ("handle", ASCENDING)])
        .skip(max(0, offset))
        .limit(max(1, min(limit, 500)))
    )
    return await cursor.to_list(length=None), total
