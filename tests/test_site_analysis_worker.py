"""run_site_analysis: the pipeline orchestration, with crawl/GSC/schema stubbed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import workers.tasks as tasks
from db.session import async_session_factory, init_db
from models.db import SiteAnalysis, SiteCrawl
from services.site_analysis_gsc import GscResult


async def _new_analysis(**kwargs: object) -> str:
    await init_db()
    host = f"{uuid4().hex[:8]}.example.com"
    fields: dict[str, object] = {"status": "pending", "phase": "pending"}
    fields.update(kwargs)
    row = SiteAnalysis(start_url=f"https://{host}/", host=host, **fields)  # type: ignore[arg-type]
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        return row.id


async def _row(analysis_id: str) -> SiteAnalysis:
    async with async_session_factory() as session:
        row = await session.get(SiteAnalysis, analysis_id)
        assert row is not None
        return row


async def _seed_crawl(host: str, **kwargs: object) -> str:
    row = SiteCrawl(
        start_url=f"https://{host}/", host=host, status="complete",
        pages_fetched=kwargs.pop("pages_fetched", 12), avg_score=kwargs.pop("avg_score", 80.0),
        site_name="Example", templates_measured=kwargs.pop("templates_measured", 2), **kwargs,  # type: ignore[arg-type]
    )
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        return row.id


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the three external legs: crawl spawn/await, GSC, schema."""
    state: dict = {"crawl_id": None, "gsc": GscResult(connected=True, site_url="sc-domain:x",
                                                       indexed_count=8, not_indexed_count=2),
                   "schema": {"sampled": 5, "product_field_gaps": {"aggregateRating": 3, "review": 3}}}

    async def spawn(analysis_id, start_url, options, user_id):
        host = start_url.split("/")[2]
        state["crawl_id"] = await _seed_crawl(host)
        return state["crawl_id"]

    async def await_crawl(crawl_id):
        async with async_session_factory() as session:
            return await session.get(SiteCrawl, crawl_id)

    async def load_pages(crawl_id):
        return [{"url": "https://s/p1", "template": "product", "status_code": 200}]

    async def collect_gsc(user_id, host, pages):
        return state["gsc"]

    async def sample_schema(pages, *, user_agent):
        return state["schema"]

    monkeypatch.setattr(tasks, "_spawn_crawl", spawn)
    monkeypatch.setattr(tasks, "_await_crawl", await_crawl)
    monkeypatch.setattr(tasks, "_load_crawl_pages", load_pages)
    # These are imported inside run_site_analysis, so patch at the source module.
    import services.site_analysis_gsc as gsc_mod
    import services.site_analysis_schema as schema_mod

    monkeypatch.setattr(gsc_mod, "collect_gsc", collect_gsc)
    monkeypatch.setattr(schema_mod, "sample_schema", sample_schema)
    return state


async def test_a_full_analysis_runs_crawl_then_gsc_then_schema() -> None:
    analysis_id = await _new_analysis()
    await tasks.run_site_analysis(analysis_id)
    row = await _row(analysis_id)
    assert row.status == "complete"
    assert row.phase == "done"
    assert row.crawl_id is not None
    assert row.pages_fetched == 12 and row.avg_score == 80.0
    assert row.gsc_connected is True and row.indexed_count == 8
    assert row.schema_gap_count == 6  # 3 + 3 product field gaps
    assert row.result_json["gsc"]["site_url"] == "sc-domain:x"
    assert row.result_json["schema"]["sampled"] == 5


async def test_a_crawl_that_could_not_start_fails_the_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_crawl(analysis_id, start_url, options, user_id):
        return None

    monkeypatch.setattr(tasks, "_spawn_crawl", no_crawl)
    analysis_id = await _new_analysis()
    await tasks.run_site_analysis(analysis_id)
    row = await _row(analysis_id)
    assert row.status == "failed"
    assert "could not be crawled" in (row.error_message or "")


async def test_a_crawl_with_no_pages_fails_the_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty_crawl(crawl_id):
        async with async_session_factory() as session:
            row = await session.get(SiteCrawl, crawl_id)
            row.pages_fetched = 0
            await session.commit()
            return row

    monkeypatch.setattr(tasks, "_await_crawl", empty_crawl)
    analysis_id = await _new_analysis()
    await tasks.run_site_analysis(analysis_id)
    assert (await _row(analysis_id)).status == "failed"


async def test_gsc_being_absent_still_completes_the_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """GSC is best-effort: no connection must not fail the pipeline."""
    import services.site_analysis_gsc as gsc_mod

    async def no_gsc(user_id, host, pages):
        return GscResult(error="no verified property matches host")

    monkeypatch.setattr(gsc_mod, "collect_gsc", no_gsc)
    analysis_id = await _new_analysis()
    await tasks.run_site_analysis(analysis_id)
    row = await _row(analysis_id)
    assert row.status == "complete"
    assert row.gsc_connected is False
    assert any("Search Console" in n for n in (row.notes or []))


async def test_a_partial_crawl_makes_the_analysis_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    async def partial_crawl(crawl_id):
        async with async_session_factory() as session:
            row = await session.get(SiteCrawl, crawl_id)
            row.status = "partial"
            await session.commit()
            return row

    monkeypatch.setattr(tasks, "_await_crawl", partial_crawl)
    analysis_id = await _new_analysis()
    await tasks.run_site_analysis(analysis_id)
    assert (await _row(analysis_id)).status == "partial"


# --- scheduling and reconciliation -----------------------------------------------------


async def test_a_stop_during_the_crawl_is_forwarded_to_the_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_await_crawl` blocks on the crawl; the side watcher must relay a stop meanwhile."""
    forwarded: list[str] = []
    monkeypatch.setattr(tasks, "request_site_crawl_stop", lambda cid: forwarded.append(cid) or True)
    monkeypatch.setattr(tasks, "_STOP_WATCH_INTERVAL_S", 0.01)
    analysis_id = await _new_analysis()

    async def running_crawl(crawl_id):
        # The crawl is "in flight": the user asks the analysis to stop right now.
        # (Recorded directly: request_site_analysis_stop only accepts ids of
        # analyses scheduled through schedule_site_analysis, and this one runs inline.)
        tasks._analysis_stop_requested.add(analysis_id)
        for _ in range(100):  # give the watcher a moment; it should relay almost at once
            if forwarded:
                break
            await asyncio.sleep(0.01)
        async with async_session_factory() as session:
            return await session.get(SiteCrawl, crawl_id)

    monkeypatch.setattr(tasks, "_await_crawl", running_crawl)
    try:
        await tasks.run_site_analysis(analysis_id)
    finally:
        tasks._analysis_stop_requested.discard(analysis_id)
    row = await _row(analysis_id)
    assert forwarded, "the crawl was never told to stop while it was running"
    assert row.status == "stopped"


async def test_scheduling_runs_the_analysis_detached() -> None:
    analysis_id = await _new_analysis()
    tasks.schedule_site_analysis(analysis_id)
    assert tasks.site_analysis_is_running(analysis_id)
    await tasks.wait_for_site_analysis(analysis_id)
    assert (await _row(analysis_id)).status == "complete"
    assert not tasks.site_analysis_is_running(analysis_id)


async def test_reconcile_fails_a_stale_analysis_but_not_a_fresh_one() -> None:
    stale = await _new_analysis(
        status="running", heartbeat_at=datetime.now(UTC) - timedelta(seconds=tasks.ANALYSIS_STALE_AFTER_S + 60)
    )
    fresh = await _new_analysis(status="running", heartbeat_at=datetime.now(UTC))
    await tasks.reconcile_orphaned_analyses()
    assert (await _row(stale)).status == "failed"
    assert (await _row(fresh)).status == "running"


async def test_a_missing_analysis_row_is_a_no_op() -> None:
    await init_db()
    await tasks.run_site_analysis("does-not-exist")  # must not raise
