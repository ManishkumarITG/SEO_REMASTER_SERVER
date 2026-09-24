"""Site crawl routes: 202 shape, validation, polling, page reads, Mongo-down.

The pre-flight probe and the background job are stubbed on the SERVICE module.
The job stub is not optional: TestClient runs BackgroundTasks synchronously
after the response, so without it every test would run a real crawl. MongoDB
reads are stubbed on `db.site_pages`.
"""

from __future__ import annotations

import codecs
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

import db.site_pages as site_pages
import main
import services.site_crawl_service as service
from db.session import async_session_factory, init_db
from exceptions.crawler import FetchError
from exceptions.stores import RobotsDisallowed
from models.db import SiteCrawl
from services.crawler.robots import RobotsRules
from services.crawler.site_crawl import SiteProbe


@asynccontextmanager
async def _fake_client(**_: object):
    yield None


def _host() -> str:
    return f"{uuid4().hex[:8]}.example.com"


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    scheduled: list[str] = []

    def fake_schedule(crawl_id: str) -> None:
        scheduled.append(crawl_id)

    async def fake_probe(client, url):
        host = url.split("/")[2]
        return SiteProbe(start_url=url, origin=f"https://{host}", host=host,
                         robots=RobotsRules.permissive(), site_name="Example Site")

    monkeypatch.setattr(service, "schedule_site_crawl", fake_schedule)
    monkeypatch.setattr(service, "probe_site", fake_probe)
    monkeypatch.setattr(service, "crawl_client", _fake_client)
    return scheduled


def _client() -> TestClient:
    return TestClient(main.app)


async def _seed_row(**kwargs: object) -> SiteCrawl:
    await init_db()
    host = kwargs.pop("host", None) or _host()
    row = SiteCrawl(start_url=f"https://{host}/", host=host, **kwargs)  # type: ignore[arg-type]
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        return row


# --- POST -------------------------------------------------------------------------------


def test_creating_a_crawl_returns_202_and_schedules_the_job(_stubs: list[str]) -> None:
    host = _host()
    response = _client().post("/api/site-crawls", json={"start_url": f"https://{host}/"})
    assert response.status_code == 202
    body = response.json()
    assert body["host"] == host
    assert body["status"] == "pending"
    assert body["reused"] is False
    assert _stubs == [body["crawl_id"]]


def test_a_bare_host_is_accepted_and_normalised() -> None:
    host = _host()
    response = _client().post("/api/site-crawls", json={"start_url": host})
    assert response.status_code == 202
    assert response.json()["start_url"] == f"https://{host}/"


def test_a_value_that_is_not_a_url_is_rejected() -> None:
    for value in ("mailto:someone@example.com", "javascript:void(0)", "   "):
        response = _client().post("/api/site-crawls", json={"start_url": value})
        assert response.status_code == 422, value


def test_bounds_above_the_ceiling_are_rejected_at_the_edge() -> None:
    """The numbers decide how many requests we make to somebody else."""
    client = _client()
    assert client.post("/api/site-crawls", json={"start_url": _host(), "max_pages": 1_000_000}).status_code == 422
    assert client.post("/api/site-crawls", json={"start_url": _host(), "max_depth": 99}).status_code == 422


def test_requested_bounds_are_recorded_on_the_row() -> None:
    response = _client().post(
        "/api/site-crawls",
        json={"start_url": _host(), "max_pages": 12, "max_depth": 2, "include_subdomains": True},
    )
    assert response.status_code == 202
    status = _client().get(f"/api/site-crawls/{response.json()['crawl_id']}").json()
    assert status["max_pages"] == 12 and status["max_depth"] == 2


def test_a_robots_blocked_site_is_refused_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    async def blocked(client, url):
        raise RobotsDisallowed(url)

    monkeypatch.setattr(service, "probe_site", blocked)
    response = _client().post("/api/site-crawls", json={"start_url": _host()})
    assert response.status_code == 403
    assert "robots.txt" in response.json()["detail"]


def test_an_unreachable_site_is_refused_with_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreachable(client, url):
        raise FetchError("name resolution failed")

    monkeypatch.setattr(service, "probe_site", unreachable)
    response = _client().post("/api/site-crawls", json={"start_url": _host()})
    assert response.status_code == 422
    assert "name resolution failed" in response.json()["detail"]


def test_a_second_crawl_of_a_busy_host_reuses_the_one_in_flight(_stubs: list[str]) -> None:
    host = _host()
    client = _client()
    first = client.post("/api/site-crawls", json={"start_url": f"https://{host}/"}).json()
    second = client.post("/api/site-crawls", json={"start_url": f"https://{host}/about"}).json()
    assert second["crawl_id"] == first["crawl_id"]
    assert second["reused"] is True
    assert _stubs == [first["crawl_id"]]  # no second job


# --- polling -----------------------------------------------------------------------------


def test_an_unknown_crawl_id_is_404() -> None:
    assert _client().get(f"/api/site-crawls/{uuid4()}").status_code == 404


async def test_progress_is_a_fraction_of_the_page_budget() -> None:
    row = await _seed_row(status="crawling", max_pages=50, max_depth=3, pages_fetched=25)
    body = _client().get(f"/api/site-crawls/{row.id}").json()
    assert body["progress_pct"] == 50
    assert body["status"] == "crawling"


async def test_a_terminal_crawl_reads_100_percent() -> None:
    row = await _seed_row(status="partial", max_pages=50, pages_fetched=50, hit_page_budget=True)
    body = _client().get(f"/api/site-crawls/{row.id}").json()
    assert body["progress_pct"] == 100
    assert body["hit_page_budget"] is True


async def test_the_list_can_be_filtered_to_one_host() -> None:
    row = await _seed_row(status="complete", pages_fetched=4)
    await _seed_row(status="complete")
    body = _client().get("/api/site-crawls", params={"host": row.host}).json()
    assert [c["crawl_id"] for c in body["crawls"]] == [row.id]
    assert body["total"] == 1


# --- page reads ----------------------------------------------------------------------------


async def test_pages_are_returned_with_their_paging_total(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed_row(status="complete")

    async def load_pages(crawl_id, *, limit, offset, sort, issue, include_navigation):
        assert crawl_id == row.id and issue == "thin"
        assert include_navigation is False  # the answer only, by default
        return [{"url": "https://s/a", "depth": 1, "word_count": 12, "unknown_key": "ignored"}], 7

    monkeypatch.setattr(site_pages, "load_pages", load_pages)
    body = _client().get(f"/api/site-crawls/{row.id}/pages", params={"issue": "thin"}).json()
    assert body["total"] == 7
    assert body["pages"][0]["url"] == "https://s/a"
    assert body["issue"] == "thin"


async def test_an_unknown_issue_filter_is_rejected() -> None:
    row = await _seed_row(status="complete")
    response = _client().get(f"/api/site-crawls/{row.id}/pages", params={"issue": "not-a-thing"})
    assert response.status_code == 422


async def test_pages_of_an_unknown_crawl_are_404() -> None:
    assert _client().get(f"/api/site-crawls/{uuid4()}/pages").status_code == 404


async def test_mongo_being_down_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed_row(status="complete")

    async def boom(*_: object, **__: object):
        raise ServerSelectionTimeoutError("no servers")

    monkeypatch.setattr(site_pages, "load_pages", boom)
    response = _client().get(f"/api/site-crawls/{row.id}/pages")
    assert response.status_code == 503
    assert "MongoDB unavailable" in response.json()["detail"]


async def test_one_page_comes_back_with_both_halves_of_its_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _seed_row(status="complete")

    async def load_page(crawl_id, url):
        return {"url": url, "title": "A", "outlinks": ["https://s/b"], "h1": ["A"]}

    async def load_inlinks(crawl_id, url, limit=100):
        return ["https://s/"]

    monkeypatch.setattr(site_pages, "load_page", load_page)
    monkeypatch.setattr(site_pages, "load_inlinks", load_inlinks)
    body = _client().get(f"/api/site-crawls/{row.id}/page", params={"url": "https://s/a"}).json()
    assert body["outlinks"] == ["https://s/b"]
    assert body["inlinks"] == ["https://s/"]
    assert body["h1"] == ["A"]


async def test_a_url_that_was_not_crawled_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed_row(status="complete")

    async def missing(crawl_id, url):
        return None

    monkeypatch.setattr(site_pages, "load_page", missing)
    response = _client().get(f"/api/site-crawls/{row.id}/page", params={"url": "https://s/nope"})
    assert response.status_code == 404


async def test_the_issues_endpoint_returns_counts_and_the_orphan_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _seed_row(status="complete")

    async def counts(crawl_id):
        return {"broken": 2, "thin": 5, "orphan": 300}

    async def orphans(crawl_id, limit):
        return [f"https://s/{i}" for i in range(limit)], 300

    monkeypatch.setattr(site_pages, "issue_counts", counts)
    monkeypatch.setattr(site_pages, "load_orphans", orphans)
    body = _client().get(f"/api/site-crawls/{row.id}/issues").json()
    assert body["counts"]["broken"] == 2
    assert body["orphan_count"] == 300
    assert body["orphans_truncated"] is True  # the count stays exact, the list is capped
    assert len(body["orphans"]) == service.MAX_ORPHANS_LISTED


# --- page-type filter ----------------------------------------------------------------------


def test_a_crawl_can_be_restricted_to_product_pages() -> None:
    response = _client().post(
        "/api/site-crawls", json={"start_url": _host(), "page_types": ["product"]}
    )
    assert response.status_code == 202
    status = _client().get(f"/api/site-crawls/{response.json()['crawl_id']}").json()
    assert status["page_types"] == ["product"]


def test_both_page_types_are_recorded_in_a_stable_order() -> None:
    """The row, the worker and the report must all show the same string."""
    response = _client().post(
        "/api/site-crawls", json={"start_url": _host(), "page_types": ["collection", "product"]}
    )
    status = _client().get(f"/api/site-crawls/{response.json()['crawl_id']}").json()
    assert status["page_types"] == ["product", "collection"]


def test_no_page_types_means_every_page() -> None:
    response = _client().post("/api/site-crawls", json={"start_url": _host()})
    status = _client().get(f"/api/site-crawls/{response.json()['crawl_id']}").json()
    assert status["page_types"] == []


def test_an_unknown_page_type_is_rejected() -> None:
    response = _client().post(
        "/api/site-crawls", json={"start_url": _host(), "page_types": ["blog"]}
    )
    assert response.status_code == 422


async def test_navigation_pages_are_hidden_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed_row(status="complete")
    seen: list[bool] = []

    async def load_pages(crawl_id, *, limit, offset, sort, issue, include_navigation):
        seen.append(include_navigation)
        return [], 0

    monkeypatch.setattr(site_pages, "load_pages", load_pages)
    client = _client()
    client.get(f"/api/site-crawls/{row.id}/pages")
    client.get(f"/api/site-crawls/{row.id}/pages", params={"include_navigation": "true"})
    assert seen == [False, True]


async def test_pages_can_be_filtered_to_one_catalog_type(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed_row(status="complete")

    async def load_pages(crawl_id, *, limit, offset, sort, issue, include_navigation):
        assert issue == "product"
        return [{"url": "https://s/products/x", "page_type": "product"}], 1

    monkeypatch.setattr(site_pages, "load_pages", load_pages)
    body = _client().get(f"/api/site-crawls/{row.id}/pages", params={"issue": "product"}).json()
    assert body["pages"][0]["page_type"] == "product"


# --- abandoned crawls ---------------------------------------------------------------------


async def test_a_crawl_whose_worker_died_is_closed_out_when_polled() -> None:
    """Otherwise the row says "crawling" forever and the client polls a corpse."""
    from workers.tasks import SITE_CRAWL_STALE_AFTER_S

    row = await _seed_row(
        status="crawling",
        max_pages=50,
        pages_fetched=62,
        heartbeat_at=datetime.now(UTC) - timedelta(seconds=SITE_CRAWL_STALE_AFTER_S + 60),
    )
    body = _client().get(f"/api/site-crawls/{row.id}").json()
    assert body["status"] == "failed"
    assert "server restarted" in (body["error"] or "")
    assert body["pages_fetched"] == 62  # what it found is still reported


async def test_a_crawl_with_a_fresh_heartbeat_is_left_running() -> None:
    row = await _seed_row(status="crawling", heartbeat_at=datetime.now(UTC))
    body = _client().get(f"/api/site-crawls/{row.id}").json()
    assert body["status"] == "crawling"
    assert body["error"] is None


async def test_a_crawl_running_in_this_process_is_never_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with an old heartbeat, a task we are holding is alive by definition."""
    from workers.tasks import SITE_CRAWL_STALE_AFTER_S

    row = await _seed_row(
        status="crawling",
        heartbeat_at=datetime.now(UTC) - timedelta(seconds=SITE_CRAWL_STALE_AFTER_S + 60),
    )
    monkeypatch.setattr(service, "site_crawl_is_running", lambda crawl_id: True)
    body = _client().get(f"/api/site-crawls/{row.id}").json()
    assert body["status"] == "crawling"


# --- stopping -----------------------------------------------------------------------------


async def test_stopping_a_running_crawl_signals_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed_row(status="crawling", heartbeat_at=datetime.now(UTC))
    asked: list[str] = []
    monkeypatch.setattr(service, "request_site_crawl_stop", lambda cid: asked.append(cid) or True)
    response = _client().post(f"/api/site-crawls/{row.id}/stop")
    assert response.status_code == 200
    assert asked == [row.id]
    # Still "crawling": the stop is cooperative and the client polls until it lands.
    assert response.json()["status"] == "crawling"


async def test_stopping_a_finished_crawl_is_not_an_error() -> None:
    row = await _seed_row(status="complete", pages_fetched=10)
    response = _client().post(f"/api/site-crawls/{row.id}/stop")
    assert response.status_code == 200
    assert response.json()["status"] == "complete"


def test_stopping_an_unknown_crawl_is_404() -> None:
    assert _client().post(f"/api/site-crawls/{uuid4()}/stop").status_code == 404


# --- templates endpoint ---------------------------------------------------------------


async def test_the_templates_endpoint_returns_one_record_per_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _seed_row(status="complete", templates_found=2, templates_measured=1)

    async def load_template_reports(crawl_id):
        assert crawl_id == row.id
        return [
            {"template": "product", "page_count": 200, "representative_url": "https://s/products/x",
             "median_words": 480, "sample_urls": ["https://s/products/x"],
             "lighthouse": {"performance_score": 71, "seo_score": 92}, "error": ""},
            {"template": "blog", "page_count": 3, "representative_url": "",
             "median_words": 0, "sample_urls": [], "lighthouse": None,
             "error": "not tested - only the first 8 templates are measured"},
        ]

    monkeypatch.setattr(site_pages, "load_template_reports", load_template_reports)
    body = _client().get(f"/api/site-crawls/{row.id}/templates").json()
    assert [t["template"] for t in body["templates"]] == ["product", "blog"]
    assert body["templates"][0]["lighthouse"]["performance_score"] == 71
    assert body["templates"][0]["page_count"] == 200
    assert body["templates"][1]["lighthouse"] is None
    assert body["found"] == 2 and body["measured"] == 1


async def test_a_crawl_that_measured_nothing_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Template measurement can be switched off; that is a legitimate answer."""
    row = await _seed_row(status="complete")

    async def none(crawl_id):
        return []

    monkeypatch.setattr(site_pages, "load_template_reports", none)
    response = _client().get(f"/api/site-crawls/{row.id}/templates")
    assert response.status_code == 200
    assert response.json()["templates"] == []


async def test_templates_of_an_unknown_crawl_are_404() -> None:
    assert _client().get(f"/api/site-crawls/{uuid4()}/templates").status_code == 404


async def test_mongo_down_on_templates_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed_row(status="complete")

    async def boom(crawl_id):
        raise ServerSelectionTimeoutError("no servers")

    monkeypatch.setattr(site_pages, "load_template_reports", boom)
    assert _client().get(f"/api/site-crawls/{row.id}/templates").status_code == 503


async def test_the_page_list_carries_the_template_and_heading_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _seed_row(status="complete")

    async def load_pages(crawl_id, *, limit, offset, sort, issue, include_navigation):
        return [{"url": "https://s/products/x", "template": "product",
                 "heading_count": 7, "heading_skips": 1}], 1

    monkeypatch.setattr(site_pages, "load_pages", load_pages)
    page = _client().get(f"/api/site-crawls/{row.id}/pages").json()["pages"][0]
    assert page["template"] == "product"
    assert page["heading_count"] == 7 and page["heading_skips"] == 1


async def test_the_page_detail_carries_the_outline_and_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _seed_row(status="complete")

    async def load_page(crawl_id, url):
        return {"url": url, "title": "A", "template": "product",
                "headings": [{"level": 1, "text": "Linen Shirt"}, {"level": 3, "text": "Care"}],
                "text_content": "A breathable shirt made from European flax."}

    async def load_inlinks(crawl_id, url, limit=100):
        return []

    monkeypatch.setattr(site_pages, "load_page", load_page)
    monkeypatch.setattr(site_pages, "load_inlinks", load_inlinks)
    body = _client().get(f"/api/site-crawls/{row.id}/page", params={"url": "https://s/p"}).json()
    assert [h["level"] for h in body["headings"]] == [1, 3]
    assert "European flax" in body["text_content"]


# --- export endpoint ------------------------------------------------------------------


@pytest.fixture
def _export_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def iter_pages(crawl_id, *, include_navigation=False, batch_size=200):
        yield [{"url": "https://s/a", "title": "A", "status_code": 200, "depth": 0}]

    async def load_template_reports(crawl_id):
        return []

    async def issue_counts(crawl_id):
        return {}

    monkeypatch.setattr(site_pages, "iter_pages", iter_pages)
    monkeypatch.setattr(site_pages, "load_template_reports", load_template_reports)
    monkeypatch.setattr(site_pages, "issue_counts", issue_counts)


async def test_the_csv_export_downloads_as_a_named_file(_export_stubs: None) -> None:
    row = await _seed_row(status="complete", host="shop.example.com", pages_fetched=1)
    response = _client().get(f"/api/site-crawls/{row.id}/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert ".csv" in disposition and "shop.example.com" in disposition
    # The browser fetches this cross-origin, so the page must be able to read it.
    assert "Content-Disposition" in response.headers.get("access-control-expose-headers", "")
    assert response.content.startswith(codecs.BOM_UTF8)
    assert b"https://s/a" in response.content


async def test_the_json_export_downloads_as_a_named_file(_export_stubs: None) -> None:
    row = await _seed_row(status="complete", host="shop.example.com", pages_fetched=1)
    response = _client().get(f"/api/site-crawls/{row.id}/export.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert ".json" in response.headers["content-disposition"]
    doc = response.json()
    assert doc["crawl"]["host"] == "shop.example.com"
    assert [p["url"] for p in doc["pages"]] == ["https://s/a"]


async def test_exporting_a_running_crawl_is_refused(_export_stubs: None) -> None:
    """An export of a half-finished crawl is indistinguishable from a finished one."""
    row = await _seed_row(status="crawling", heartbeat_at=datetime.now(UTC))
    response = _client().get(f"/api/site-crawls/{row.id}/export.csv")
    assert response.status_code == 409
    assert "still crawling" in response.json()["detail"]


async def test_a_stopped_crawl_can_still_be_exported(_export_stubs: None) -> None:
    """Stopping is a terminal outcome, and its pages are worth downloading."""
    row = await _seed_row(status="stopped", pages_fetched=1)
    assert _client().get(f"/api/site-crawls/{row.id}/export.csv").status_code == 200


async def test_exporting_an_unknown_crawl_is_404() -> None:
    assert _client().get(f"/api/site-crawls/{uuid4()}/export.csv").status_code == 404


async def test_an_unsupported_export_format_is_rejected() -> None:
    row = await _seed_row(status="complete")
    assert _client().get(f"/api/site-crawls/{row.id}/export.xlsx").status_code == 422


async def test_navigation_pages_are_excluded_from_an_export_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _seed_row(status="complete")
    seen: list[bool] = []

    async def iter_pages(crawl_id, *, include_navigation=False, batch_size=200):
        seen.append(include_navigation)
        return
        yield  # pragma: no cover - makes this an async generator

    async def empty(crawl_id):
        return []

    monkeypatch.setattr(site_pages, "iter_pages", iter_pages)
    monkeypatch.setattr(site_pages, "load_template_reports", empty)
    monkeypatch.setattr(site_pages, "issue_counts", lambda cid: empty(cid))

    client = _client()
    client.get(f"/api/site-crawls/{row.id}/export.csv")
    client.get(f"/api/site-crawls/{row.id}/export.csv", params={"include_navigation": "true"})
    assert seen == [False, True]
