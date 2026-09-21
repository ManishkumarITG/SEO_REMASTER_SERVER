"""MongoDB persistence for crawled catalogs.

Skipped automatically when no local MongoDB is running. Uses the throwaway
`seo_remaster_pytest` database and a unique store domain per test.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import db.mongo as mongo
import db.store_catalog as catalog
from config import get_settings
from services.stores.models import CatalogItem
from services.stores.seo_gaps import build_catalog_record, item_gaps

TEST_DB = "seo_remaster_pytest"


@pytest.fixture
async def mongo_db(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mongodb_db", TEST_DB, raising=False)
    monkeypatch.setattr(mongo, "_client", None)
    monkeypatch.setattr(mongo, "_indexes_ready", False)
    monkeypatch.setattr(catalog, "_indexed_client", None)
    if not await mongo.ping():
        pytest.skip("no local MongoDB on MONGODB_URL")
    client = mongo.get_client()
    await client.drop_database(TEST_DB)
    yield
    await client.drop_database(TEST_DB)
    await mongo.close_client()


def _domain() -> str:
    return f"{uuid4().hex[:10]}.example.com"


def _rec(domain: str, kind: str, handle: str, *, enriched: bool = False, meta_title: str = "", crawl: str = "c1"):
    item = CatalogItem(kind=kind, handle=handle, url=f"https://{domain}/{kind}s/{handle}", name=handle.title(),
                       description="d", featured_image_url="i", featured_image_alt="a", enriched=enriched,
                       meta_title=meta_title, meta_description="x" * 100 if enriched else "")
    item.gaps = item_gaps(item)
    return build_catalog_record(item, store_domain=domain, store_url=f"https://{domain}/", crawl_id=crawl,
                                source="products_json")


async def test_a_product_and_a_collection_sharing_a_handle_are_stored_separately(mongo_db: None) -> None:
    d = _domain()
    assert await catalog.save_catalog_items([_rec(d, "product", "sale"), _rec(d, "collection", "sale")]) == 2
    products, _ = await catalog.load_catalog_items(d, "product")
    collections, _ = await catalog.load_catalog_items(d, "collection")
    assert [p["handle"] for p in products] == ["sale"] and [c["handle"] for c in collections] == ["sale"]


async def test_recrawling_updates_in_place_and_preserves_first_seen(mongo_db: None) -> None:
    d = _domain()
    await catalog.save_catalog_items([_rec(d, "product", "a")])
    (before,), _ = await catalog.load_catalog_items(d, "product")
    written = await catalog.save_catalog_items([_rec(d, "product", "a", crawl="c2")])
    assert written == 1, "unchanged re-crawl must still report the doc as addressed (matched, not modified)"
    (after,), total = await catalog.load_catalog_items(d, "product")
    assert total == 1 and after["first_seen_at"] == before["first_seen_at"] and after["last_crawl_id"] == "c2"


async def test_an_unchanged_recrawl_writes_zero_revisions_and_a_change_writes_one(mongo_db: None) -> None:
    d = _domain()
    rec = _rec(d, "product", "a", enriched=True, meta_title="Linen Shirt for Warm Days and Cool Nights | Shop")
    await catalog.save_catalog_items([rec])
    _, r1 = await catalog.save_catalog_enrichment([rec])
    _, r2 = await catalog.save_catalog_enrichment([rec])
    changed = _rec(d, "product", "a", enriched=True, meta_title="A Completely Rewritten SEO Title | Shop", crawl="c2")
    _, r3 = await catalog.save_catalog_enrichment([changed])
    assert (r1, r2, r3) == (1, 0, 1)
    revisions = await catalog.load_item_revisions(d, "product", "a")
    assert len(revisions) == 2 and revisions[0]["crawl_id"] == "c2"


async def test_items_are_returned_worst_gaps_first_and_paging_never_repeats_or_skips(mongo_db: None) -> None:
    d = _domain()
    recs = []
    for i in range(30):
        r = _rec(d, "product", f"p{i:02d}", enriched=True, meta_title=("x" * 40) if i % 3 else "")
        recs.append(r)
    await catalog.save_catalog_items(recs)
    await catalog.save_catalog_enrichment(recs)
    seen: list[str] = []
    offset = 0
    while True:
        page, total = await catalog.load_catalog_items(d, "product", limit=7, offset=offset)
        if not page:
            break
        seen += [p["handle"] for p in page]
        offset += 7
    assert total == 30 and len(seen) == 30 and len(set(seen)) == 30
    first, _ = await catalog.load_catalog_items(d, "product", limit=1)
    assert first[0]["seo_gap_count"] == max(r["seo_gap_count"] for r in recs)


async def test_only_gaps_and_flag_filters_narrow_the_read(mongo_db: None) -> None:
    d = _domain()
    clean = _rec(d, "product", "clean", enriched=True, meta_title="A Perfectly Sized Meta Title Right Here | Shop")
    gappy = _rec(d, "product", "gappy", enriched=True, meta_title="")
    await catalog.save_catalog_items([clean, gappy])
    await catalog.save_catalog_enrichment([clean, gappy])
    items, total = await catalog.load_catalog_items(d, "product", only_gaps=True)
    assert total == 1 and items[0]["handle"] == "gappy"
    items, total = await catalog.load_catalog_items(d, "product", flag="meta_title_missing")
    assert total == 1
    counts = await catalog.flag_counts(d)
    assert counts.get("meta_title_missing") == 1


async def test_internal_fields_are_projected_out_of_list_reads(mongo_db: None) -> None:
    d = _domain()
    rec = _rec(d, "product", "a", enriched=True, meta_title="Some Title Long Enough To Pass Checks | Shop")
    await catalog.save_catalog_items([rec])
    await catalog.save_catalog_enrichment([rec])
    (doc,), _ = await catalog.load_catalog_items(d, "product")
    assert "_id" not in doc and "content_hash" not in doc and "description" not in doc
    detail = await catalog.load_catalog_item(d, "product", "a")
    assert detail is not None and detail["description"] == "d" and "content_hash" not in detail


async def test_another_store_in_the_same_database_is_not_returned(mongo_db: None) -> None:
    a, b = _domain(), _domain()
    await catalog.save_catalog_items([_rec(a, "product", "x"), _rec(b, "product", "y")])
    items, total = await catalog.load_catalog_items(a, "product")
    assert total == 1 and items[0]["handle"] == "x"
