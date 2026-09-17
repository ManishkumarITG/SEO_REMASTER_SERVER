"""run_audit's failure handling: nothing is left stuck, and nothing runs unbounded.

Uses the dev database like the other DB-touching tests, with a fresh random URL
per audit so rows never collide.
"""

from __future__ import annotations

import asyncio

import pytest

import workers.tasks as tasks
from db.session import async_session_factory, init_db
from models.db import Audit
from services.crawler.fetcher import FetchError
from tests.factories import GOOD_HTML, make_page


async def _new_audit(status: str = "pending") -> str:
    await init_db()
    audit = Audit(url="https://example.com/", status=status)
    async with async_session_factory() as session:
        session.add(audit)
        await session.commit()
        return audit.id


async def _status_of(audit_id: str) -> tuple[str, dict | None]:
    async with async_session_factory() as session:
        row = await session.get(Audit, audit_id)
        assert row is not None
        return row.status, row.results_json


async def test_unfetchable_url_is_recorded_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    audit_id = await _new_audit()

    async def boom(url: str):
        raise FetchError("Timed out after 15s fetching https://example.com/")

    monkeypatch.setattr(tasks, "fetch_page", boom)
    await tasks.run_audit(audit_id)

    status, results = await _status_of(audit_id)
    assert status == "failed"
    assert "Timed out" in results["error"]


async def test_unexpected_crash_is_recorded_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An audit that blows up must still land as 'failed', never stay 'running'."""
    audit_id = await _new_audit()

    async def boom(url: str):
        raise RuntimeError("selectolax exploded")

    monkeypatch.setattr(tasks, "fetch_page", boom)
    await tasks.run_audit(audit_id)

    status, results = await _status_of(audit_id)
    assert status == "failed"
    assert "RuntimeError" in results["error"]


async def test_missing_audit_id_does_not_raise() -> None:
    await tasks.run_audit("does-not-exist")  # must not raise


async def test_orphaned_audits_are_failed_on_startup() -> None:
    running = await _new_audit("running")
    pending = await _new_audit("pending")

    await tasks.reconcile_orphaned_audits()

    for audit_id in (running, pending):
        status, results = await _status_of(audit_id)
        assert status == "failed"
        assert "restarted" in results["error"]


async def test_completed_audits_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    audit_id = await _new_audit()
    page = make_page(GOOD_HTML)

    async def ok(url: str):
        return page

    monkeypatch.setattr(tasks, "fetch_page", ok)
    monkeypatch.setattr(tasks, "get_analyzers", list)  # no analyzers: fast, no network
    await tasks.run_audit(audit_id)
    assert (await _status_of(audit_id))[0] == "complete"

    await tasks.reconcile_orphaned_audits()
    assert (await _status_of(audit_id))[0] == "complete"


async def test_concurrent_audits_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the semaphore, N submissions mean N simultaneous Lighthouse runs."""
    peak = 0
    live = 0

    async def slow(url: str):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)
            raise FetchError("done measuring")
        finally:
            live -= 1

    monkeypatch.setattr(tasks, "fetch_page", slow)
    audit_ids = [await _new_audit() for _ in range(tasks.MAX_CONCURRENT_AUDITS + 4)]

    await asyncio.gather(*(tasks.run_audit(audit_id) for audit_id in audit_ids))

    assert peak <= tasks.MAX_CONCURRENT_AUDITS
    for audit_id in audit_ids:  # every queued audit still ran, just not all at once
        assert (await _status_of(audit_id))[0] == "failed"
