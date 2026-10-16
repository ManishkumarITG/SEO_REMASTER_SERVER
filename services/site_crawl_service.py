"""Site-wide crawl service: the HTTP-facing layer.

Create-then-schedule mirrors store_crawl_service.py and audit_service.py: the row
is committed BEFORE the background task is registered, because the worker
re-fetches it by id on its own session. Only the id crosses into the worker.

The POST runs a synchronous pre-flight (two small requests) so an unreachable
site answers 422 and a robots-blocked one 403 immediately, instead of a job that
fails two seconds later. The crawl itself happens in the worker, so endpoint
latency is independent of site size. The worker re-probes rather than receiving
the probe object, keeping the "only the id crosses" rule.

Reads: 404 for an unknown crawl id or absent page, and 503 with the SAME string
store_crawl_service uses when MongoDB is unreachable, so the frontend error
handling stays uniform. No 401 anywhere: crawls are anonymous-friendly, as the
catalog crawls are.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from pymongo.errors import PyMongoError
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from db import site_pages
from exceptions.crawler import FetchError
from exceptions.stores import RobotsDisallowed
from models.db import SiteCrawl
from schemas import (
    SiteCrawlCreatedResponse,
    SiteCrawlCreateRequest,
    SiteCrawlIssuesResponse,
    SiteCrawlListResponse,
    SiteCrawlPageDetail,
    SiteCrawlPagesResponse,
    SiteCrawlStatusResponse,
    SiteCrawlSummary,
    SiteCrawlTemplatesResponse,
    page_from_doc,
    template_from_doc,
)
from schemas.report import ReportEngine
from services import site_crawl_export, site_crawl_report
from services.crawler.http_client import crawl_client
from services.crawler.models import CrawlLimits
from services.crawler.site_crawl import probe_site
from services.crawler.urls import host_of
from services.report_engines import report_errors_as_http
from services.reports.scoring import grade_for
from workers.tasks import (
    SITE_CRAWL_STALE_AFTER_S,
    request_site_crawl_stop,
    schedule_site_crawl,
    site_crawl_is_running,
)

logger = logging.getLogger("seo.site_crawl")

_TERMINAL = ("complete", "partial", "failed", "stopped")
#: Cap on the orphan list in the issues response. The COUNT is always exact; a
#: site with 4,000 orphans has one problem, not 4,000, and the paged
#: `?issue=orphan` endpoint is there for the full list.
MAX_ORPHANS_LISTED = 200


def _mongo_503(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}")


def _progress_pct(row: SiteCrawl) -> int:
    """0-100. A fraction of the page BUDGET, because the real total is unknowable.

    A crawl discovers its own work: until it ends, nobody knows how many pages
    the site has. Showing "47 of 500 allowed" is honest; showing "47 of 47 found
    so far" would sit at 100% from the first page.

    A crawl with NO page limit has no denominator, so this stays 0 until it ends
    and the client shows an indeterminate bar. Inventing a percentage there would
    be a number with nothing behind it.
    """
    if row.status in _TERMINAL:
        return 100
    if row.status == "pending" or row.max_pages <= 0:
        return 0
    return min(99, max(1, int(100 * row.pages_fetched / row.max_pages)))


def _status_response(row: SiteCrawl) -> SiteCrawlStatusResponse:
    return SiteCrawlStatusResponse(
        crawl_id=row.id,
        start_url=row.start_url,
        host=row.host,
        site_name=row.site_name,
        status=row.status,  # type: ignore[arg-type]
        max_pages=row.max_pages,
        max_depth=row.max_depth,
        pages_discovered=row.pages_discovered,
        pages_fetched=row.pages_fetched,
        pages_ok=row.pages_ok,
        client_errors=row.client_errors,
        server_errors=row.server_errors,
        transport_errors=row.transport_errors,
        skipped_by_robots=row.skipped_by_robots,
        skipped_asset=row.skipped_asset,
        skipped_non_html=row.skipped_non_html,
        skipped_off_site=row.skipped_off_site,
        beyond_depth=row.beyond_depth,
        not_attempted=row.not_attempted,
        seeded_from_sitemap=row.seeded_from_sitemap,
        max_depth_seen=row.max_depth_seen,
        progress_pct=_progress_pct(row),
        hit_page_budget=row.hit_page_budget,
        deadline_hit=row.deadline_hit,
        aborted_reason=row.aborted_reason,
        orphan_count=row.orphan_count,
        noindex_count=row.noindex_count,
        missing_title_count=row.missing_title_count,
        missing_description_count=row.missing_description_count,
        broken_page_count=row.broken_page_count,
        # `or 0` because these four were added after the table existed and are
        # therefore nullable - see the note on models.db.SiteCrawl.
        page_types=[t for t in (row.page_types or "").split(",") if t],
        target_pages=row.target_pages or 0,
        navigation_pages=row.navigation_pages or 0,
        skipped_page_type=row.skipped_page_type or 0,
        throttled=row.throttled or 0,
        gave_up_throttled=row.gave_up_throttled or 0,
        templates_found=row.templates_found or 0,
        templates_measured=row.templates_measured or 0,
        pages_analyzed=row.pages_analyzed or 0,
        avg_score=row.avg_score,
        grade=grade_for(row.avg_score) if row.avg_score is not None else None,
        critical_findings=row.critical_findings or 0,
        warning_findings=row.warning_findings or 0,
        pages_persisted=row.pages_persisted,
        persistence_ok=row.persistence_ok,
        notes=[str(note) for note in (row.notes or [])],
        elapsed_s=row.elapsed_s,
        error=row.error_message,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _summary(row: SiteCrawl) -> SiteCrawlSummary:
    return SiteCrawlSummary(
        crawl_id=row.id,
        start_url=row.start_url,
        host=row.host,
        site_name=row.site_name,
        status=row.status,  # type: ignore[arg-type]
        pages_fetched=row.pages_fetched,
        pages_ok=row.pages_ok,
        orphan_count=row.orphan_count,
        broken_page_count=row.broken_page_count,
        avg_score=row.avg_score,
        grade=grade_for(row.avg_score) if row.avg_score is not None else None,
        page_types=[t for t in (row.page_types or "").split(",") if t],
        target_pages=row.target_pages or 0,
        created_at=row.created_at,
        completed_at=row.completed_at,
        error=row.error_message,
    )


def _requested_options(payload: SiteCrawlCreateRequest) -> dict[str, Any]:
    """The bound overrides the caller actually sent, omitting the ones left None.

    Stored on the row so the worker rebuilds the same limits, and so a report can
    say what was asked for rather than what the defaults happen to be today.
    """
    options: dict[str, Any] = {
        name: value
        for name, value in (
            ("include_subdomains", payload.include_subdomains),
            ("follow_nofollow", payload.follow_nofollow),
            ("seed_from_sitemap", payload.seed_from_sitemap),
            ("max_navigation_pages", payload.max_navigation_pages),
        )
        if value is not None
    }
    if payload.page_types:
        # Deduped and ordered here so the row, the worker and the report all show
        # the same string for the same request.
        options["page_types"] = [t for t in ("product", "collection") if t in payload.page_types]
    return options


async def _require_crawl(crawl_id: str, session: AsyncSession) -> SiteCrawl:
    row = await session.get(SiteCrawl, crawl_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Site crawl {crawl_id} not found")
    return await _reap_if_abandoned(row, session)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for timezone-aware columns."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _reap_if_abandoned(row: SiteCrawl, session: AsyncSession) -> SiteCrawl:
    """Close out a row whose worker died without it.

    Startup reconciliation alone is not enough. It only runs when a process
    starts, so a crawl killed mid-run - by a restart, or by `uvicorn --reload`
    noticing a saved file - leaves a row that says "crawling" and never changes
    again, and the client polls it forever. Checking on read is the one moment
    somebody actually cares about the answer.

    The heartbeat is what makes this safe: a crawl running in ANOTHER process is
    still writing one, so it is never reaped by this one.
    """
    if row.status in _TERMINAL or site_crawl_is_running(row.id):
        return row
    last_seen = _as_utc(row.heartbeat_at) or _as_utc(row.started_at) or _as_utc(row.created_at)
    if last_seen is None:
        return row
    if datetime.now(UTC) - last_seen < timedelta(seconds=SITE_CRAWL_STALE_AFTER_S):
        return row
    logger.info("Site crawl %s has been silent since %s - marking it interrupted", row.id, last_seen)
    row.status = "failed"
    row.error_message = (
        "The crawl stopped because the server restarted. Everything it had already "
        "found is kept. If the server is running with --reload, saving a file restarts it."
    )
    row.completed_at = datetime.now(UTC)
    await session.commit()
    return row


# --- crawls -------------------------------------------------------------------------


async def create_site_crawl(
    payload: SiteCrawlCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession,
    user_id: str | None,
  ) -> SiteCrawlCreatedResponse:
    start_url = payload.start_url
    host = host_of(start_url)
    if not host:
        raise HTTPException(status_code=422, detail="start_url must include a host")

    # Duplicate-crawl guard: a double-clicked button must not start a second
    # multi-minute job against the same site. Hand back the one already running.
    in_flight = await session.scalar(
        select(SiteCrawl)
        .where(SiteCrawl.host == host, SiteCrawl.status.notin_(_TERMINAL))
        .order_by(desc(SiteCrawl.created_at))
        .limit(1)
    )
    if in_flight is not None:
        return SiteCrawlCreatedResponse(
            crawl_id=in_flight.id, start_url=in_flight.start_url, host=host,
            status=in_flight.status, reused=True,  # type: ignore[arg-type]
        )

    settings = get_settings()
    try:
        async with crawl_client(user_agent=settings.crawler_user_agent, http2=settings.crawl_http2) as client:
            probe = await probe_site(client, start_url)
    except RobotsDisallowed as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=f"Could not reach the site: {exc}") from exc

    options = _requested_options(payload)
    limits = CrawlLimits.from_settings(
        settings,
        **({"max_pages": payload.max_pages} if payload.max_pages is not None else {}),
        **({"max_depth": payload.max_depth} if payload.max_depth is not None else {}),
        **options,
    )

    row = SiteCrawl(
        start_url=probe.start_url,
        host=probe.host,
        site_name=probe.site_name[:255] or None,
        user_id=user_id,
        status="pending",
        max_pages=limits.max_pages,
        max_depth=limits.max_depth,
        page_types=",".join(limits.page_types) or None,
        options=options or None,
    )
    session.add(row)
    await session.commit()
    # NOT a FastAPI BackgroundTask: those run inside the response and die with
    # the client connection, which is how a long crawl used to be cut short.
    schedule_site_crawl(row.id)
    return SiteCrawlCreatedResponse(
        crawl_id=row.id, start_url=row.start_url, host=row.host, status="pending"
    )


async def get_site_crawl(crawl_id: str, session: AsyncSession) -> SiteCrawlStatusResponse:
    return _status_response(await _require_crawl(crawl_id, session))


async def stop_site_crawl(crawl_id: str, session: AsyncSession) -> SiteCrawlStatusResponse:
    """Ask a running crawl to finish now, keeping everything it has already found.

    404 for an unknown id. A crawl that has already finished is NOT an error: the
    caller wanted it stopped and it is stopped, so the current row is returned.
    The stop itself is cooperative, so this returns while the crawl is still
    winding down and the client keeps polling until the status turns terminal.
    """
    row = await _require_crawl(crawl_id, session)
    if row.status in _TERMINAL:
        return _status_response(row)
    if not request_site_crawl_stop(crawl_id):
        # Running in a previous process, or already finishing. Nothing left to
        # signal, and startup reconciliation will close out a truly orphaned row.
        logger.info("Stop requested for site crawl %s, which is not running here", crawl_id)
    return _status_response(row)


def is_running(crawl_id: str) -> bool:
    return site_crawl_is_running(crawl_id)


async def list_site_crawls(
    session: AsyncSession, *, host: str | None, limit: int
) -> SiteCrawlListResponse:
    conditions = [SiteCrawl.host == host] if host else []
    total = await session.scalar(select(func.count()).select_from(SiteCrawl).where(*conditions)) or 0
    rows = await session.scalars(
        select(SiteCrawl).where(*conditions).order_by(desc(SiteCrawl.created_at)).limit(limit)
    )
    return SiteCrawlListResponse(crawls=[_summary(row) for row in rows], total=total)


# --- pages ---------------------------------------------------------------------------


async def get_site_crawl_pages(
    crawl_id: str,
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    sort: str,
    issue: str | None,
    include_navigation: bool = False,
) -> SiteCrawlPagesResponse:
    row = await _require_crawl(crawl_id, session)
    try:
        docs, total = await site_pages.load_pages(
            crawl_id, limit=limit, offset=offset, sort=sort, issue=issue,
            include_navigation=include_navigation,
        )
    except PyMongoError as exc:
        raise _mongo_503(exc) from exc
    return SiteCrawlPagesResponse(
        crawl_id=crawl_id,
        host=row.host,
        pages=[page_from_doc(doc) for doc in docs],
        total=total,
        limit=limit,
        offset=offset,
        issue=issue,
        include_navigation=include_navigation,
    )


async def get_site_crawl_page(crawl_id: str, url: str, session: AsyncSession) -> SiteCrawlPageDetail:
    await _require_crawl(crawl_id, session)
    try:
        doc = await site_pages.load_page(crawl_id, url)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"{url} is not in crawl {crawl_id}")
        inlinks = await site_pages.load_inlinks(crawl_id, url)
    except PyMongoError as exc:
        raise _mongo_503(exc) from exc
    fields = SiteCrawlPageDetail.model_fields
    known = {k: v for k, v in doc.items() if k in fields and k != "inlinks"}
    return SiteCrawlPageDetail(**known, inlinks=inlinks)


async def get_site_crawl_templates(
    crawl_id: str, session: AsyncSession
) -> SiteCrawlTemplatesResponse:
    """The per-template Lighthouse reports for one crawl.

    Returns an empty list rather than a 404 when a crawl produced none: page
    scoring and template measurement can both be switched off, and "we measured
    nothing" is a legitimate answer about a crawl that otherwise succeeded.
    """
    row = await _require_crawl(crawl_id, session)
    try:
        docs = await site_pages.load_template_reports(crawl_id)
    except PyMongoError as exc:
        raise _mongo_503(exc) from exc
    templates = [template_from_doc(doc) for doc in docs]
    return SiteCrawlTemplatesResponse(
        crawl_id=crawl_id,
        host=row.host,
        templates=templates,
        found=row.templates_found or len(templates),
        measured=row.templates_measured or sum(1 for t in templates if t.lighthouse),
    )


async def get_site_crawl_export(
    crawl_id: str, session: AsyncSession, *, fmt: str, include_navigation: bool = False
) -> tuple[AsyncIterator[bytes], str, str]:
    """-> (byte stream, media type, filename) for a finished crawl.

    Refused while a crawl is still running: an export of a half-finished crawl
    looks identical to an export of a finished one, and nothing in the file would
    say which it was.
    """
    row = await _require_crawl(crawl_id, session)
    if row.status not in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"This crawl is still {row.status}. Wait for it to finish before exporting.",
        )
    if fmt == "csv":
        return (
            site_crawl_export.stream_csv(row, include_navigation=include_navigation),
            "text/csv; charset=utf-8",
            site_crawl_export.export_filename(row, "csv"),
        )
    return (
        site_crawl_export.stream_json(row, include_navigation=include_navigation),
        "application/json; charset=utf-8",
        site_crawl_export.export_filename(row, "json"),
    )


async def generate_site_crawl_report(
    crawl_id: str, session: AsyncSession, *, engine: ReportEngine = "gemini"
) -> tuple[bytes, str]:
    """-> (HTML bytes, filename) for an AI-written audit report.

    503 when the chosen engine is not configured, 409 when the crawl is not
    finished, 502 when the engine itself fails - the report is generated live,
    so a model outage is a gateway failure, not ours.
    """
    row = await _require_crawl(crawl_id, session)
    if row.status not in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"This crawl is still {row.status}. Wait for it to finish before generating a report.",
        )
    with report_errors_as_http(engine):
        return await site_crawl_report.generate_report_html(row, engine=engine)


async def get_site_crawl_issues(crawl_id: str, session: AsyncSession) -> SiteCrawlIssuesResponse:
    row = await _require_crawl(crawl_id, session)
    try:
        counts = await site_pages.issue_counts(crawl_id)
        orphans, orphan_total = await site_pages.load_orphans(crawl_id, MAX_ORPHANS_LISTED)
    except PyMongoError as exc:
        raise _mongo_503(exc) from exc
    return SiteCrawlIssuesResponse(
        crawl_id=crawl_id,
        host=row.host,
        status=row.status,  # type: ignore[arg-type]
        counts=counts,
        orphans=orphans,
        orphan_count=orphan_total,
        orphans_truncated=orphan_total > len(orphans),
    )
