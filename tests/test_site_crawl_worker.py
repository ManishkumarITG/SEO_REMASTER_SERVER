"""run_site_crawl: status transitions, orphan marking, degradation, reconciliation.

Mirrors test_store_crawl_worker.py. The probe, the crawl generator and MongoDB
are stubbed on the `tasks` module (they are imported at its top for exactly this
reason), so nothing reaches the network or MongoDB. Rows live in the real dev
SQLite with fresh ids per test.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import workers.tasks as tasks
from db.session import async_session_factory, init_db
from exceptions.crawler import FetchError
from exceptions.stores import RobotsDisallowed
from models.db import SiteCrawl
from services.crawler.models import CrawledPage
from services.crawler.robots import RobotsRules
from services.crawler.site_crawl import SiteProbe

HOST = "site.example.com"
ORIGIN = f"https://{HOST}"


async def _new_crawl(status: str = "pending", **kwargs: object) -> str:
    await init_db()
    host = f"{uuid4().hex[:8]}.example.com"
    row = SiteCrawl(
        start_url=f"https://{host}/", host=host, status=status,
        max_pages=50, max_depth=3, **kwargs,  # type: ignore[arg-type]
    )
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        return row.id


async def _row(crawl_id: str) -> SiteCrawl:
    async with async_session_factory() as session:
        row = await session.get(SiteCrawl, crawl_id)
        assert row is not None
        return row


@asynccontextmanager
async def _fake_client(**_: object):
    yield None


def _page(url: str, **kwargs: object) -> CrawledPage:
    return CrawledPage(url=url, status_code=kwargs.pop("status_code", 200), **kwargs)  # type: ignore[arg-type]


#: Every page_hook crawl_site was handed, so a test can assert one was supplied.
seen_hooks: list[object] = []


def _install_crawl(monkeypatch: pytest.MonkeyPatch, pages: list[CrawledPage], **stat_values: object) -> None:
    """Stub crawl_site with a generator that yields `pages` and sets `stats`."""
    seen_hooks.clear()

    async def fake_crawl_site(client, start_url, *, robots=None, limits=None, gate=None,
                              stats=None, extra_seeds=(), page_hook=None, should_stop=None):
        seen_hooks.append(page_hook)
        for page in pages:
            yield page
        if stats is not None:
            stats.fetched = sum(1 for p in pages if p.fetched)
            stats.ok = sum(1 for p in pages if p.ok)
            stats.discovered = len(pages)
            for name, value in stat_values.items():
                setattr(stats, name, value)

    monkeypatch.setattr(tasks, "crawl_site", fake_crawl_site)


@pytest.fixture(autouse=True)
def _stub_everything(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    monkeypatch.setattr(tasks, "crawl_client", _fake_client)
    saved: list[dict] = []
    marked: list[list[str]] = []

    async def probe(client, url):
        host = url.split("/")[2]
        return SiteProbe(start_url=url, origin=f"https://{host}", host=host,
                         robots=RobotsRules.permissive(), site_name="Example Site")

    async def save_pages(records):
        saved.extend(records)
        return len(records)

    async def mark_orphans(crawl_id, urls):
        marked.append(list(urls))
        return len(urls)

    async def build_template_reports(pages, **kwargs):
        # The real one calls PageSpeed Insights, which is a 10-30s network round
        # trip per template. No test may reach it.
        return [{"template": "other", "page_count": len(pages), "representative_url": "",
                 "median_words": 0, "sample_urls": [], "lighthouse": None, "error": ""}]

    async def save_template_reports(crawl_id, records):
        return len(records)

    monkeypatch.setattr(tasks, "probe_site", probe)
    monkeypatch.setattr(tasks, "save_pages", save_pages)
    monkeypatch.setattr(tasks, "mark_orphans", mark_orphans)
    monkeypatch.setattr(tasks, "build_template_reports", build_template_reports)
    monkeypatch.setattr(tasks, "save_template_reports", save_template_reports)
    _install_crawl(monkeypatch, [])
    saved.clear()
    return saved


# --- the happy path -------------------------------------------------------------------


async def test_a_finished_crawl_is_complete_with_its_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [
        _page(f"{ORIGIN}/", title="Home", meta_description="d", outlinks=[f"{ORIGIN}/a"]),
        _page(f"{ORIGIN}/a", title="A", meta_description="d"),
    ])
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "complete"
    assert row.pages_fetched == 2 and row.pages_ok == 2
    assert row.pages_persisted == 2 and row.persistence_ok is True
    assert row.completed_at is not None and row.started_at is not None
    assert row.site_name == "Example Site"


async def test_pages_are_written_with_the_crawl_id_and_host(monkeypatch: pytest.MonkeyPatch, _stub_everything) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/", title="Home")])
    await tasks.run_site_crawl(crawl_id)
    (doc,) = _stub_everything
    assert doc["crawl_id"] == crawl_id
    assert doc["host"] == (await _row(crawl_id)).host
    assert doc["orphan"] is False  # set for real only once the graph is known
    assert doc["title"] == "Home"


async def test_the_headline_findings_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [
        _page(f"{ORIGIN}/", title="Home", meta_description="d"),
        _page(f"{ORIGIN}/a", title="", meta_description=""),
        _page(f"{ORIGIN}/hidden", title="H", meta_description="d", noindex=True),
        _page(f"{ORIGIN}/gone", status_code=404),
    ])
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.missing_title_count == 1
    assert row.missing_description_count == 1
    assert row.noindex_count == 1
    assert row.broken_page_count == 1


# --- orphans ----------------------------------------------------------------------------


async def test_a_page_nothing_links_to_is_marked_an_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    marked: list[list[str]] = []

    async def mark_orphans(cid, urls):
        marked.append(list(urls))
        return len(urls)

    monkeypatch.setattr(tasks, "mark_orphans", mark_orphans)
    host = (await _row(crawl_id)).host
    origin = f"https://{host}"
    _install_crawl(monkeypatch, [
        _page(f"{origin}/", outlinks=[f"{origin}/a"]),
        _page(f"{origin}/a"),
        _page(f"{origin}/lonely"),
    ])
    await tasks.run_site_crawl(crawl_id)
    assert marked == [[f"{origin}/lonely"]]
    assert (await _row(crawl_id)).orphan_count == 1


async def test_the_seed_is_never_reported_as_an_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing links to the home page from inside the site, and that is normal."""
    crawl_id = await _new_crawl()
    host = (await _row(crawl_id)).host
    origin = f"https://{host}"
    _install_crawl(monkeypatch, [_page(f"{origin}/", outlinks=[f"{origin}/a"]), _page(f"{origin}/a")])
    await tasks.run_site_crawl(crawl_id)
    assert (await _row(crawl_id)).orphan_count == 0


async def test_a_skipped_url_is_never_an_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    """It was never fetched, so "nothing links to it" says nothing about it."""
    crawl_id = await _new_crawl()
    host = (await _row(crawl_id)).host
    origin = f"https://{host}"
    _install_crawl(monkeypatch, [
        _page(f"{origin}/"),
        CrawledPage(url=f"{origin}/private", skipped="robots"),
    ])
    await tasks.run_site_crawl(crawl_id)
    assert (await _row(crawl_id)).orphan_count == 0


# --- partial and failed outcomes ----------------------------------------------------------


async def test_hitting_the_page_budget_finishes_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/")], hit_page_budget=True, aborted_reason="page budget")
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "partial"
    assert row.hit_page_budget is True
    assert row.aborted_reason == "page budget"


async def test_a_depth_limited_crawl_finishes_partial_and_says_how_much_was_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/")], beyond_depth=42)
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "partial" and row.beyond_depth == 42


async def test_fetching_nothing_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [])
    await tasks.run_site_crawl(crawl_id)
    assert (await _row(crawl_id)).status == "failed"


async def test_a_robots_disallowed_site_fails_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()

    async def probe(client, url):
        raise RobotsDisallowed(url)

    monkeypatch.setattr(tasks, "probe_site", probe)
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "failed"
    assert "robots.txt" in (row.error_message or "")


async def test_an_unreachable_site_fails_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()

    async def probe(client, url):
        raise FetchError("connection refused")

    monkeypatch.setattr(tasks, "probe_site", probe)
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "failed" and "connection refused" in (row.error_message or "")


async def test_an_unexpected_error_becomes_a_failed_row_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawl_id = await _new_crawl()

    async def boom(client, start_url, **_):
        raise RuntimeError("something odd")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(tasks, "crawl_site", boom)
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "failed" and "RuntimeError" in (row.error_message or "")


async def test_a_missing_row_is_a_no_op() -> None:
    await init_db()
    await tasks.run_site_crawl("does-not-exist")  # must not raise


# --- degradation ---------------------------------------------------------------------------


async def test_mongo_being_down_does_not_fail_the_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crawl ran; the pages were not kept. persistence_ok says exactly that."""
    crawl_id = await _new_crawl()

    async def save_nothing(records):
        return 0

    monkeypatch.setattr(tasks, "save_pages", save_nothing)
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/", title="Home", meta_description="d")])
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.status == "complete"
    assert row.pages_persisted == 0 and row.persistence_ok is False


# --- reconciliation ---------------------------------------------------------------------------


async def test_reconcile_fails_crawls_left_running_by_a_dead_process() -> None:
    stuck = await _new_crawl(status="crawling")  # no heartbeat at all
    done = await _new_crawl(status="complete")
    await tasks.reconcile_orphaned_site_crawls()
    assert (await _row(stuck)).status == "failed"
    assert "restarted" in ((await _row(stuck)).error_message or "")
    assert (await _row(done)).status == "complete"  # terminal rows are untouched


async def test_reconcile_leaves_a_crawl_that_is_still_alive_alone() -> None:
    """The regression that killed a live crawl: a second process starting up.

    Reconciliation used to fail every non-terminal row, so merely launching
    another server - even one that died on a port clash a second later - marked a
    healthy running crawl as failed.
    """
    alive = await _new_crawl(status="crawling", heartbeat_at=datetime.now(UTC))
    await tasks.reconcile_orphaned_site_crawls()
    row = await _row(alive)
    assert row.status == "crawling"
    assert row.error_message is None


async def test_reconcile_claims_a_crawl_whose_heartbeat_went_quiet() -> None:
    stale = await _new_crawl(
        status="crawling",
        heartbeat_at=datetime.now(UTC) - timedelta(seconds=tasks.SITE_CRAWL_STALE_AFTER_S + 60),
    )
    await tasks.reconcile_orphaned_site_crawls()
    assert (await _row(stale)).status == "failed"


async def test_a_running_crawl_records_a_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/", title="Home")])
    await tasks.run_site_crawl(crawl_id)
    assert (await _row(crawl_id)).heartbeat_at is not None


def test_site_page_record_carries_the_crawl_identity() -> None:
    record = tasks.site_page_record(_page("https://s/x", title="T"), crawl_id="c1", host="s")
    assert record["crawl_id"] == "c1" and record["host"] == "s" and record["url"] == "https://s/x"
    assert record["orphan"] is False
    assert "outlinks" in record  # the graph is persisted, not just counted


# --- analysis wiring -------------------------------------------------------------------


async def test_the_crawl_is_given_an_analyzer_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoring only works from inside the crawl, so the hook must actually be passed."""
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/", title="Home")])
    await tasks.run_site_crawl(crawl_id)
    assert seen_hooks and seen_hooks[0] is not None


async def test_turning_analyzers_off_passes_no_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    monkeypatch.setattr(tasks, "bulk_analyzers", lambda names: [])
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/", title="Home")])
    await tasks.run_site_crawl(crawl_id)
    assert seen_hooks == [None]


async def test_analysis_totals_reach_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_id = await _new_crawl()
    _install_crawl(
        monkeypatch, [_page(f"{ORIGIN}/", title="Home")],
        analyzed=3, score_total=240.0, critical_findings=2, warning_findings=5,
    )
    await tasks.run_site_crawl(crawl_id)
    row = await _row(crawl_id)
    assert row.pages_analyzed == 3
    assert row.avg_score == 80.0  # 240 / 3, computed once at the end
    assert row.critical_findings == 2 and row.warning_findings == 5


# --- scheduling and stopping -----------------------------------------------------------------


async def test_a_scheduled_crawl_runs_detached_from_whoever_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the crawl must not be tied to the request that started it."""
    crawl_id = await _new_crawl()
    _install_crawl(monkeypatch, [_page(f"{ORIGIN}/", title="Home")])

    tasks.schedule_site_crawl(crawl_id)
    assert tasks.site_crawl_is_running(crawl_id)
    await tasks.wait_for_site_crawl(crawl_id)

    assert (await _row(crawl_id)).status == "complete"
    assert not tasks.site_crawl_is_running(crawl_id)  # the registry cleans itself up


async def test_scheduling_the_same_crawl_twice_starts_one_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawl_id = await _new_crawl()
    started: list[str] = []

    async def counting_run(cid: str) -> None:
        started.append(cid)
        await asyncio.sleep(0)

    monkeypatch.setattr(tasks, "run_site_crawl", counting_run)
    tasks.schedule_site_crawl(crawl_id)
    tasks.schedule_site_crawl(crawl_id)
    await tasks.wait_for_site_crawl(crawl_id)
    assert started == [crawl_id]


async def test_stopping_a_crawl_keeps_what_it_already_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop is cooperative: cancelling the task would lose the fetched pages."""
    crawl_id = await _new_crawl()
    saved: list[dict] = []

    async def save_pages(records):
        saved.extend(records)
        return len(records)

    async def slow_crawl(client, start_url, *, robots=None, limits=None, gate=None,
                         stats=None, extra_seeds=(), page_hook=None, should_stop=None):
        for i in range(10):
            if should_stop is not None and should_stop():
                if stats is not None:
                    stats.stopped_by_user = True
                    stats.aborted_reason = "stopped by user"
                break
            yield _page(f"{ORIGIN}/p{i}", title=f"P{i}")
            if i == 2:
                tasks.request_site_crawl_stop(crawl_id)
            await asyncio.sleep(0)
        if stats is not None:
            stats.fetched = len(saved) or 3
            stats.ok = stats.fetched

    monkeypatch.setattr(tasks, "save_pages", save_pages)
    monkeypatch.setattr(tasks, "crawl_site", slow_crawl)

    tasks.schedule_site_crawl(crawl_id)
    await tasks.wait_for_site_crawl(crawl_id)

    row = await _row(crawl_id)
    assert row.status == "stopped"
    assert row.aborted_reason == "stopped by user"
    assert len(saved) >= 3  # the pages fetched before the stop were kept


def test_stopping_a_crawl_that_is_not_running_says_so() -> None:
    assert tasks.request_site_crawl_stop("no-such-crawl") is False
