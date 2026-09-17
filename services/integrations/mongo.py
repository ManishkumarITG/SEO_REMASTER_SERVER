"""MongoDB persistence for Google Search Console data.

Every GSC read from Google is mirrored into MongoDB so the app keeps a growing
historical record that can be queried without calling Google again (and without
burning API quota).

Design rules:
- Writes are BEST-EFFORT: if MongoDB is down, the API still answers from Google.
  Persistence failures are logged, never raised to the client.
- OAuth tokens are NEVER written here. Credentials stay in the SQL layer
  (models.db.GoogleCredential); Mongo holds only reporting data.

Collections
-----------
gsc_sites             one doc per (user_id, site_url) - the verified properties
gsc_search_analytics  one doc per (user_id, site_url, range, dimensions, keys) - metric rows
gsc_syncs             one doc per fetch - an audit log of what was pulled and when
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING, AsyncMongoClient, UpdateOne
from pymongo.errors import PyMongoError

from config import get_settings

logger = logging.getLogger("seo.mongo")

SITES_COLLECTION = "gsc_sites"
ROWS_COLLECTION = "gsc_search_analytics"
SYNCS_COLLECTION = "gsc_syncs"

#: Joins dimension values into one scalar so a unique index can cover them.
#: (A unique index directly on the `keys` array would be multikey and reject
#: legitimately different rows.)
_KEY_SEP = "\x1f"

_client: AsyncMongoClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None
_indexes_ready = False


def _joined(keys: list[str]) -> str:
    return _KEY_SEP.join(keys)


def get_client() -> AsyncMongoClient:
    """Process-wide client, rebuilt if the running event loop changed.

    AsyncMongoClient binds to the loop it was created on and raises if reused
    from another one. The server has a single loop, but scripts, tests and
    workers may each run their own, so detect the switch and start fresh.
    """
    global _client, _client_loop, _indexes_ready
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _client is not None and loop is not None and _client_loop is not loop:
        # The old client belongs to a loop we can no longer await on, so drop the
        # reference and let GC reclaim its sockets rather than closing it here.
        _client = None
        _indexes_ready = False

    if _client is None:
        settings = get_settings()
        _client = AsyncMongoClient(
            settings.mongodb_url,
            # Fail fast instead of hanging a request when MongoDB is unreachable.
            serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
            connectTimeoutMS=settings.mongodb_timeout_ms,
            tz_aware=True,
        )
        _client_loop = loop
    return _client


def get_db() -> Any:
    return get_client()[get_settings().mongodb_db]


async def close_client() -> None:
    global _client, _client_loop, _indexes_ready
    if _client is not None:
        await _client.close()
        _client = None
        _client_loop = None
        _indexes_ready = False


async def ping() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except PyMongoError:
        return False


async def ensure_indexes() -> None:
    """Create indexes once per process. Safe to call repeatedly."""
    global _indexes_ready
    if _indexes_ready:
        return
    db = get_db()
    await db[SITES_COLLECTION].create_index(
        [("user_id", 1), ("site_url", 1)], unique=True, name="uniq_user_site"
    )
    await db[ROWS_COLLECTION].create_index(
        [
            ("user_id", 1),
            ("site_url", 1),
            ("start_date", 1),
            ("end_date", 1),
            ("dimensions", 1),
            ("keys_key", 1),
        ],
        unique=True,
        name="uniq_user_site_range_dims_keys",
    )
    # Supports "top queries for this property/range" reads.
    await db[ROWS_COLLECTION].create_index(
        [("user_id", 1), ("site_url", 1), ("start_date", 1), ("end_date", 1), ("clicks", DESCENDING)],
        name="by_range_clicks",
    )
    await db[SYNCS_COLLECTION].create_index(
        [("user_id", 1), ("requested_at", DESCENDING)], name="by_user_recent"
    )
    _indexes_ready = True


async def save_sites(user_id: str, sites: list[dict[str, str]]) -> int:
    """Upsert the verified property list. Returns the number of docs written."""
    if not sites:
        return 0
    now = datetime.now(UTC)
    try:
        await ensure_indexes()
        operations = [
            UpdateOne(
                {"user_id": user_id, "site_url": site["siteUrl"]},
                {
                    "$set": {
                        "permission_level": site.get("permissionLevel", "unknown"),
                        "last_seen_at": now,
                    },
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
            for site in sites
        ]
        result = await get_db()[SITES_COLLECTION].bulk_write(operations, ordered=False)
        return (result.upserted_count or 0) + (result.modified_count or 0)
    except PyMongoError as exc:
        logger.warning("MongoDB save_sites failed (%s): %s", type(exc).__name__, exc)
        return 0


async def save_search_analytics(
    user_id: str,
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    rows: list[dict[str, Any]],
) -> int:
    """Upsert metric rows for one query window, then log the sync. Best-effort."""
    dims = ",".join(dimensions)
    now = datetime.now(UTC)
    written = 0
    try:
        await ensure_indexes()
        db = get_db()
        if rows:
            operations = [
                UpdateOne(
                    {
                        "user_id": user_id,
                        "site_url": site_url,
                        "start_date": start_date,
                        "end_date": end_date,
                        "dimensions": dims,
                        "keys_key": _joined(row.get("keys", [])),
                    },
                    {
                        "$set": {
                            "keys": row.get("keys", []),
                            "clicks": row.get("clicks", 0),
                            "impressions": row.get("impressions", 0),
                            "ctr": row.get("ctr", 0),
                            "position": row.get("position", 0),
                            "fetched_at": now,
                        },
                        "$setOnInsert": {"first_fetched_at": now},
                    },
                    upsert=True,
                )
                for row in rows
            ]
            result = await db[ROWS_COLLECTION].bulk_write(operations, ordered=False)
            written = (result.upserted_count or 0) + (result.modified_count or 0)
        await db[SYNCS_COLLECTION].insert_one(
            {
                "user_id": user_id,
                "site_url": site_url,
                "start_date": start_date,
                "end_date": end_date,
                "dimensions": dims,
                "row_count": len(rows),
                "rows_written": written,
                "requested_at": now,
            }
        )
    except PyMongoError as exc:
        logger.warning("MongoDB save_search_analytics failed (%s): %s", type(exc).__name__, exc)
    return written


async def load_sites(user_id: str) -> list[dict[str, Any]]:
    await ensure_indexes()
    cursor = get_db()[SITES_COLLECTION].find({"user_id": user_id}, {"_id": 0}).sort("site_url", 1)
    return await cursor.to_list(500)


async def load_search_analytics(
    user_id: str,
    site_url: str,
    start_date: str | None = None,
    end_date: str | None = None,
    dimensions: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    await ensure_indexes()
    query: dict[str, Any] = {"user_id": user_id, "site_url": site_url}
    if start_date:
        query["start_date"] = start_date
    if end_date:
        query["end_date"] = end_date
    if dimensions:
        query["dimensions"] = dimensions
    cursor = (
        get_db()[ROWS_COLLECTION]
        .find(query, {"_id": 0, "keys_key": 0, "user_id": 0})
        .sort("clicks", DESCENDING)
        .limit(limit)
    )
    return await cursor.to_list(limit)


async def load_syncs(user_id: str, limit: int = 25) -> list[dict[str, Any]]:
    await ensure_indexes()
    cursor = (
        get_db()[SYNCS_COLLECTION]
        .find({"user_id": user_id}, {"_id": 0, "user_id": 0})
        .sort("requested_at", DESCENDING)
        .limit(limit)
    )
    return await cursor.to_list(limit)
