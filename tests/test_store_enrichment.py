"""Head-only enrichment: bounded, polite, never fails the crawl for one page. No network (respx)."""

from __future__ import annotations

import asyncio

import httpx
import respx

import services.stores.enrichment as enrichment
from services.crawler.http_client import MAX_HEAD_BYTES, crawl_client, read_until_sentinel
from services.crawler.robots import RobotsRules
from services.stores.enrichment import HostGate, enrich_head_meta, extract_head_meta
from services.stores.models import CatalogItem, EnrichmentStats
from tests.factories import head_html

ORIGIN = "https://shop.test"


def _items(n: int) -> list[CatalogItem]:
    return [CatalogItem(kind="product", handle=f"p{i}", url=f"{ORIGIN}/products/p{i}", name=f"P{i}") for i in range(n)]


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


async def _collect(items, robots=None, **kw):
    async with crawl_client() as client:
        return [m async for m in enrich_head_meta(client, items, robots=robots or RobotsRules.permissive(), **kw)]


@respx.mock
async def test_each_item_yields_one_head_meta_with_title_and_description() -> None:
    for i in range(3):
        respx.get(f"{ORIGIN}/products/p{i}").mock(return_value=_html(head_html(f"Title {i}", f"Desc {i}")))
    metas = await _collect(_items(3))
    assert sorted(m.handle for m in metas) == ["p0", "p1", "p2"]
    assert all(m.ok and m.meta_title.startswith("Title") and m.meta_description.startswith("Desc") for m in metas)


@respx.mock
async def test_meta_is_recovered_from_a_page_far_larger_than_the_cap() -> None:
    """The <head> is read; the 600 KiB body is not."""
    respx.get(f"{ORIGIN}/products/p0").mock(return_value=_html(head_html("Big", "D", body_kib=600)))
    (meta,) = await _collect(_items(1))
    assert meta.ok and meta.meta_title == "Big" and meta.meta_description == "D"
    assert meta.truncated is False  # stopped on the sentinel, not the cap


async def test_read_until_sentinel_finds_a_sentinel_split_across_chunks() -> None:
    chunks = [b"<html><head><title>T</title></he", b"ad><body>" + b"x" * 5000]

    async def aiter_bytes():
        for c in chunks:
            yield c

    class FakeResponse:
        def aiter_bytes(self):  # noqa: D401
            return aiter_bytes()

    body, hit, truncated = await read_until_sentinel(FakeResponse(), MAX_HEAD_BYTES)  # type: ignore[arg-type]
    assert hit is True and truncated is False
    assert b"</head>" in body.lower()


async def test_read_until_sentinel_reports_truncation_when_no_sentinel_appears() -> None:
    async def aiter_bytes():
        for _ in range(10):
            yield b"y" * 1000

    class FakeResponse:
        def aiter_bytes(self):  # noqa: D401
            return aiter_bytes()

    body, hit, truncated = await read_until_sentinel(FakeResponse(), 2500)  # type: ignore[arg-type]
    assert hit is False and truncated is True and len(body) == 2500


@respx.mock
async def test_a_single_failing_page_does_not_abort_the_others() -> None:
    respx.get(f"{ORIGIN}/products/p0").mock(side_effect=httpx.ConnectError("nope"))
    respx.get(f"{ORIGIN}/products/p1").mock(return_value=_html(head_html("Fine", "D")))
    metas = {m.handle: m for m in await _collect(_items(2))}
    assert metas["p1"].ok
    assert not metas["p0"].ok and metas["p0"].error


@respx.mock
async def test_a_404_page_is_reported_not_retried() -> None:
    route = respx.get(f"{ORIGIN}/products/p0").mock(return_value=httpx.Response(404))
    (meta,) = await _collect(_items(1))
    assert meta.error == "HTTP 404" and route.call_count == 1


@respx.mock
async def test_a_5xx_is_retried_exactly_once() -> None:
    route = respx.get(f"{ORIGIN}/products/p0").mock(return_value=httpx.Response(502))
    stats = EnrichmentStats()
    await _collect(_items(1), stats=stats)
    assert route.call_count == 2 and stats.retries == 1


@respx.mock
async def test_items_disallowed_by_robots_are_skipped_without_a_request() -> None:
    route = respx.get(f"{ORIGIN}/products/p0").mock(return_value=_html(head_html("X")))
    rules = RobotsRules(disallow=["/products/*"])
    (meta,) = await _collect(_items(1), robots=rules)
    assert meta.skipped_by_robots and not route.called


@respx.mock
async def test_concurrency_never_exceeds_the_cap() -> None:
    peak = live = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return _html(head_html("T", "D"))

    respx.get(url__regex=rf"{ORIGIN}/products/p\d+").mock(side_effect=slow)
    await _collect(_items(20), concurrency=4)
    assert peak <= 4


def test_a_hostile_crawl_delay_is_clamped() -> None:
    gate = HostGate.for_robots(RobotsRules(crawl_delay=600))
    assert gate.interval_s == enrichment.CRAWL_DELAY_CLAMP_S


@respx.mock
async def test_five_consecutive_429s_abort_the_pass() -> None:
    respx.get(url__regex=rf"{ORIGIN}/products/p\d+").mock(return_value=httpx.Response(429))
    stats = EnrichmentStats()
    metas = await _collect(_items(12), concurrency=1, stats=stats)
    assert stats.aborted_reason.startswith("5 consecutive")
    assert len(metas) == 12  # every item is still reported
    assert sum(1 for m in metas if m.error == "not_attempted") >= 1


def test_extract_head_meta_handles_a_truncated_document_and_decodes_entities() -> None:
    html = '<html><head><title>Caf&eacute; &amp; Co</title><meta NAME="Description" content="A &ndash; B">'
    fields = extract_head_meta(html)
    assert fields["meta_title"] == "Café & Co"
    assert fields["meta_description"] == "A – B"
