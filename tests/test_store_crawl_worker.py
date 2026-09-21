"""run_store_crawl: status transitions, progress, degradation, concurrency.

Mirrors test_worker.py. Discovery, enrichment and Mongo are stubbed on the
`tasks` module (they are imported at its top for exactly this reason), so
nothing reaches the network or MongoDB. Rows live in the real dev SQLite with
fresh ids per test.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

import workers.tasks as tasks
from db.session import async_session_factory, init_db
from exceptions.crawler import FetchError
from exceptions.stores import CatalogUnavailable
from models.db import StoreCrawl
from services.crawler.robots import RobotsRules
from services.stores.models import CatalogItem, DiscoveryResult, HeadMeta, OriginInfo, ProbeResult


async def _new_crawl(status: str = "pending") -> str:
    await init_db()
    domain = f"{uuid4().hex[:8]}.example.com"
    row = StoreCrawl(store_url=f"https://{domain}/", store_domain=domain, status=status)
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        return row.id


async def _row(crawl_id: str) -> StoreCrawl:
    async with async_session_factory() as session:
        row = await session.get(StoreCrawl, crawl_id)
        assert row is not None
        return row


def _items(n_products: int, n_collections: int = 0) -> list[CatalogItem]:
    items = [CatalogItem(kind="collection", handle=f"c{i}", url=f"https://s/collections/c{i}", name=f"C{i}")
             for i in range(n_collections)]
    items += [CatalogItem(kind="product", handle=f"p{i}", url=f"https://s/products/p{i}", name=f"P{i}")
              for i in range(n_products)]
    return items


def _probe() -> ProbeResult:
    origin = OriginInfo(requested_url="https://s/", origin="https://s", final_url="https://s/", status_code=200,
                        site_name="Shop")
    return ProbeResult(origin=origin, robots=RobotsRules.permissive(), products_json_status=200,
                       collections_json_status=None, sitemap_status=None)


@asynccontextmanager
async def _fake_client(**_: object):
    yield None


@pytest.fixture(autouse=True)
def _stub_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "crawl_client", _fake_client)

    async def probe(client, url):
        return _probe()

    monkeypatch.setattr(tasks, "probe_store", probe)

    async def save_items(records):
        return len(records)

    async def save_enrich(records):
        return len(records), 0

    monkeypatch.setattr(tasks, "save_catalog_items", save_items)
    monkeypatch.setattr(tasks, "save_catalog_enrichment", save_enrich)


def _discovery(items: list[CatalogItem]):
    async def discover(client, probe, **kw):
        products = sum(1 for i in items if i.kind == "product")
        return DiscoveryResult(source="products_json", items=items, products_total=products,
                               collections_total=len(items) - products, pages_fetched=1)

    return discover


def _enricher(fail_handles: set[str] | None = None, on_item=None):
    async def enrich(client, items, **kw):
        for item in items:
            if on_item:
                await on_item(item)
            if fail_handles and item.handle in fail_handles:
                yield HeadMeta(kind=item.kind, handle=item.handle, error="HTTP 500", http_status=500)
            else:
                yield HeadMeta(kind=item.kind, handle=item.handle, http_status=200,
                               meta_title=f"{item.name} | Shop", meta_description="d" * 90)

    return enrich


async def test_a_full_crawl_completes_with_counts_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _items(5, 2)
    monkeypatch.setattr(tasks, "discover_catalog", _discovery(items))
    monkeypatch.setattr(tasks, "enrich_head_meta", _enricher())
    crawl_id = await _new_crawl()

    await tasks.run_store_crawl(crawl_id)

    row = await _row(crawl_id)
    assert row.status == "complete"
    assert (row.products_total, row.collections_total) == (5, 2)
    assert (row.products_enriched, row.collections_enriched, row.enrich_failures) == (5, 2, 0)
    assert row.discovered_at is not None and row.completed_at is not None
    assert row.persistence_ok is True and row.items_persisted > 0
    # every title was "<name> | Shop" -> the suffix is inferred and every item is a default title
    assert row.shop_suffix == "shop"
    assert row.flag_counts["meta_title_is_default"] == 7


async def test_progress_counters_advance_while_the_crawl_is_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """The polling contract: a client mid-crawl must see status=enriching and a rising count."""
    n = tasks.FLUSH_EVERY_ITEMS * 2 + 1
    items = _items(n)
    observed: list[tuple[str, int]] = []
    seen = 0

    async def peek(item: CatalogItem) -> None:
        nonlocal seen
        seen += 1
        if seen == tasks.FLUSH_EVERY_ITEMS + 2:  # just after the first flush
            row = await _row(crawl_id)
            observed.append((row.status, row.products_enriched))

    monkeypatch.setattr(tasks, "discover_catalog", _discovery(items))
    monkeypatch.setattr(tasks, "enrich_head_meta", _enricher(on_item=peek))
    crawl_id = await _new_crawl()

    await tasks.run_store_crawl(crawl_id)

    assert observed, "the stub never observed the row mid-crawl"
    status, count = observed[0]
    assert status == "enriching" and 0 < count < n


async def test_partial_enrichment_completes_as_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _items(4)
    monkeypatch.setattr(tasks, "discover_catalog", _discovery(items))
    monkeypatch.setattr(tasks, "enrich_head_meta", _enricher(fail_handles={"p1"}))
    crawl_id = await _new_crawl()
    await tasks.run_store_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "partial" and row.enrich_failures == 1 and row.products_enriched == 3


async def test_total_enrichment_failure_is_recorded_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _items(2)
    monkeypatch.setattr(tasks, "discover_catalog", _discovery(items))
    monkeypatch.setattr(tasks, "enrich_head_meta", _enricher(fail_handles={"p0", "p1"}))
    crawl_id = await _new_crawl()
    await tasks.run_store_crawl(crawl_id)
    assert (await _row(crawl_id)).status == "failed"


async def test_an_unreachable_store_is_recorded_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(client, url):
        raise FetchError("Could not reach https://s/")

    monkeypatch.setattr(tasks, "probe_store", probe)
    crawl_id = await _new_crawl()
    await tasks.run_store_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "failed" and "Could not reach" in (row.error_message or "")


async def test_a_store_with_no_catalog_is_failed_naming_what_was_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def discover(client, probe, **kw):
        raise CatalogUnavailable("https://s/", ["/products.json", "/collections.json", "/sitemap.xml"])

    monkeypatch.setattr(tasks, "discover_catalog", discover)
    crawl_id = await _new_crawl()
    await tasks.run_store_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "failed" and "/products.json" in (row.error_message or "")


async def test_an_unexpected_crash_is_recorded_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def discover(client, probe, **kw):
        raise RuntimeError("selectolax exploded")

    monkeypatch.setattr(tasks, "discover_catalog", discover)
    crawl_id = await _new_crawl()
    await tasks.run_store_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "failed" and "RuntimeError" in (row.error_message or "")


async def test_missing_crawl_id_does_not_raise() -> None:
    await tasks.run_store_crawl("does-not-exist")


async def test_a_crawl_succeeds_when_mongodb_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The degradation guarantee: the crawl ran; the catalog was not kept; the row says so."""
    items = _items(3)
    monkeypatch.setattr(tasks, "discover_catalog", _discovery(items))
    monkeypatch.setattr(tasks, "enrich_head_meta", _enricher())

    async def down_items(records):
        return 0

    async def down_enrich(records):
        return 0, 0

    monkeypatch.setattr(tasks, "save_catalog_items", down_items)
    monkeypatch.setattr(tasks, "save_catalog_enrichment", down_enrich)
    crawl_id = await _new_crawl()
    await tasks.run_store_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "complete" and row.persistence_ok is False and row.items_persisted == 0
    assert "MongoDB" in (row.error_message or "")


async def test_enrichment_stops_at_the_item_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import get_settings

    monkeypatch.setattr(get_settings(), "crawl_max_enrich_items", 3, raising=False)
    items = _items(10)
    monkeypatch.setattr(tasks, "discover_catalog", _discovery(items))
    monkeypatch.setattr(tasks, "enrich_head_meta", _enricher())
    crawl_id = await _new_crawl()
    await tasks.run_store_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "partial" and row.products_enriched == 3 and row.enrich_skipped == 7
    assert "CRAWL_MAX_ENRICH_ITEMS" in (row.error_message or "")


async def test_orphaned_crawls_are_failed_on_startup_and_terminal_ones_left_alone() -> None:
    ids = {s: await _new_crawl(s) for s in ("pending", "discovering", "enriching", "partial", "complete")}
    await tasks.reconcile_orphaned_crawls()
    for s in ("pending", "discovering", "enriching"):
        row = await _row(ids[s])
        assert row.status == "failed" and "restarted" in (row.error_message or "")
    assert (await _row(ids["partial"])).status == "partial"
    assert (await _row(ids["complete"])).status == "complete"


async def test_concurrent_crawls_are_capped_and_do_not_share_the_audit_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    peak = live = 0

    async def slow_discover(client, probe, **kw):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)
            raise FetchError("done measuring")
        finally:
            live -= 1

    monkeypatch.setattr(tasks, "discover_catalog", slow_discover)
    ids = [await _new_crawl() for _ in range(tasks.MAX_CONCURRENT_CRAWLS + 3)]

    # Saturate the crawl semaphore, then prove an audit still acquires ITS slot.
    audit_slot_acquired = asyncio.Event()

    async def audit_probe() -> None:
        async with tasks._slot:
            audit_slot_acquired.set()

    await asyncio.gather(*(tasks.run_store_crawl(i) for i in ids), audit_probe())
    assert peak <= tasks.MAX_CONCURRENT_CRAWLS
    assert audit_slot_acquired.is_set()
    for i in ids:
        assert (await _row(i)).status == "failed"
