"""CacheProtocol + in-process TTL cache; Redis implementation used only when REDIS_URL is set."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from config import get_settings

logger = logging.getLogger("seo.cache")

#: Ceiling on entries held by the in-process cache. Keys are derived from
#: user-supplied URLs (one PSI entry per audited page, one Search Console entry
#: per page+range), so an unbounded dict grows for the life of the process and
#: expired entries are only ever reclaimed if something asks for them again.
MAX_ENTRIES = 2000


class CacheProtocol(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...


class InMemoryTTLCache:
    """Per-process TTL cache, bounded at MAX_ENTRIES. Good enough for dev / a single worker."""

    def __init__(self, default_ttl_seconds: int = 3600, max_entries: int = MAX_ENTRIES) -> None:
        self._default_ttl = default_ttl_seconds
        self._max_entries = max_entries
        self._store: dict[str, tuple[float, Any]] = {}

    async def get(self, key: str) -> Any | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        if len(self._store) >= self._max_entries and key not in self._store:
            self._evict()
        self._store[key] = (time.monotonic() + ttl, value)

    def _evict(self) -> None:
        """Drop expired entries; if that frees nothing, drop the oldest quarter.

        dicts preserve insertion order, so the first keys are the least recently
        written - close enough to LRU for a cache whose only job is to avoid
        repeating an API call.
        """
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._store.items() if now >= expires_at]
        for key in expired:
            del self._store[key]
        if expired:
            return
        for key in list(self._store)[: max(1, self._max_entries // 4)]:
            del self._store[key]


class RedisCache:
    """JSON-serializing cache backed by Redis. Instantiated only when REDIS_URL is set.

    A cache is an optimisation, never a dependency: if Redis is unreachable or
    returns something unparseable, every operation degrades to a miss rather
    than raising into the analyzer that happened to ask.
    """

    def __init__(self, redis_url: str, default_ttl_seconds: int = 3600) -> None:
        import redis.asyncio as redis  # lazy: redis is an optional dependency

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._default_ttl = default_ttl_seconds

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as exc:  # noqa: BLE001 - a cache miss must never fail the caller
            logger.warning("Redis GET failed (%s): %s - treating as a miss", type(exc).__name__, exc)
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        try:
            await self._redis.set(key, json.dumps(value), ex=ttl)
        except Exception as exc:  # noqa: BLE001 - failing to cache is not failing the request
            logger.warning("Redis SET failed (%s): %s - result not cached", type(exc).__name__, exc)


_cache: CacheProtocol | None = None


def get_cache() -> CacheProtocol:
    global _cache
    if _cache is None:
        settings = get_settings()
        if settings.redis_url:
            try:
                _cache = RedisCache(settings.redis_url, settings.cache_ttl_seconds)
            except Exception as exc:  # noqa: BLE001
                # REDIS_URL is set but the optional `redis` package is missing or the
                # URL is malformed. Same graceful-degradation rule as a missing API
                # key: warn once and keep serving. Without this the error reaches
                # every analyzer that touches the cache and each records an `error`.
                logger.warning(
                    "Could not initialise the Redis cache (%s: %s) - falling back to the "
                    "in-process cache. Check REDIS_URL, and run: pip install redis",
                    type(exc).__name__, exc,
                )
                _cache = InMemoryTTLCache(settings.cache_ttl_seconds)
        else:
            _cache = InMemoryTTLCache(settings.cache_ttl_seconds)
    return _cache
