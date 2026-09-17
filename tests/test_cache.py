"""The cache is an optimisation, never a dependency, and never an unbounded dict."""

from __future__ import annotations

import time

import pytest

from services.cache import InMemoryTTLCache, RedisCache


async def test_hit_and_expiry(monkeypatch):
    cache = InMemoryTTLCache(default_ttl_seconds=60)
    await cache.set("k", {"v": 1})
    assert await cache.get("k") == {"v": 1}

    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 61)
    assert await cache.get("k") is None


async def test_entry_count_stays_bounded():
    """Distinct URLs must not grow the cache forever - keys come from user input."""
    cache = InMemoryTTLCache(default_ttl_seconds=3600, max_entries=50)
    for index in range(500):
        await cache.set(f"psi:mobile:https://example.com/page-{index}", {"score": index})

    assert len(cache._store) <= 50


async def test_expired_entries_are_reclaimed_without_being_read(monkeypatch):
    """Entries nobody asks for again are still evicted; otherwise they leak."""
    cache = InMemoryTTLCache(default_ttl_seconds=60, max_entries=10)
    for index in range(10):
        await cache.set(f"old-{index}", index)

    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 61)
    await cache.set("fresh", 1)

    assert len(cache._store) == 1
    assert await cache.get("fresh") == 1


class _BrokenRedis:
    async def get(self, key):
        raise ConnectionError("redis is down")

    async def set(self, key, value, ex=None):
        raise ConnectionError("redis is down")


async def test_redis_outage_degrades_to_a_miss():
    """A Redis outage must not turn every analyzer into an error result."""
    cache = RedisCache.__new__(RedisCache)
    cache._redis = _BrokenRedis()
    cache._default_ttl = 60

    assert await cache.get("k") is None
    await cache.set("k", {"v": 1})  # must not raise


async def test_redis_url_without_the_package_falls_back(monkeypatch):
    import services.cache as cache_module

    monkeypatch.setattr(cache_module, "_cache", None)
    settings = cache_module.get_settings()
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    monkeypatch.setattr(
        cache_module, "RedisCache", lambda *a, **k: (_ for _ in ()).throw(ImportError("no redis"))
    )

    cache = cache_module.get_cache()

    assert isinstance(cache, InMemoryTTLCache)
    monkeypatch.setattr(cache_module, "_cache", None)


@pytest.mark.parametrize("bad_url", ["redis://", "not-a-url"])
async def test_malformed_redis_url_falls_back(monkeypatch, bad_url):
    import services.cache as cache_module

    monkeypatch.setattr(cache_module, "_cache", None)
    settings = cache_module.get_settings()
    monkeypatch.setattr(settings, "redis_url", bad_url)
    monkeypatch.setattr(
        cache_module, "RedisCache", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad url"))
    )

    assert isinstance(cache_module.get_cache(), InMemoryTTLCache)
    monkeypatch.setattr(cache_module, "_cache", None)
