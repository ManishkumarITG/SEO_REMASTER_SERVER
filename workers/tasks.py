"""Background job execution: audits and store catalog crawls.

run_audit(audit_id) and run_store_crawl(crawl_id) are the only entry points for
background work, so swapping FastAPI BackgroundTasks for ARQ / Celery later is a
change to this file only. Both take just the row id: the worker re-fetches the
row on its own session and nothing else crosses the boundary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from config import get_settings
from db.session import async_session_factory
from db.store_catalog import save_catalog_enrichment, save_catalog_items
from exceptions.crawler import FetchError
from exceptions.stores import CatalogUnavailable, RobotsDisallowed
from models.db import Audit, StoreCrawl
from services.analyzers.registry import get_analyzers
from services.crawler.fetcher import fetch_page
from services.crawler.http_client import crawl_client
from services.reports.scoring import build_report
from services.stores.discovery import discover_catalog, probe_store
from services.stores.enrichment import BLOCKED_HOST_CONCURRENCY, HostGate, enrich_head_meta
from services.stores.models import CatalogItem, EnrichmentStats
from services.stores.seo_gaps import build_catalog_record, catalog_gaps, item_gaps

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


# --- Store crawls ---------------------------------------------------------------------

#: Ceiling on catalog crawls running at once. SEPARATE from MAX_CONCURRENT_AUDITS
#: on purpose: one crawl is minutes of sustained outbound fetching against one
#: store, an audit is seconds. Sharing a semaphore would let two crawls occupy
#: every audit slot while a user stares at a spinner, and in reverse let queued
#: audits starve a crawl whose progress bar is on screen. The number is 2 because
#: the real ceiling is sockets: 2 crawls x 8 enrichment workers = 16 on top of
#: whatever the audits are doing, inside http_client.DEFAULT_LIMITS (24).
MAX_CONCURRENT_CRAWLS = 2
_crawl_slot = asyncio.Semaphore(MAX_CONCURRENT_CRAWLS)

#: Phase 1 budget. Even a 10,000-product store is ~40 sequential JSON pages.
DISCOVERY_TIMEOUT_SECONDS = 120
#: Phase 2 budget. Past this the crawl finishes "partial" with the rest unenriched.
CRAWL_TIMEOUT_SECONDS = 20 * 60
#: Flush progress counters every N items or every T seconds, whichever first.
#: 25 items is ~5 s of work at the measured rate, so a 1,000-item crawl does ~40
#: UPDATEs instead of 1,000 against SQLite's single writer; 2 s keeps the number
#: moving on a slow store. The same boundary batches the MongoDB bulk_write.
FLUSH_EVERY_ITEMS = 25
FLUSH_EVERY_SECONDS = 2.0

_NON_TERMINAL_CRAWL = ("pending", "discovering", "enriching")


class _ProgressTracker:
    """In-memory crawl counters, flushed to SQL on a schedule.

    Absolute values, never `column + n`: a lost flush self-heals on the next one.
    `bump()` is synchronous and only the single collecting loop awaits
    `maybe_flush()`, so ten in-flight fetches can never fire ten UPDATEs at once.
    Progress writes are cosmetic - they log and never raise.
    """

    __slots__ = ("crawl_id", "products", "collections", "failures", "_since_flush", "_last_flush")

    def __init__(self, crawl_id: str) -> None:
        self.crawl_id = crawl_id
        self.products = 0
        self.collections = 0
        self.failures = 0
        self._since_flush = 0
        self._last_flush = time.monotonic()

    @property
    def enriched(self) -> int:
        return self.products + self.collections

    def bump(self, kind: str, *, ok: bool) -> None:
        if ok:
            if kind == "product":
                self.products += 1
            else:
                self.collections += 1
        else:
            self.failures += 1
        self._since_flush += 1

    async def maybe_flush(self) -> None:
        if self._since_flush >= FLUSH_EVERY_ITEMS or time.monotonic() - self._last_flush >= FLUSH_EVERY_SECONDS:
            await self.flush()

    async def flush(self) -> None:
        self._since_flush = 0
        self._last_flush = time.monotonic()
        try:
            async with async_session_factory() as session:
                await session.execute(
                    update(StoreCrawl)
                    .where(StoreCrawl.id == self.crawl_id)
                    .values(
                        products_enriched=self.products,
                        collections_enriched=self.collections,
                        enrich_failures=self.failures,
                    )
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - progress is cosmetic; the crawl must not die for it
            logger.warning("Store crawl %s: progress flush failed: %s", self.crawl_id, exc)


async def _begin_crawl(crawl_id: str) -> tuple[str, str] | None:
    """Flip to "discovering" and return (store_url, store_domain). Own session, opened AND closed.

    The worker never holds a SQLAlchemy session across the phases: a session held
    open for a 3-minute network-bound phase is an open SQLite transaction, which
    is how every other request in the process starts seeing "database is locked".
    """
    async with async_session_factory() as session:
        row = await session.get(StoreCrawl, crawl_id)
        if row is None:
            return None
        row.status = "discovering"
        await session.commit()
        return row.store_url, row.store_domain


async def _record_discovery(crawl_id: str, *, source: str, products: int, collections: int,
                            persisted: int, store_name: str) -> None:
    async with async_session_factory() as session:
        await session.execute(
            update(StoreCrawl)
            .where(StoreCrawl.id == crawl_id)
            .values(
                status="enriching",
                source=source,
                products_total=products,
                collections_total=collections,
                items_persisted=persisted,
                store_name=store_name[:255] or None,
                discovered_at=datetime.now(UTC),
            )
        )
        await session.commit()


def _terminal_status(tracker: _ProgressTracker, attempted: int, total: int, aborted: str) -> str:
    """complete: everything attempted and nothing failed; failed: nothing enriched; else partial."""
    if total == 0:
        return "failed"
    if tracker.enriched == 0:
        return "failed"
    if tracker.failures == 0 and attempted == total and not aborted:
        return "complete"
    return "partial"


async def run_store_crawl(crawl_id: str) -> None:
    async with _crawl_slot:
        begun = await _begin_crawl(crawl_id)
        if begun is None:
            logger.error("Store crawl %s not found", crawl_id)
            return
        store_url, store_domain = begun
        settings = get_settings()

        tracker = _ProgressTracker(crawl_id)
        status = "failed"
        error: str | None = None
        persisted = revisions = 0
        persistence_ok = True
        attempted = total = skipped = 0
        source = "none"
        shop_suffix = ""
        flag_counts: dict[str, int] = {}
        stats = EnrichmentStats()
        items: list[CatalogItem] = []

        try:
            async with crawl_client(user_agent=settings.crawler_user_agent, http2=settings.crawl_http2) as client:
                async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
                    probe = await probe_store(client, store_url)
                    discovered = await discover_catalog(client, probe)
                items = discovered.items
                source = discovered.source
                total = len(items)
                logger.info(
                    "Store crawl %s: discovered %d products, %d collections via %s (%d requests)",
                    crawl_id, discovered.products_total, discovered.collections_total, source,
                    discovered.pages_fetched,
                )

                # Phase 1 persisted before enrichment starts: the catalog is readable
                # (names, descriptions, prices) while phase 2 is still running.
                records = _records(items, store_domain=store_domain, store_url=store_url,
                                   crawl_id=crawl_id, source=source)
                written = await save_catalog_items(records)
                persisted += written
                persistence_ok = written > 0
                await _record_discovery(
                    crawl_id, source=source, products=discovered.products_total,
                    collections=discovered.collections_total, persisted=written,
                    store_name=probe.origin.site_name,
                )

                # Collections first (few, high-traffic), then products newest-updated
                # first as a free proxy for "the merchant cares about this one".
                ordered = sorted(
                    items,
                    key=lambda i: (0 if i.kind == "collection" else 1, -(i.updated_at.timestamp() if i.updated_at else 0)),
                )
                cap = max(0, settings.crawl_max_enrich_items)
                to_enrich = ordered[:cap]
                skipped = len(ordered) - len(to_enrich)
                for item in ordered[cap:]:
                    item.enrich_error = ""  # deliberately not attempted -> "not_enriched" flag
                by_key = {(i.kind, i.handle): i for i in items}
                concurrency = BLOCKED_HOST_CONCURRENCY if discovered.blocked else settings.crawl_enrich_concurrency
                gate = HostGate.for_robots(probe.robots)

                async with asyncio.timeout(CRAWL_TIMEOUT_SECONDS):
                    batch: list[CatalogItem] = []
                    async for meta in enrich_head_meta(
                        client, to_enrich, robots=probe.robots, gate=gate, concurrency=concurrency,
                        deadline_s=CRAWL_TIMEOUT_SECONDS - 30, stats=stats,
                    ):
                        item = by_key[(meta.kind, meta.handle)]
                        _apply_meta(item, meta)
                        attempted += 1
                        tracker.bump(meta.kind, ok=meta.ok)
                        batch.append(item)
                        if len(batch) >= FLUSH_EVERY_ITEMS:
                            w, r = await _flush_batch(batch, store_domain=store_domain, store_url=store_url,
                                                      crawl_id=crawl_id, source=source, items=items)
                            persisted += w
                            revisions += r
                            batch = []
                        await tracker.maybe_flush()
                    if batch:
                        w, r = await _flush_batch(batch, store_domain=store_domain, store_url=store_url,
                                                  crawl_id=crawl_id, source=source, items=items)
                        persisted += w
                        revisions += r

            # Final pass: duplicates need every item's meta in hand. O(n) Counter,
            # then one more write so the duplicate flags land on the docs.
            shop_suffix = catalog_gaps(items)
            flag_counts = _count_flags(items)
            final_records = _records(items, store_domain=store_domain, store_url=store_url, crawl_id=crawl_id,
                                     source=source)
            w, r = await save_catalog_enrichment([rec for rec in final_records if rec["enriched"]])
            persisted += w
            revisions += r
            if items and persisted == 0:
                persistence_ok = False
            status = _terminal_status(tracker, attempted, len(to_enrich), stats.aborted_reason)
            if skipped and status == "complete":
                status = "partial"
            if stats.aborted_reason:
                error = f"Enrichment stopped early: {stats.aborted_reason}."
            elif skipped:
                error = f"{skipped} item(s) were not enriched (CRAWL_MAX_ENRICH_ITEMS={cap})."

        except RobotsDisallowed as exc:
            error = exc.message
            logger.info("Store crawl %s: %s", crawl_id, exc.message)
        except CatalogUnavailable as exc:
            error = exc.message
            logger.info("Store crawl %s found no catalog: %s", crawl_id, exc.message)
        except FetchError as exc:
            error = str(exc)
            logger.warning("Store crawl %s could not reach the store: %s", crawl_id, exc)
        except TimeoutError:
            status = "partial" if tracker.enriched else "failed"
            error = f"The crawl exceeded its {CRAWL_TIMEOUT_SECONDS}s budget and was stopped."
            logger.warning("Store crawl %s timed out", crawl_id)
        except Exception as exc:  # noqa: BLE001 - a crashed crawl must still be recorded as failed
            logger.exception("Store crawl %s crashed", crawl_id)
            error = f"Internal error: {type(exc).__name__}: {exc}"

        if status in ("complete", "partial") and not persistence_ok:
            error = "The catalog was crawled but could not be saved: MongoDB is unavailable."

        completed_at = datetime.now(UTC)
        try:
            async with async_session_factory() as session:
                await session.execute(
                    update(StoreCrawl)
                    .where(StoreCrawl.id == crawl_id)
                    .values(
                        status=status,
                        source=source,
                        products_enriched=tracker.products,
                        collections_enriched=tracker.collections,
                        enrich_failures=tracker.failures,
                        enrich_skipped=skipped,
                        items_persisted=persisted,
                        revisions_written=revisions,
                        persistence_ok=persistence_ok,
                        shop_suffix=shop_suffix or None,
                        flag_counts=flag_counts or None,
                        error_message=error,
                        completed_at=completed_at,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Store crawl %s: could not persist the result - retrying once", crawl_id)
            await _mark_crawl_failed(crawl_id, "The crawl finished but its result could not be saved.")
        logger.info("Store crawl %s %s: %d/%d enriched, %d persisted", crawl_id, status, tracker.enriched, total,
                    persisted)


def _apply_meta(item: CatalogItem, meta: Any) -> None:
    item.http_status = meta.http_status
    if meta.skipped_by_robots:
        item.enriched = False
        item.enrich_error = "skipped_by_robots"
        return
    if meta.error:
        item.enriched = False
        item.enrich_error = meta.error
        return
    item.enriched = True
    item.enrich_error = ""
    item.meta_title = meta.meta_title
    item.meta_description = meta.meta_description
    item.canonical_url = meta.canonical_url
    item.og_title = meta.og_title
    item.og_description = meta.og_description
    if not item.name and meta.og_title:
        # Sitemap-tier items arrive with no name; og:title is the cleanest source.
        item.name = meta.og_title
    if not item.description and meta.og_description:
        item.description = meta.og_description


def _records(items: Sequence[CatalogItem], **ctx: Any) -> list[dict[str, Any]]:
    return [build_catalog_record(item, **ctx) for item in items]


async def _flush_batch(batch: list[CatalogItem], *, items: Sequence[CatalogItem], **ctx: Any) -> tuple[int, int]:
    # Per-item flags now so the UI sees them as enrichment lands; cross-item
    # duplicate flags are added in the final pass.
    for item in batch:
        item.gaps = item_gaps(item)
    return await save_catalog_enrichment(_records(batch, **ctx))


def _count_flags(items: Sequence[CatalogItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for flag in item.gaps:
            counts[flag] = counts.get(flag, 0) + 1
    return counts


async def _mark_crawl_failed(crawl_id: str, message: str) -> None:
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(StoreCrawl)
                .where(StoreCrawl.id == crawl_id)
                .values(status="failed", error_message=message, completed_at=datetime.now(UTC))
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - nothing left to do but say so
        logger.exception("Store crawl %s: could not even record the failure", crawl_id)


async def reconcile_orphaned_crawls() -> int:
    """Fail crawls left mid-flight by a previous process. Called once at startup.

    Same reasoning as reconcile_orphaned_audits. One subtlety worth knowing: an
    orphaned crawl's phase-1 catalog SURVIVES in MongoDB (it was upserted before
    enrichment began), so the job is marked failed while a partial catalog stays
    readable. The UI should say "the last crawl was interrupted", not "no data".
    """
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                update(StoreCrawl)
                .where(StoreCrawl.status.in_(_NON_TERMINAL_CRAWL))
                .values(
                    status="failed",
                    error_message="The server restarted before this crawl finished.",
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
            count = result.rowcount or 0
    except Exception:  # noqa: BLE001 - startup must not fail over housekeeping
        logger.exception("Could not reconcile orphaned store crawls")
        return 0
    if count:
        logger.info("Marked %d store crawl(s) abandoned by a previous run as failed", count)
    return count
