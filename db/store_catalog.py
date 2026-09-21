"""MongoDB persistence for crawled store catalogs.

Mirrors the conventions of db/mongo.py (the GSC store) and shares its client:
best-effort writes that log and return 0, loud reads that let PyMongoError
propagate for the service to turn into a 503.

Two collections, not four
-------------------------
Products and collections share `store_catalog_items` with a `kind` discriminator.
They share every field the product asks for, so one collection means one unique
index, one upsert path and one read path with a `kind` filter. `kind` has to be in
the identity regardless: `/products/sale` and `/collections/sale` are legitimately
distinct, and a `(store_domain, handle)` key would let one overwrite the other.

Identity is `(store_domain, kind, handle)` - three scalars - so the `keys_key`
trick db/mongo.py needs for its `keys` ARRAY is deliberately not used here.

Scoped globally per store, not per user. `request.session["user_id"]` is only
set after a Google/Reddit OAuth round trip, so a plain visitor has None and
user-keying would file everything under one anonymous bucket - fake scoping.
A store's meta description is public. Who crawled lives in SQL (store_crawls);
what the store contains lives here, once.

Index-ready cache
-----------------
db/mongo.py guards index creation with a module-level bool that get_client()
resets on an event-loop switch. A second module cannot be reached by that reset,
so after a loop switch this module would skip index creation on a brand-new
client. Keying on client identity instead is strictly more correct and needs
zero edits to the file the working GSC feature depends on.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, UpdateOne
from pymongo.errors import PyMongoError

from db.mongo import get_client, get_db

logger = logging.getLogger("seo.mongo.catalog")

ITEMS_COLLECTION = "store_catalog_items"
REVISIONS_COLLECTION = "store_catalog_revisions"

#: Fields never returned by list reads - internal, or too large for a list.
_LIST_PROJECTION = {"_id": 0, "content_hash": 0, "description": 0}
_DETAIL_PROJECTION = {"_id": 0, "content_hash": 0}

_SORTS: dict[str, list[tuple[str, int]]] = {
    # Worst first - finding gaps is the point. `handle` makes the order total so
    # skip/limit cannot duplicate or miss an item across pages.
    "gaps": [("seo_gap_count", DESCENDING), ("handle", ASCENDING)],
    "name": [("name", ASCENDING), ("handle", ASCENDING)],
    "handle": [("handle", ASCENDING)],
}

_indexed_client: AsyncMongoClient | None = None


async def ensure_indexes() -> None:
    """Create indexes once per client. Safe to call repeatedly."""
    global _indexed_client
    client = get_client()
    if _indexed_client is client:
        return
    db = get_db()
    items = db[ITEMS_COLLECTION]
    await items.create_index(
        [("store_domain", ASCENDING), ("kind", ASCENDING), ("handle", ASCENDING)],
        unique=True,
        name="uniq_store_kind_handle",
    )
    await items.create_index(
        [("store_domain", ASCENDING), ("kind", ASCENDING), ("seo_gap_count", DESCENDING), ("handle", ASCENDING)],
        name="by_store_kind_gaps",
    )
    await items.create_index(
        [("store_domain", ASCENDING), ("kind", ASCENDING), ("name", ASCENDING), ("handle", ASCENDING)],
        name="by_store_kind_name",
    )
    await items.create_index(
        [("store_domain", ASCENDING), ("last_seen_at", DESCENDING)],
        name="by_store_recent",
    )
    revisions = db[REVISIONS_COLLECTION]
    await revisions.create_index(
        [("store_domain", ASCENDING), ("kind", ASCENDING), ("handle", ASCENDING), ("content_hash", ASCENDING)],
        unique=True,
        name="uniq_item_hash",
    )
    await revisions.create_index(
        [("store_domain", ASCENDING), ("kind", ASCENDING), ("handle", ASCENDING), ("captured_at", DESCENDING)],
        name="by_item_recent",
    )
    _indexed_client = client


def _identity(record: dict[str, Any]) -> dict[str, Any]:
    return {"store_domain": record["store_domain"], "kind": record["kind"], "handle": record["handle"]}


# --- Writes (best-effort) -----------------------------------------------------


async def save_catalog_items(records: list[dict[str, Any]]) -> int:
    """Phase 1: upsert the discovered items. Returns how many docs the write addressed.

    That is `upserted + matched`, NOT `upserted + modified` as db/mongo.py
    returns. A re-crawl of an unchanged store modifies nothing, and the worker
    would otherwise read 0 as "MongoDB failed" and flip persistence_ok.
    """
    if not records:
        return 0
    now = datetime.now(UTC)
    try:
        await ensure_indexes()
        operations = []
        for record in records:
            doc = dict(record)
            doc.pop("content_hash", None)  # phase 2 owns the hash
            doc["last_seen_at"] = now
            operations.append(
                UpdateOne(_identity(record), {"$set": doc, "$setOnInsert": {"first_seen_at": now}}, upsert=True)
            )
        result = await get_db()[ITEMS_COLLECTION].bulk_write(operations, ordered=False)
        return (result.upserted_count or 0) + (result.matched_count or 0)
    except PyMongoError as exc:
        logger.warning("MongoDB save_catalog_items failed (%s): %s", type(exc).__name__, exc)
        return 0


async def save_catalog_enrichment(records: list[dict[str, Any]]) -> tuple[int, int]:
    """Phase 2: write meta + flags for one batch, appending a revision only where content changed.

    Returns (items_written, revisions_written). The revisions unique index makes
    the append idempotent even if the pre-read was stale, so re-crawling an
    unchanged store writes exactly zero revisions.
    """
    if not records:
        return 0, 0
    now = datetime.now(UTC)
    try:
        await ensure_indexes()
        db = get_db()
        items = db[ITEMS_COLLECTION]

        identities = [_identity(r) for r in records]
        previous = {
            (d["kind"], d["handle"]): d.get("content_hash")
            async for d in items.find(
                {"store_domain": records[0]["store_domain"], "$or": identities},
                {"_id": 0, "kind": 1, "handle": 1, "content_hash": 1},
            )
        }

        updates = []
        revisions = []
        for record in records:
            doc = dict(record)
            doc["enriched_at"] = now
            doc["last_seen_at"] = now
            updates.append(UpdateOne(_identity(record), {"$set": doc, "$setOnInsert": {"first_seen_at": now}},
                                     upsert=True))
            new_hash = record.get("content_hash")
            if new_hash and previous.get((record["kind"], record["handle"])) != new_hash:
                snapshot = {
                    key: record.get(key)
                    for key in ("name", "meta_title", "meta_description", "description_excerpt",
                                "price", "available", "featured_image_url", "featured_image_alt",
                                "canonical_url", "seo_flags", "seo_gap_count", "url")
                }
                revisions.append(
                    UpdateOne(
                        {**_identity(record), "content_hash": new_hash},
                        {"$setOnInsert": {**snapshot, "crawl_id": record.get("last_crawl_id"), "captured_at": now}},
                        upsert=True,
                    )
                )

        written = await items.bulk_write(updates, ordered=False)
        items_written = (written.upserted_count or 0) + (written.matched_count or 0)
        revisions_written = 0
        if revisions:
            rev = await db[REVISIONS_COLLECTION].bulk_write(revisions, ordered=False)
            revisions_written = rev.upserted_count or 0
        return items_written, revisions_written
    except PyMongoError as exc:
        logger.warning("MongoDB save_catalog_enrichment failed (%s): %s", type(exc).__name__, exc)
        return 0, 0


# --- Reads (loud) -----------------------------------------------------------------


async def load_catalog_items(
    store_domain: str,
    kind: str,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = "gaps",
    only_gaps: bool = False,
    flag: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """(items, total matching the filter). `total` is for paging - it is NOT len(items)."""
    await ensure_indexes()
    query: dict[str, Any] = {"store_domain": store_domain, "kind": kind}
    if flag:
        query["seo_flags"] = flag
    elif only_gaps:
        query["seo_gap_count"] = {"$gt": 0}
    collection = get_db()[ITEMS_COLLECTION]
    total = await collection.count_documents(query)
    cursor = collection.find(query, _LIST_PROJECTION).sort(_SORTS.get(sort, _SORTS["gaps"])).skip(offset).limit(limit)
    return await cursor.to_list(limit), total


async def load_catalog_item(store_domain: str, kind: str, handle: str) -> dict[str, Any] | None:
    await ensure_indexes()
    return await get_db()[ITEMS_COLLECTION].find_one(
        {"store_domain": store_domain, "kind": kind, "handle": handle}, _DETAIL_PROJECTION
    )


async def load_item_revisions(store_domain: str, kind: str, handle: str, limit: int = 20) -> list[dict[str, Any]]:
    await ensure_indexes()
    cursor = (
        get_db()[REVISIONS_COLLECTION]
        .find({"store_domain": store_domain, "kind": kind, "handle": handle}, {"_id": 0})
        .sort("captured_at", DESCENDING)
        .limit(limit)
    )
    return await cursor.to_list(limit)


async def flag_counts(store_domain: str, kind: str | None = None) -> dict[str, int]:
    """How many items carry each flag - the numbers the UI leads with."""
    await ensure_indexes()
    match: dict[str, Any] = {"store_domain": store_domain}
    if kind:
        match["kind"] = kind
    pipeline = [
        {"$match": match},
        {"$unwind": "$seo_flags"},
        {"$group": {"_id": "$seo_flags", "n": {"$sum": 1}}},
    ]
    cursor = await get_db()[ITEMS_COLLECTION].aggregate(pipeline)
    return {doc["_id"]: doc["n"] async for doc in cursor}


async def delete_store(store_domain: str) -> int:
    """Test/admin helper: remove every doc for one store. Returns docs removed."""
    await ensure_indexes()
    db = get_db()
    a = await db[ITEMS_COLLECTION].delete_many({"store_domain": store_domain})
    b = await db[REVISIONS_COLLECTION].delete_many({"store_domain": store_domain})
    return a.deleted_count + b.deleted_count
