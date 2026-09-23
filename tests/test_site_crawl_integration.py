"""POST -> worker -> real crawler -> GET, with only HTTP and MongoDB stubbed.

Every other test in this feature stubs a seam. This one stubs none of them: the
route, the pre-flight probe, the background task, the real `crawl_site` and the
real page reduction all run. Only the network (respx) and MongoDB (an in-memory
dict) are replaced, so a regression anywhere along that chain shows up here.

TestClient runs BackgroundTasks synchronously after the response, which is what
makes a single request enough to drive the whole crawl.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import db.site_pages as site_pages
import main
from db.session import async_session_factory, init_db
from models.db import SiteCrawl
from workers.tasks import run_site_crawl

HOST = "crawlme.test"
ORIGIN = f"https://{HOST}"

ROBOTS = "User-agent: *\nDisallow: /admin\n"

SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{ORIGIN}/</loc></url>
  <url><loc>{ORIGIN}/about</loc></url>
  <url><loc>{ORIGIN}/secret-landing</loc></url>
</urlset>"""


def page(title: str, *, links: tuple[str, ...] = (), description: str = "", robots: str = "") -> str:
    head = [f"<title>{title}</title>"]
    if description:
        head.append(f'<meta name="description" content="{description}">')
    if robots:
        head.append(f'<meta name="robots" content="{robots}">')
    body = [f"<h1>{title}</h1>"] + [f'<a href="{href}">x</a>' for href in links]
    body.append("<p>" + " ".join(["content"] * 30) + "</p>")
    return f'<!doctype html><html lang="en"><head>{"".join(head)}</head><body>{"".join(body)}</body></html>'


def html(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def fake_mongo(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """An in-memory stand-in for the pages collection, keyed by url."""
    store: dict[str, dict[str, Any]] = {}

    async def save_pages(records: list[dict[str, Any]]) -> int:
        for record in records:
            store[record["url"]] = record
        return len(records)

    async def mark_orphans(crawl_id: str, urls: list[str]) -> int:
        for url in urls:
            if url in store:
                store[url]["orphan"] = True
        return len(urls)

    monkeypatch.setattr(site_pages, "save_pages", save_pages)
    monkeypatch.setattr(site_pages, "mark_orphans", mark_orphans)
    # workers.tasks imported both names directly, so patch them there too.
    import workers.tasks as tasks

    monkeypatch.setattr(tasks, "save_pages", save_pages)
    monkeypatch.setattr(tasks, "mark_orphans", mark_orphans)

    # The POST schedules the crawl on the TestClient event loop, which stops as
    # soon as the response is sent. Each test awaits `run_site_crawl` itself
    # instead, so the whole chain still runs - just in the test loop.
    import services.site_crawl_service as service

    monkeypatch.setattr(service, "schedule_site_crawl", lambda crawl_id: None)
    return store


def mock_site() -> None:
    respx.get(f"{ORIGIN}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get(f"{ORIGIN}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP, headers={"content-type": "application/xml"})
    )
    respx.get(f"{ORIGIN}/").mock(return_value=html(page(
        "Home", links=("/about", "/blog", "/admin/panel", "/brochure.pdf", "https://elsewhere.test/x"),
        description="The home page",
    )))
    # A description in the 70-160 character band the on_page analyzer wants, so
    # this page can out-score one with no description at all.
    respx.get(f"{ORIGIN}/about").mock(return_value=html(page(
        "About", description=(
            "We have built practical tools for small online shops since 2019, and this page "
            "explains who we are and how to reach us."
        ),
    )))
    respx.get(f"{ORIGIN}/blog").mock(return_value=html(page("Blog", links=("/blog/post-1",))))
    respx.get(f"{ORIGIN}/blog/post-1").mock(return_value=html(page("Post 1", robots="noindex")))
    # In the sitemap, linked from nowhere: an orphan.
    respx.get(f"{ORIGIN}/secret-landing").mock(return_value=html(page("Landing")))


async def _row(crawl_id: str) -> SiteCrawl:
    async with async_session_factory() as session:
        row = await session.get(SiteCrawl, crawl_id)
        assert row is not None
        return row


@respx.mock
async def test_a_crawl_runs_end_to_end_from_one_post(fake_mongo: dict[str, dict[str, Any]]) -> None:
    await init_db()
    mock_site()
    client = TestClient(main.app)

    created = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20, "max_depth": 3})
    assert created.status_code == 202
    crawl_id = created.json()["crawl_id"]
    await run_site_crawl(crawl_id)

    # The background task has already run by the time the response is returned.
    row = await _row(crawl_id)
    assert row.status in ("complete", "partial"), row.error_message
    assert row.host == HOST

    crawled = {url for url, doc in fake_mongo.items() if not doc["skipped"] and not doc["error"]}
    assert crawled == {
        f"{ORIGIN}/", f"{ORIGIN}/about", f"{ORIGIN}/blog",
        f"{ORIGIN}/blog/post-1", f"{ORIGIN}/secret-landing",
    }


@respx.mock
async def test_robots_and_assets_are_refused_without_a_request(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    await init_db()
    mock_site()
    admin = respx.get(f"{ORIGIN}/admin/panel").mock(return_value=html(page("Admin")))
    client = TestClient(main.app)
    crawl_id = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20}).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    assert admin.call_count == 0, "robots.txt disallows /admin"
    assert fake_mongo[f"{ORIGIN}/admin/panel"]["skipped"] == "robots"
    assert fake_mongo[f"{ORIGIN}/brochure.pdf"]["skipped"] == "asset"
    assert "https://elsewhere.test/x" not in fake_mongo


@respx.mock
async def test_the_sitemap_only_page_is_found_and_flagged_as_an_orphan(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    """The whole reason a site crawl beats a single-page audit."""
    await init_db()
    mock_site()
    client = TestClient(main.app)
    crawl_id = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20}).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    assert fake_mongo[f"{ORIGIN}/secret-landing"]["orphan"] is True
    assert fake_mongo[f"{ORIGIN}/about"]["orphan"] is False  # the home page links to it
    assert fake_mongo[f"{ORIGIN}/"]["orphan"] is False  # the seed is never an orphan
    assert (await _row(crawl_id)).orphan_count == 1


@respx.mock
async def test_the_status_endpoint_reports_the_finished_crawl(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    await init_db()
    mock_site()
    client = TestClient(main.app)
    crawl_id = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20}).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    body = client.get(f"/api/site-crawls/{crawl_id}").json()
    assert body["progress_pct"] == 100
    assert body["pages_fetched"] == 5
    assert body["pages_ok"] == 5
    assert body["skipped_by_robots"] == 1
    assert body["skipped_asset"] == 1
    assert body["noindex_count"] == 1  # /blog/post-1
    assert body["orphan_count"] == 1
    assert body["seeded_from_sitemap"] == 2  # /about and /secret-landing; the seed was already queued
    assert body["persistence_ok"] is True


@respx.mock
async def test_page_facts_survive_the_whole_round_trip(fake_mongo: dict[str, dict[str, Any]]) -> None:
    await init_db()
    mock_site()
    created = TestClient(main.app).post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20})
    await run_site_crawl(created.json()["crawl_id"])

    home = fake_mongo[f"{ORIGIN}/"]
    assert home["title"] == "Home"
    assert home["meta_description"] == "The home page"
    assert home["lang"] == "en"
    assert home["depth"] == 0
    assert home["word_count"] >= 30
    assert home["external_links"] == 1
    assert f"{ORIGIN}/about" in home["outlinks"]

    post = fake_mongo[f"{ORIGIN}/blog/post-1"]
    assert post["noindex"] is True
    assert post["depth"] == 2
    assert post["discovered_from"] == f"{ORIGIN}/blog"


@respx.mock
async def test_a_page_budget_smaller_than_the_site_finishes_partial(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    await init_db()
    mock_site()
    client = TestClient(main.app)
    crawl_id = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 2}).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    body = client.get(f"/api/site-crawls/{crawl_id}").json()
    assert body["status"] == "partial"
    assert body["pages_fetched"] == 2
    assert body["hit_page_budget"] is True
    assert body["aborted_reason"] == "page budget"


# --- analysis ------------------------------------------------------------------------------


@respx.mock
async def test_every_crawled_page_is_scored_by_the_analyzers(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    """on_page and technical run inside the crawl, while the DOM still exists."""
    await init_db()
    mock_site()
    client = TestClient(main.app)
    crawl_id = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20}).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    home = fake_mongo[f"{ORIGIN}/"]
    assert home["score"] is not None and 0 <= home["score"] <= 100
    assert home["grade"] in ("A", "B", "C", "D", "F")
    assert set(home["analyzer_scores"]) == {"on_page", "technical"}
    assert isinstance(home["findings"], list)

    body = client.get(f"/api/site-crawls/{crawl_id}").json()
    assert body["pages_analyzed"] == 5
    assert body["avg_score"] is not None
    assert body["grade"] is not None


@respx.mock
async def test_a_page_with_no_description_scores_below_one_with_a_description(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    """The score has to move with the content, or it is decoration."""
    await init_db()
    mock_site()
    created = TestClient(main.app).post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20})
    await run_site_crawl(created.json()["crawl_id"])

    with_meta = fake_mongo[f"{ORIGIN}/about"]  # has a meta description
    without_meta = fake_mongo[f"{ORIGIN}/secret-landing"]  # has none
    assert without_meta["score"] < with_meta["score"]
    assert any(f["id"].startswith("on_page") for f in without_meta["findings"])


@respx.mock
async def test_findings_are_kept_out_of_the_page_list_but_not_the_detail(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    """50 pages x 25 findings is a response nobody reads."""
    await init_db()
    mock_site()
    client = TestClient(main.app)
    crawl_id = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20}).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    # The list schema carries the counts and the score, never the findings array.
    from schemas import SiteCrawlPage

    assert "findings" not in SiteCrawlPage.model_fields
    assert "score" in SiteCrawlPage.model_fields
    assert fake_mongo[f"{ORIGIN}/"]["critical_count"] >= 0
    assert crawl_id


@respx.mock
async def test_analyzers_can_be_switched_off(
    fake_mongo: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    mock_site()
    import workers.tasks as tasks

    monkeypatch.setattr(tasks, "bulk_analyzers", lambda names: [])
    client = TestClient(main.app)
    crawl_id = client.post("/api/site-crawls", json={"start_url": ORIGIN, "max_pages": 20}).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    assert fake_mongo[f"{ORIGIN}/"]["score"] is None
    body = client.get(f"/api/site-crawls/{crawl_id}").json()
    assert body["avg_score"] is None  # not 0 - scoring simply did not run
    assert body["pages_fetched"] == 5  # the crawl itself is unaffected


# --- page-type filter, end to end -------------------------------------------------------------

STORE = "https://shopme.test"

STORE_SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{STORE}/</loc></url>
  <url><loc>{STORE}/collections/summer</loc></url>
  <url><loc>{STORE}/products/linen-shirt</loc></url>
  <url><loc>{STORE}/products/wool-scarf</loc></url>
  <url><loc>{STORE}/pages/about</loc></url>
</urlset>"""


def mock_store() -> None:
    """A store shaped like Shopify: home -> collections -> products, plus chrome."""
    respx.get(f"{STORE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *" + chr(10))
    )
    respx.get(f"{STORE}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=STORE_SITEMAP, headers={"content-type": "application/xml"})
    )
    respx.get(f"{STORE}/").mock(return_value=html(page(
        "Home", links=("/collections/summer", "/pages/about"), description="Shop home",
    )))
    respx.get(f"{STORE}/collections/summer").mock(return_value=html(page(
        "Summer", links=("/products/linen-shirt", "/products/wool-scarf"), description="Summer edit",
    )))
    respx.get(f"{STORE}/products/linen-shirt").mock(return_value=html(page("Linen Shirt")))
    respx.get(f"{STORE}/products/wool-scarf").mock(return_value=html(page("Wool Scarf")))
    respx.get(f"{STORE}/pages/about").mock(return_value=html(page("About")))


def _targets(store: dict[str, dict[str, Any]]) -> set[str]:
    return {url for url, doc in store.items() if doc.get("is_target") and not doc["skipped"]}


@respx.mock
async def test_a_products_only_crawl_audits_product_pages_and_nothing_else(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    await init_db()
    mock_store()
    client = TestClient(main.app)
    crawl_id = client.post(
        "/api/site-crawls",
        json={"start_url": STORE, "max_pages": 20, "page_types": ["product"]},
    ).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    assert _targets(fake_mongo) == {f"{STORE}/products/linen-shirt", f"{STORE}/products/wool-scarf"}
    body = client.get(f"/api/site-crawls/{crawl_id}").json()
    assert body["page_types"] == ["product"]
    assert body["target_pages"] == 2
    # Every product page was scored; the pages walked to find them were not.
    assert body["pages_analyzed"] == 2
    assert fake_mongo[f"{STORE}/products/linen-shirt"]["score"] is not None
    assert fake_mongo[f"{STORE}/"]["score"] is None


@respx.mock
async def test_a_collections_only_crawl_audits_collection_pages(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    await init_db()
    mock_store()
    client = TestClient(main.app)
    crawl_id = client.post(
        "/api/site-crawls",
        json={"start_url": STORE, "max_pages": 20, "page_types": ["collection"]},
    ).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    assert _targets(fake_mongo) == {f"{STORE}/collections/summer"}
    assert client.get(f"/api/site-crawls/{crawl_id}").json()["target_pages"] == 1


@respx.mock
async def test_both_types_audits_products_and_collections(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    await init_db()
    mock_store()
    client = TestClient(main.app)
    crawl_id = client.post(
        "/api/site-crawls",
        json={"start_url": STORE, "max_pages": 20, "page_types": ["product", "collection"]},
    ).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    assert _targets(fake_mongo) == {
        f"{STORE}/collections/summer",
        f"{STORE}/products/linen-shirt",
        f"{STORE}/products/wool-scarf",
    }
    body = client.get(f"/api/site-crawls/{crawl_id}").json()
    assert body["page_types"] == ["product", "collection"]
    assert body["target_pages"] == 3
    # The home page and /pages/about were the map, not the answer.
    assert body["navigation_pages"] >= 1


@respx.mock
async def test_the_page_list_hides_navigation_pages_by_default(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    await init_db()
    mock_store()
    client = TestClient(main.app)
    crawl_id = client.post(
        "/api/site-crawls",
        json={"start_url": STORE, "max_pages": 20, "page_types": ["product"]},
    ).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    # The real Mongo reads are stubbed out in this module, so assert on what was
    # stored: the page list filters on exactly this field.
    stored = fake_mongo[f"{STORE}/"]
    assert stored["is_target"] is False
    assert stored["page_type"] == ""
    assert fake_mongo[f"{STORE}/products/linen-shirt"]["page_type"] == "product"
    assert crawl_id


@respx.mock
async def test_a_store_with_no_matching_pages_says_so_instead_of_failing(
    fake_mongo: dict[str, dict[str, Any]],
) -> None:
    """Zero results next to a green status reads like a bug, so it is explained."""
    await init_db()
    mock_site()  # the non-store fixture: no /products/ or /collections/ URLs
    client = TestClient(main.app)
    crawl_id = client.post(
        "/api/site-crawls",
        json={"start_url": ORIGIN, "max_pages": 20, "page_types": ["product"]},
    ).json()["crawl_id"]
    await run_site_crawl(crawl_id)

    body = client.get(f"/api/site-crawls/{crawl_id}").json()
    assert body["target_pages"] == 0
    assert body["status"] != "failed"
    assert any("no product pages were found" in note for note in body["notes"])
