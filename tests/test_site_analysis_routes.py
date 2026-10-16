"""Analysis routes: the GSC gate, 202+schedule, polling, stop, export, report."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import main
import services.site_analysis_service as service
from db.session import async_session_factory, init_db
from models.db import SiteAnalysis, SiteCrawl


def _host() -> str:
    return f"{uuid4().hex[:8]}.example.com"


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    scheduled: list[str] = []

    def fake_schedule(analysis_id: str) -> None:
        scheduled.append(analysis_id)

    # By default, pretend GSC is connected so create() passes its gate. Patching
    # the seam avoids forging a signed session cookie for a user_id.
    async def connected(user_id):
        return True

    monkeypatch.setattr(service, "schedule_site_analysis", fake_schedule)
    monkeypatch.setattr(service, "gsc_connected", connected)
    return scheduled


def _client() -> TestClient:
    return TestClient(main.app)


async def _seed(**kwargs: object) -> SiteAnalysis:
    await init_db()
    host = kwargs.pop("host", None) or _host()
    row = SiteAnalysis(start_url=f"https://{host}/", host=host, **kwargs)  # type: ignore[arg-type]
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        return row


# --- the GSC gate + create -------------------------------------------------------------


def test_creating_an_analysis_returns_202_and_schedules_it(_stubs: list[str]) -> None:
    host = _host()
    response = _client().post("/api/analyses", json={"start_url": f"https://{host}/", "max_pages": 50})
    assert response.status_code == 202
    body = response.json()
    assert body["host"] == host and body["status"] == "pending"
    assert _stubs == [body["analysis_id"]]


def test_analysis_is_refused_without_a_search_console_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_connected(user_id):
        return False

    monkeypatch.setattr(service, "gsc_connected", not_connected)
    response = _client().post("/api/analyses", json={"start_url": _host()})
    assert response.status_code == 400
    assert "Search Console" in response.json()["detail"]


def test_requested_crawl_options_are_recorded(_stubs: list[str]) -> None:
    response = _client().post(
        "/api/analyses",
        json={"start_url": _host(), "max_pages": 12, "page_types": ["product"], "max_depth": 2},
    )
    status = _client().get(f"/api/analyses/{response.json()['analysis_id']}").json()
    assert status["options"]["max_pages"] == 12
    assert status["options"]["page_types"] == ["product"]


def test_a_second_analysis_of_a_busy_host_reuses_the_one_running(_stubs: list[str]) -> None:
    host = _host()
    client = _client()
    first = client.post("/api/analyses", json={"start_url": f"https://{host}/"}).json()
    second = client.post("/api/analyses", json={"start_url": f"https://{host}/about"}).json()
    assert second["analysis_id"] == first["analysis_id"]
    assert second["reused"] is True
    assert _stubs == [first["analysis_id"]]  # no second schedule


def test_a_bad_url_is_rejected() -> None:
    assert _client().post("/api/analyses", json={"start_url": "mailto:a@b.c"}).status_code == 422


# --- polling / list / stop -------------------------------------------------------------


async def test_progress_tracks_the_phase() -> None:
    row = await _seed(status="running", phase="gsc", pages_fetched=10, avg_score=77.0)
    body = _client().get(f"/api/analyses/{row.id}").json()
    assert body["phase"] == "gsc"
    assert body["progress_pct"] == 75
    assert body["grade"] is not None


async def test_a_finished_analysis_reads_100_and_carries_its_result() -> None:
    row = await _seed(status="complete", phase="done", pages_fetched=8,
                      result_json={"gsc": {"connected": True}, "schema": {"sampled": 5}})
    body = _client().get(f"/api/analyses/{row.id}").json()
    assert body["progress_pct"] == 100
    assert body["result"]["schema"]["sampled"] == 5


async def test_an_unknown_analysis_is_404() -> None:
    assert _client().get(f"/api/analyses/{uuid4()}").status_code == 404


async def test_the_list_can_filter_by_host() -> None:
    unique = _host()  # unique so the shared dev DB cannot leak other rows in
    row = await _seed(status="complete", host=unique)
    await _seed(status="complete")
    body = _client().get("/api/analyses", params={"host": unique}).json()
    assert [a["analysis_id"] for a in body["analyses"]] == [row.id]


async def test_stopping_a_running_analysis_signals_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    row = await _seed(status="running", phase="crawl", heartbeat_at=datetime.now(UTC))
    asked: list[str] = []
    monkeypatch.setattr(service, "request_site_analysis_stop", lambda aid: asked.append(aid) or True)
    response = _client().post(f"/api/analyses/{row.id}/stop")
    assert response.status_code == 200 and asked == [row.id]


# --- outputs ---------------------------------------------------------------------------


async def test_export_delegates_to_the_spawned_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl_host = "shop.example.com"
    crawl = SiteCrawl(start_url=f"https://{crawl_host}/", host=crawl_host, status="complete", pages_fetched=5)
    async with async_session_factory() as session:
        session.add(crawl)
        await session.commit()
        crawl_id = crawl.id
    row = await _seed(status="complete", phase="done", crawl_id=crawl_id, host=crawl_host)

    async def fake_export(cid, session, *, fmt):
        assert cid == crawl_id
        async def stream():
            yield b"url,score\n"
        return stream(), "text/csv; charset=utf-8", "analysis.csv"

    import services.site_crawl_service as crawl_service
    monkeypatch.setattr(crawl_service, "get_site_crawl_export", fake_export)
    response = _client().get(f"/api/analyses/{row.id}/export.csv")
    assert response.status_code == 200
    assert b"url,score" in response.content


async def test_export_before_completion_is_409() -> None:
    row = await _seed(status="running", phase="crawl", heartbeat_at=datetime.now(UTC))
    assert _client().get(f"/api/analyses/{row.id}/export.csv").status_code == 409


async def test_report_generates_from_the_analysis_and_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl = SiteCrawl(start_url="https://s/", host="s", status="complete", pages_fetched=3)
    async with async_session_factory() as session:
        session.add(crawl)
        await session.commit()
        crawl_id = crawl.id
    row = await _seed(status="complete", phase="done", crawl_id=crawl_id)

    async def fake_report(analysis, crawl_row, *, engine="gemini"):
        assert analysis.id == row.id and crawl_row.id == crawl_id
        return b"<!doctype html><html><body>analysis report</body></html>", "analysis-report.html"

    import services.site_analysis_report as report_mod
    monkeypatch.setattr(report_mod, "generate_analysis_report_html", fake_report)
    response = _client().get(f"/api/analyses/{row.id}/report.html")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "analysis-report.html" in response.headers["content-disposition"]
    assert b"analysis report" in response.content


async def test_report_when_gemini_unconfigured_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    from exceptions.gemini import GeminiNotConfigured

    crawl = SiteCrawl(start_url="https://s2/", host="s2", status="complete", pages_fetched=3)
    async with async_session_factory() as session:
        session.add(crawl)
        await session.commit()
        crawl_id = crawl.id
    row = await _seed(status="complete", phase="done", crawl_id=crawl_id)

    async def unconfigured(analysis, crawl_row, *, engine="gemini"):
        raise GeminiNotConfigured("no key")

    import services.site_analysis_report as report_mod
    monkeypatch.setattr(report_mod, "generate_analysis_report_html", unconfigured)
    assert _client().get(f"/api/analyses/{row.id}/report.html").status_code == 503


async def test_report_for_an_unfinished_analysis_is_409() -> None:
    row = await _seed(status="running", phase="crawl", heartbeat_at=datetime.now(UTC))
    assert _client().get(f"/api/analyses/{row.id}/report.html").status_code == 409


async def test_report_engine_ollama_is_forwarded_for_an_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    crawl = SiteCrawl(start_url="https://s/", host="s", status="complete", pages_fetched=3)
    async with async_session_factory() as session:
        session.add(crawl)
        await session.commit()
        crawl_id = crawl.id
    row = await _seed(status="complete", phase="done", crawl_id=crawl_id)
    seen: list[str] = []

    async def fake_report(analysis, crawl_row, *, engine="gemini"):
        seen.append(engine)
        return b"<!doctype html><html><body>local</body></html>", "analysis-s-ollama-report.html"

    import services.site_analysis_report as report_mod
    monkeypatch.setattr(report_mod, "generate_analysis_report_html", fake_report)
    response = _client().get(f"/api/analyses/{row.id}/report.html?engine=ollama")
    assert response.status_code == 200
    assert seen == ["ollama"]
    assert "ollama-report.html" in response.headers["content-disposition"]


async def test_report_when_ollama_fails_is_502_for_an_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    from exceptions.ollama import OllamaError

    crawl = SiteCrawl(start_url="https://s/", host="s", status="complete", pages_fetched=3)
    async with async_session_factory() as session:
        session.add(crawl)
        await session.commit()
        crawl_id = crawl.id
    row = await _seed(status="complete", phase="done", crawl_id=crawl_id)

    async def failing(analysis, crawl_row, *, engine="gemini"):
        raise OllamaError("Ollama answered 404: model 'qwen3:4b' not found, try pulling it first")

    import services.site_analysis_report as report_mod
    monkeypatch.setattr(report_mod, "generate_analysis_report_html", failing)
    response = _client().get(f"/api/analyses/{row.id}/report.html?engine=ollama")
    assert response.status_code == 502
    assert "try pulling it first" in response.json()["detail"]
