"""MongoDB persistence round-trip.

Skipped automatically when no local MongoDB is running, so the suite stays
green on machines (and CI) without one. Uses a throwaway database that is
dropped afterwards - never the real seo_remaster db.
"""

from __future__ import annotations

import pytest

import db.mongo as mongo
from config import get_settings

TEST_DB = "seo_remaster_pytest"
USER = "pytest-user"


@pytest.fixture
async def mongo_db(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mongodb_db", TEST_DB, raising=False)
    monkeypatch.setattr(mongo, "_client", None)
    monkeypatch.setattr(mongo, "_indexes_ready", False)
    if not await mongo.ping():
        pytest.skip("no local MongoDB on MONGODB_URL")
    client = mongo.get_client()
    await client.drop_database(TEST_DB)
    yield
    await client.drop_database(TEST_DB)
    await mongo.close_client()


async def test_sites_round_trip(mongo_db: None) -> None:
    written = await mongo.save_sites(
        USER,
        [
            {"siteUrl": "https://example.com/", "permissionLevel": "siteOwner"},
            {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteFullUser"},
        ],
    )
    assert written == 2
    sites = await mongo.load_sites(USER)
    assert [s["site_url"] for s in sites] == ["https://example.com/", "sc-domain:example.com"]
    assert sites[0]["permission_level"] == "siteOwner"


async def test_sites_upsert_is_idempotent(mongo_db: None) -> None:
    site = [{"siteUrl": "https://example.com/", "permissionLevel": "siteOwner"}]
    await mongo.save_sites(USER, site)
    await mongo.save_sites(USER, site)
    assert len(await mongo.load_sites(USER)) == 1  # unique index prevents duplicates


async def test_search_analytics_round_trip_and_sync_log(mongo_db: None) -> None:
    rows = [
        {"keys": ["blue widgets"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 4.2},
        {"keys": ["red widgets"], "clicks": 3, "impressions": 90, "ctr": 0.033, "position": 8.9},
    ]
    written = await mongo.save_search_analytics(
        USER, "https://example.com/", "2026-08-01", "2026-08-28", ["query"], rows
    )
    assert written == 2

    stored = await mongo.load_search_analytics(USER, "https://example.com/")
    assert len(stored) == 2
    assert stored[0]["keys"] == ["blue widgets"]  # sorted by clicks desc
    assert stored[0]["clicks"] == 10
    assert "user_id" not in stored[0]  # internal fields are projected out

    syncs = await mongo.load_syncs(USER)
    assert len(syncs) == 1
    assert syncs[0]["row_count"] == 2
    assert syncs[0]["dimensions"] == "query"


async def test_rerunning_same_window_updates_instead_of_duplicating(mongo_db: None) -> None:
    args = (USER, "https://example.com/", "2026-08-01", "2026-08-28", ["query"])
    await mongo.save_search_analytics(
        *args, [{"keys": ["widgets"], "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 3.0}]
    )
    await mongo.save_search_analytics(
        *args, [{"keys": ["widgets"], "clicks": 9, "impressions": 80, "ctr": 0.11, "position": 2.5}]
    )
    stored = await mongo.load_search_analytics(USER, "https://example.com/")
    assert len(stored) == 1  # same key+range upserted, not duplicated
    assert stored[0]["clicks"] == 9  # refreshed to the newer numbers


async def test_different_ranges_are_kept_separately(mongo_db: None) -> None:
    row = [{"keys": ["widgets"], "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 3.0}]
    await mongo.save_search_analytics(
        USER, "https://example.com/", "2026-07-01", "2026-07-28", ["query"], row
    )
    await mongo.save_search_analytics(
        USER, "https://example.com/", "2026-08-01", "2026-08-28", ["query"], row
    )
    assert len(await mongo.load_search_analytics(USER, "https://example.com/")) == 2
    july = await mongo.load_search_analytics(
        USER, "https://example.com/", "2026-07-01", "2026-07-28", "query"
    )
    assert len(july) == 1


async def test_multi_dimension_keys_do_not_collide(mongo_db: None) -> None:
    rows = [
        {"keys": ["widgets", "https://example.com/a"], "clicks": 4, "impressions": 40, "ctr": 0.1, "position": 5.0},
        {"keys": ["widgets", "https://example.com/b"], "clicks": 2, "impressions": 20, "ctr": 0.1, "position": 6.0},
    ]
    written = await mongo.save_search_analytics(
        USER, "https://example.com/", "2026-08-01", "2026-08-28", ["query", "page"], rows
    )
    assert written == 2
    stored = await mongo.load_search_analytics(USER, "https://example.com/")
    assert len(stored) == 2
    assert all(len(row["keys"]) == 2 for row in stored)
