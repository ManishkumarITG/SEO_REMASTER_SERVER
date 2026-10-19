"""MongoDB connection management.

This module owns the shared AsyncMongoClient and nothing else. Search Console
data is deliberately NOT persisted: GSC reads go to Google and are returned
straight to the caller, so Google stays the single source of truth and no
search data accumulates here.

The collections that DO live in MongoDB are owned by the modules that write
them: `db.site_pages` (crawled pages and templates) and `db.store_catalog`.
Each creates its own indexes.

OAuth tokens are NEVER written to MongoDB. Credentials stay in the SQL layer
(models.db.GoogleCredential).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from config import get_settings

_client: AsyncMongoClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None

def get_client() -> AsyncMongoClient:
    """Process-wide client, rebuilt if the running event loop changed.

    AsyncMongoClient binds to the loop it was created on and raises if reused
    from another one. The server has a single loop, but scripts, tests and
    workers may each run their own, so detect the switch and start fresh.
    """
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _client is not None and loop is not None and _client_loop is not loop:
        # The old client belongs to a loop we can no longer await on, so drop the
        # reference and let GC reclaim its sockets rather than closing it here.
        _client = None

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
    global _client, _client_loop
    if _client is not None:
        await _client.close()
        _client = None
        _client_loop = None


async def ping() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except PyMongoError:
        return False
