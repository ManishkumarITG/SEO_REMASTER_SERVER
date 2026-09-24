"""CSV and JSON export: shape, encoding, flattening and streaming. No network."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

import pytest

import db.site_pages as site_pages
from models.db import SiteCrawl
from services import site_crawl_export
from services.site_crawl_export import CSV_COLUMNS, export_filename, stream_csv, stream_json

HOST = "shop.test"
NEWLINE = chr(10)


def _row(**kwargs: object) -> SiteCrawl:
    defaults: dict[str, object] = {
        "id": "crawl-1", "start_url": f"https://{HOST}/", "host": HOST, "status": "complete",
        "max_pages": 100, "max_depth": 3, "pages_fetched": 2, "pages_ok": 2,
        "created_at": datetime(2026, 9, 23, tzinfo=UTC),
        "completed_at": datetime(2026, 9, 23, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return SiteCrawl(**defaults)  # type: ignore[arg-type]


PAGES = [
    {
        "url": f"https://{HOST}/products/linen-shirt", "template": "product", "page_type": "product",
        "depth": 2, "status_code": 200, "score": 88.0, "grade": "B",
        "critical_count": 0, "warning_count": 3,
        "title": "Linen Shirt", "meta_description": "A breathable shirt.",
        "canonical_url": f"https://{HOST}/products/linen-shirt", "lang": "en",
        "noindex": False, "nofollow": False, "orphan": False,
        "h1_count": 1, "h2_count": 2, "heading_count": 3, "heading_skips": 1,
        "headings": [{"level": 1, "text": "Linen Shirt"}, {"level": 3, "text": "Care"}],
        "word_count": 420, "images": 4, "images_missing_alt": 1,
        "internal_links": 40, "internal_nofollow_links": 0, "external_links": 2,
        "discovered_from": f"https://{HOST}/collections/summer",
        "final_url": f"https://{HOST}/products/linen-shirt", "redirect_chain": [],
        "elapsed_ms": 123.7, "truncated": False, "skipped": "", "error": "",
        "findings": [{"title": "Title too short (11 chars)"}, {"title": "Image alt coverage 75%"}],
        "text_content": "A breathable shirt" + NEWLINE + "made from European flax.",
        "outlinks": [f"https://{HOST}/a"],
    },
    {
        # An older crawl's page: none of the newer fields exist on it at all.
        "url": f"https://{HOST}/about", "depth": 1, "status_code": 200, "title": "About",
    },
]

TEMPLATES = [{
    "template": "product", "page_count": 200, "representative_url": f"https://{HOST}/products/x",
    "median_words": 420, "sample_urls": [], "error": "",
    "lighthouse": {"performance_score": 71, "accessibility_score": 95,
                   "best_practices_score": 83, "seo_score": 92},
}]


@pytest.fixture(autouse=True)
def _stub_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def iter_pages(crawl_id, *, include_navigation=False, batch_size=200):
        # Two batches, so the streaming seams are exercised rather than assumed.
        yield [PAGES[0]]
        yield [PAGES[1]]

    async def load_template_reports(crawl_id):
        return TEMPLATES

    async def issue_counts(crawl_id):
        return {"broken": 0, "thin": 1}

    monkeypatch.setattr(site_pages, "iter_pages", iter_pages)
    monkeypatch.setattr(site_pages, "load_template_reports", load_template_reports)
    monkeypatch.setattr(site_pages, "issue_counts", issue_counts)


async def _csv_bytes(row: SiteCrawl) -> bytes:
    return b"".join([chunk async for chunk in stream_csv(row)])


async def _rows(row: SiteCrawl) -> list[dict[str, str]]:
    text = (await _csv_bytes(row)).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


async def _json_doc(row: SiteCrawl) -> dict:
    raw = b"".join([chunk async for chunk in stream_json(row)])
    return json.loads(raw)


# --- CSV ---------------------------------------------------------------------------


async def test_the_csv_starts_with_a_utf8_bom() -> None:
    """Without it Excel on Windows reads the file as the system codepage."""
    assert (await _csv_bytes(_row()))[:3] == b"\xef\xbb\xbf"


async def test_the_header_is_the_declared_column_order() -> None:
    text = (await _csv_bytes(_row())).decode("utf-8-sig")
    assert next(csv.reader(io.StringIO(text))) == list(CSV_COLUMNS)


async def test_every_row_has_exactly_the_header_width() -> None:
    """A ragged CSV is the classic way an export opens wrong in a spreadsheet."""
    text = (await _csv_bytes(_row())).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert len({len(r) for r in rows}) == 1
    assert len(rows) == 3  # header plus both pages


async def test_a_page_is_flattened_into_readable_cells() -> None:
    page = (await _rows(_row()))[0]
    assert page["url"].endswith("/products/linen-shirt")
    assert page["template"] == "product"
    assert page["score"] == "88.0" and page["grade"] == "B"
    # Booleans read as words, not as Python's True/False.
    assert page["noindex"] == "no" and page["orphan"] == "no"
    # Lengths are computed here because a spreadsheet cannot do it once quoted.
    assert page["title_length"] == str(len("Linen Shirt"))
    assert page["meta_description_length"] == str(len("A breathable shirt."))
    assert page["redirect_count"] == "0"
    assert page["elapsed_ms"] == "124"


async def test_the_heading_outline_becomes_one_readable_cell() -> None:
    page = (await _rows(_row()))[0]
    assert page["heading_outline"] == "h1: Linen Shirt | h3: Care"
    assert page["heading_skips"] == "1"


async def test_findings_become_one_cell_most_urgent_first() -> None:
    page = (await _rows(_row()))[0]
    assert page["top_findings"] == "Title too short (11 chars); Image alt coverage 75%"


async def test_newlines_inside_text_are_collapsed() -> None:
    """Legal CSV either way, but a multi-line cell is miserable to read."""
    text = (await _rows(_row()))[0]["text_content"]
    assert NEWLINE not in text
    assert text == "A breathable shirt made from European flax."


async def test_the_templates_lighthouse_scores_repeat_onto_its_pages() -> None:
    """Denormalised so the file pivots: one formula answers "score by template"."""
    rows = await _rows(_row())
    assert rows[0]["template_performance"] == "71"
    assert rows[0]["template_seo"] == "92"
    # The page whose template was never measured gets blanks, not zeros.
    assert rows[1]["template_performance"] == ""


async def test_a_page_from_an_older_crawl_exports_as_blanks_not_errors() -> None:
    """Crawls predating the newer fields must still export."""
    old = (await _rows(_row()))[1]
    assert old["url"].endswith("/about")
    assert old["heading_outline"] == "" and old["text_content"] == ""
    assert old["template"] == "" and old["score"] == ""


def test_the_text_column_is_last_so_it_never_hides_the_useful_ones() -> None:
    assert CSV_COLUMNS[-1] == "text_content"


# --- JSON --------------------------------------------------------------------------


async def test_the_json_is_one_valid_document() -> None:
    doc = await _json_doc(_row())
    assert sorted(doc) == ["crawl", "issue_counts", "pages", "templates"]


async def test_the_json_carries_every_page_across_batches() -> None:
    doc = await _json_doc(_row())
    assert [p["url"] for p in doc["pages"]] == [PAGES[0]["url"], PAGES[1]["url"]]


async def test_the_json_keeps_the_nested_data_the_csv_flattens() -> None:
    page = (await _json_doc(_row()))["pages"][0]
    assert page["headings"][0] == {"level": 1, "text": "Linen Shirt"}
    assert page["outlinks"] == [f"https://{HOST}/a"]
    assert NEWLINE in page["text_content"]  # unflattened, unlike the CSV


async def test_the_json_describes_the_crawl_and_its_limits() -> None:
    doc = await _json_doc(_row(aborted_reason="page budget", max_pages=100))
    crawl = doc["crawl"]
    assert crawl["host"] == HOST and crawl["status"] == "complete"
    assert crawl["limits"]["max_pages"] == 100
    assert crawl["stopped_because"] == "page budget"
    assert crawl["exported_at"]


async def test_the_json_includes_the_template_reports() -> None:
    doc = await _json_doc(_row())
    assert doc["templates"][0]["lighthouse"]["performance_score"] == 71
    assert doc["issue_counts"]["thin"] == 1


async def test_an_empty_crawl_still_produces_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(crawl_id, *, include_navigation=False, batch_size=200):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(site_pages, "iter_pages", nothing)
    doc = await _json_doc(_row())
    assert doc["pages"] == []


async def test_issue_counts_failing_does_not_fail_the_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(crawl_id):
        raise RuntimeError("mongo went away")

    monkeypatch.setattr(site_pages, "issue_counts", boom)
    doc = await _json_doc(_row())
    assert doc["issue_counts"] == {}
    assert len(doc["pages"]) == 2  # the export itself is unaffected


# --- filenames ---------------------------------------------------------------------


def test_the_filename_says_what_the_file_is() -> None:
    row = _row()
    assert export_filename(row, "csv") == "sitecrawl-shop.test-20260923.csv"
    assert export_filename(row, "json") == "sitecrawl-shop.test-20260923.json"


def test_a_hostile_host_cannot_shape_the_filename() -> None:
    row = _row(host="../../etc/pa ss wd")
    name = export_filename(row, "csv")
    assert "/" not in name and " " not in name
    assert name.startswith("sitecrawl-") and name.endswith(".csv")


def test_export_module_declares_the_encoding_excel_needs() -> None:
    assert site_crawl_export.CSV_ENCODING == "utf-8-sig"
