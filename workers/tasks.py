"""Audit execution.

run_audit(audit_id) is the single entry point for background work, so swapping
FastAPI BackgroundTasks for ARQ / Celery later is a change to this file only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import update

from db.session import async_session_factory
from exceptions.crawler import FetchError
from models.db import Audit
from services.analyzers.registry import get_analyzers
from services.crawler.fetcher import fetch_page
from services.reports.scoring import build_report

logger = logging.getLogger("seo.worker")

#: Ceiling on audits running at once. Every audit fetches a page and fans out to
#: analyzers that each make their own outbound call (PageSpeed runs a full
#: Lighthouse pass, the LLM and Search Console calls are slow too), so N queued
#: audits means N concurrent Lighthouse runs. BackgroundTasks applies no limit of
#: its own, so a handful of simultaneous submissions would otherwise saturate the
#: event loop and the outbound connection pool. Queued audits wait here instead.
MAX_CONCURRENT_AUDITS = 4
_slot = asyncio.Semaphore(MAX_CONCURRENT_AUDITS)


async def run_audit(audit_id: str) -> None:
    async with _slot, async_session_factory() as session:
        audit = await session.get(Audit, audit_id)
        if audit is None:
            logger.error("Audit %s not found", audit_id)
            return
        audit.status = "running"
        await session.commit()

        try:
            page = await fetch_page(audit.url)
            page.user_id = audit.user_id  # enables per-user analyzers (Search Console)
            analyzers = get_analyzers()
            logger.info(
                "Audit %s: running %d analyzers against %s", audit_id, len(analyzers), page.final_url
            )
            results = list(await asyncio.gather(*(analyzer.analyze(page) for analyzer in analyzers)))
            report = build_report(results)
            report["page"] = {
                "requested_url": page.requested_url,
                "final_url": page.final_url,
                "status_code": page.status_code,
                "load_time_ms": page.load_time_ms,
                "fetched_at": page.fetched_at.isoformat(),
                "redirect_chain": page.redirect_chain,
                "truncated": page.truncated,
            }
            audit.overall_score = report["overall_score"]
            audit.results_json = report
            audit.status = "complete"
            logger.info("Audit %s complete: score=%s", audit_id, report["overall_score"])
        except FetchError as exc:
            audit.results_json = {"error": str(exc)}
            audit.status = "failed"
            logger.warning("Audit %s failed to fetch: %s", audit_id, exc)
        except Exception as exc:  # noqa: BLE001 - a crashed audit must still be recorded as failed
            logger.exception("Audit %s crashed", audit_id)
            audit.results_json = {"error": f"Internal error: {type(exc).__name__}: {exc}"}
            audit.status = "failed"

        audit.completed_at = datetime.now(UTC)
        try:
            await session.commit()
        except Exception:  # noqa: BLE001
            # Losing this write leaves the row stuck on "running" and the frontend
            # polling forever, so retry once on a clean session before giving up.
            logger.exception("Audit %s: could not persist the result - retrying once", audit_id)
            await session.rollback()
            await _mark_failed(audit_id, "The audit finished but its result could not be saved.")


async def _mark_failed(audit_id: str, message: str) -> None:
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(Audit)
                .where(Audit.id == audit_id)
                .values(
                    status="failed",
                    results_json={"error": message},
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - nothing left to do but say so
        logger.exception("Audit %s: could not even record the failure", audit_id)


async def reconcile_orphaned_audits() -> int:
    """Fail audits left mid-flight by a previous process. Called once at startup.

    BackgroundTasks live in the web process: a restart (including every --reload)
    abandons whatever was running, and those rows keep status "pending"/"running"
    forever while the frontend polls them. Nothing will ever finish them, so the
    honest state is "failed".
    """
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                update(Audit)
                .where(Audit.status.in_(("pending", "running")))
                .values(
                    status="failed",
                    results_json={"error": "The server restarted before this audit finished."},
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
            count = result.rowcount or 0
    except Exception:  # noqa: BLE001 - startup must not fail over housekeeping
        logger.exception("Could not reconcile orphaned audits")
        return 0
    if count:
        logger.info("Marked %d audit(s) abandoned by a previous run as failed", count)
    return count
