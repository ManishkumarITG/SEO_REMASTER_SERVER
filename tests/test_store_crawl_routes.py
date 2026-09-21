"""Store crawl routes: 202 shape, validation, polling, catalog reads, Mongo-down.

The probe and the background job are stubbed on the SERVICE module. The job
stub is not optional: TestClient runs BackgroundTasks synchronously after the
response, so without it every test would run a real crawl.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

import db.store_catalog as catalog
import main
import services.stores.store_crawl_service as service
from db.session import async_session_factory, init_db
from exceptions.stores import CatalogUnavailable, RobotsDisallowed
from models.db import StoreCrawl
from services.crawler.robots import RobotsRules
from services.stores.models import OriginInfo, ProbeResult


@asynccontextmanager
async def _fake_client(**_: object):
    yield None


def _probe(url: str = "https://shop.example.com/") -> ProbeResult:
    origin = OriginInfo(requested_url=url, origin=url.rstrip("/"), final_url=url, status_code=200, site_name="Shop")
    return ProbeResult(origin=origin, robots=RobotsRules.permissive(), products_json_status=200,
                       collections_json_status=200, sitemap_status=200)


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch):
    scheduled: list[str] = []

    async def fake_run(crawl_id: str) -> None:
        scheduled.append(crawl_id)

    async def fake_probe(client, url):
        return _probe(url)

    monkeypatch.setattr(service, "run_store_crawl", fake_run)
    monkeypatch.setattr(service, "probe_store", fake_probe)
    monkeypatch.setattr(service, "crawl_client", _fake_client)
    return scheduled


def _client() -> TestClient:
    return TestClient(main.app)


def _domain() -> str:
    return f"{uuid4().hex[:8]}.example.com"


def test_posting_a_store_url_returns_202_and_a_pollable_crawl_id(_stubs: list[str]) -> None:
    domain = _domain()
    with _client() as client:
        response = client.post("/api/stores/crawl", json={"store_url": f"https://{domain}/"})
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["store_domain"] == domain and body["status"] == "pending" and body["reused"] is False
        assert _stubs == [body["crawl_id"]]

        poll = client.get(f"/api/stores/crawl/{body['crawl_id']}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending" and poll.json()["progress_pct"] == 0


def test_a_bare_host_is_accepted_and_www_is_folded_into_the_domain() -> None:
    with _client() as client:
        response = client.post("/api/stores/crawl", json={"store_url": f"www.{_domain()}"})
    assert response.status_code == 202
    assert not response.json()["store_domain"].startswith("www.")


def test_a_bad_store_url_is_rejected_with_422_before_any_work(_stubs: list[str]) -> None:
    with _client() as client:
        for bad in ("", "ftp://x.com", "not a url"):
            assert client.post("/api/stores/crawl", json={"store_url": bad}).status_code == 422
    assert _stubs == []


def test_a_store_without_a_catalog_answers_422_synchronously(monkeypatch: pytest.MonkeyPatch, _stubs) -> None:
    async def no_catalog(client, url):
        raise CatalogUnavailable(url, ["/products.json", "/collections.json", "/sitemap.xml"])

    monkeypatch.setattr(service, "probe_store", no_catalog)
    with _client() as client:
        response = client.post("/api/stores/crawl", json={"store_url": f"https://{_domain()}/"})
    assert response.status_code == 422 and "/sitemap.xml" in response.json()["detail"]
    assert _stubs == []


def test_a_robots_blocked_store_answers_403(monkeypatch: pytest.MonkeyPatch) -> None:
    async def blocked(client, url):
        raise RobotsDisallowed(url)

    monkeypatch.setattr(service, "probe_store", blocked)
    with _client() as client:
        response = client.post("/api/stores/crawl", json={"store_url": f"https://{_domain()}/"})
    assert response.status_code == 403


def test_a_second_crawl_of_a_store_already_in_flight_reuses_the_crawl_id(_stubs: list[str]) -> None:
    domain = _domain()
    with _client() as client:
        first = client.post("/api/stores/crawl", json={"store_url": f"https://{domain}/"}).json()
        second = client.post("/api/stores/crawl", json={"store_url": f"https://{domain}/shop"}).json()
    assert second["crawl_id"] == first["crawl_id"] and second["reused"] is True
    assert len(_stubs) == 1


def test_polling_an_unknown_crawl_id_returns_404() -> None:
    with _client() as client:
        assert client.get("/api/stores/crawl/nope").status_code == 404


async def test_progress_percent_is_100_only_in_a_terminal_status() -> None:
    await init_db()
    domain = _domain()
    rows = {
        "enriching": StoreCrawl(store_url=f"https://{domain}/", store_domain=domain, status="enriching",
                                products_total=10, products_enriched=5),
        "partial": StoreCrawl(store_url=f"https://{domain}/", store_domain=domain, status="partial",
                              products_total=10, products_enriched=5),
    }
    with _client() as client:
        # Rows go in AFTER lifespan has run: startup reconciliation would otherwise
        # fail the "enriching" row before we poll it - exactly what it is for.
        async with async_session_factory() as session:
            session.add_all(rows.values())
            await session.commit()
        mid = client.get(f"/api/stores/crawl/{rows['enriching'].id}").json()["progress_pct"]
        done = client.get(f"/api/stores/crawl/{rows['partial'].id}").json()["progress_pct"]
    assert 5 < mid < 100 and done == 100


def test_the_crawls_list_is_not_shadowed_by_the_store_domain_route() -> None:
    with _client() as client:
        response = client.get("/api/stores/crawls?limit=3")
    assert response.status_code == 200 and "crawls" in response.json()


def test_a_never_crawled_store_returns_an_empty_catalog_not_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(domain, kind, **kw):
        return [], 0

    monkeypatch.setattr(catalog, "load_catalog_items", empty)
    with _client() as client:
        response = client.get(f"/api/stores/{_domain()}/products")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [] and body["total"] == 0 and body["last_crawl_id"] is None


def test_catalog_reads_return_503_when_mongodb_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(*a, **kw):
        raise ServerSelectionTimeoutError("no mongo")

    monkeypatch.setattr(catalog, "load_catalog_items", down)
    monkeypatch.setattr(catalog, "flag_counts", down)
    with _client() as client:
        for path in (f"/api/stores/{_domain()}/products", f"/api/stores/{_domain()}/collections",
                     f"/api/stores/{_domain()}/flags"):
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.json()["detail"].startswith("MongoDB unavailable")


def test_limit_above_the_cap_is_rejected_with_422() -> None:
    with _client() as client:
        assert client.get(f"/api/stores/{_domain()}/products?limit=999").status_code == 422
        assert client.get(f"/api/stores/{_domain()}/products?sort=bogus").status_code == 422


def test_products_and_collections_read_through_separate_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def record(domain, kind, **kw):
        calls.append(kind)
        return [], 0

    monkeypatch.setattr(catalog, "load_catalog_items", record)
    with _client() as client:
        d = _domain()
        client.get(f"/api/stores/{d}/products")
        client.get(f"/api/stores/{d}/collections")
    assert calls == ["product", "collection"]
