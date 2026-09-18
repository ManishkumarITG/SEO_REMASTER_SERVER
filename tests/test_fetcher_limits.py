"""The fetcher's defences against responses we do not control.

The audited URL comes from the client, so response size, content type and
encoding are all attacker-influenced. No network: respx intercepts httpx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from exceptions.crawler import FetchError
from services.crawler.fetcher import MAX_BYTES, MAX_ROBOTS_BYTES, fetch_page

HTML = b"<!doctype html><html lang='en'><head><title>Hi</title></head><body><h1>Yo</h1></body></html>"


def _html(content: bytes, charset: str = "utf-8") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": f"text/html; charset={charset}"}, content=content)


@respx.mock
async def test_oversized_body_is_capped_not_swallowed():
    """A body larger than MAX_BYTES is truncated, and the audit still completes."""
    respx.get("https://big.test/").mock(return_value=_html(b"<html><body>" + b"A" * (MAX_BYTES * 2)))
    respx.get("https://big.test/robots.txt").mock(return_value=httpx.Response(404))

    page = await fetch_page("https://big.test/")

    assert len(page.html) <= MAX_BYTES
    assert page.truncated is True


@respx.mock
async def test_normal_page_is_untouched():
    respx.get("https://ok.test/").mock(return_value=_html(HTML))
    respx.get("https://ok.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /admin\nSitemap: https://ok.test/s.xml")
    )

    page = await fetch_page("https://ok.test/")

    assert page.truncated is False
    assert page.dom.css_first("title").text() == "Hi"
    assert page.robots_txt["disallow"] == ["/admin"]
    assert page.robots_txt["sitemaps"] == ["https://ok.test/s.xml"]


@respx.mock
async def test_non_html_is_rejected_before_the_body_is_read():
    respx.get("https://pdf.test/").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
    )

    with pytest.raises(FetchError, match="Unsupported content type"):
        await fetch_page("https://pdf.test/")


@respx.mock
async def test_declared_charset_is_honoured():
    respx.get("https://l1.test/").mock(
        return_value=_html("<html><title>caf\xe9</title></html>".encode("iso-8859-1"), "iso-8859-1")
    )
    respx.get("https://l1.test/robots.txt").mock(return_value=httpx.Response(404))

    page = await fetch_page("https://l1.test/")

    assert page.dom.css_first("title").text() == "caf\xe9"


@respx.mock
async def test_undecodable_bytes_do_not_raise():
    """A charset that lies about the bytes must not kill the audit."""
    respx.get("https://bad.test/").mock(return_value=_html(b"<html><title>\xff\xfe\x00</title></html>"))
    respx.get("https://bad.test/robots.txt").mock(return_value=httpx.Response(404))

    page = await fetch_page("https://bad.test/")

    assert isinstance(page.html, str)


@respx.mock
async def test_unknown_charset_falls_back_to_utf8():
    respx.get("https://weird.test/").mock(return_value=_html(HTML, "x-not-a-real-charset"))
    respx.get("https://weird.test/robots.txt").mock(return_value=httpx.Response(404))

    page = await fetch_page("https://weird.test/")

    assert page.dom.css_first("title").text() == "Hi"


@respx.mock
async def test_giant_robots_txt_is_capped():
    respx.get("https://rb.test/").mock(return_value=_html(HTML))
    respx.get("https://rb.test/robots.txt").mock(
        return_value=httpx.Response(200, text="# pad\n" * MAX_ROBOTS_BYTES)
    )

    page = await fetch_page("https://rb.test/")

    assert page.robots_txt["status_code"] == 200


@respx.mock
async def test_unreachable_robots_txt_is_not_fatal():
    respx.get("https://nr.test/").mock(return_value=_html(HTML))
    respx.get("https://nr.test/robots.txt").mock(side_effect=httpx.ConnectError("nope"))

    page = await fetch_page("https://nr.test/")

    assert page.robots_txt["fetched"] is False
    assert page.dom.css_first("title").text() == "Hi"
