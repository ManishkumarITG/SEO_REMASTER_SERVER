"""Template detection and the heading outline: pure parsing, no network."""

from __future__ import annotations

from services.crawler.parser import parse_html
from services.crawler.site_crawl import MAX_HEADINGS_KEPT, extract_headings, extract_page_facts
from services.crawler.templates import (
    detect_template,
    template_from_markup,
    template_from_url,
)

STORE = "https://shop.test"


def _body(attrs: str = "", inner: str = "") -> object:
    return parse_html(f"<html><head><title>T</title></head><body {attrs}>{inner}</body></html>")


# --- template from markup -------------------------------------------------------------


def test_the_data_template_attribute_wins() -> None:
    """Shopify emits this itself, so it is exact when present."""
    assert template_from_markup(_body('data-template="product"')) == "product"
    assert template_from_markup(_body('data-template="index"')) == "home"
    assert template_from_markup(_body('data-template="collection"')) == "collection"


def test_the_body_class_convention_is_read_when_there_is_no_attribute() -> None:
    dom = _body('class="page-width-normal template_product card-hover-none"')
    assert template_from_markup(dom) == "product"
    assert template_from_markup(_body('class="template-collection"')) == "collection"


def test_an_alternate_template_reduces_to_its_base() -> None:
    """A theme's `product.bundle` is still the product template."""
    assert template_from_markup(_body('data-template="product.bundle"')) == "product"
    assert template_from_markup(_body('class="template_collection.filtered"')) == "collection"


def test_a_customer_account_template_is_not_read_as_customers() -> None:
    assert template_from_markup(_body('data-template="customers/account"')) == "account"


def test_markup_with_no_template_signal_returns_none() -> None:
    assert template_from_markup(_body()) is None
    assert template_from_markup(_body('class="page-width-normal"')) is None
    assert template_from_markup(parse_html("<html></html>")) is None


def test_an_unknown_theme_name_is_not_invented_into_a_template() -> None:
    """A report must never show a template name that came from someone else's markup."""
    assert template_from_markup(_body('data-template="wholesale-portal"')) is None


# --- template from the URL ------------------------------------------------------------


def test_url_shapes_map_to_templates() -> None:
    cases = {
        f"{STORE}/": "home",
        f"{STORE}/products/linen-shirt": "product",
        f"{STORE}/collections/summer": "collection",
        f"{STORE}/collections": "collection",
        f"{STORE}/collections/summer/products/linen-shirt": "product",
        f"{STORE}/blogs/news": "blog",
        f"{STORE}/blogs/news/hello-world": "article",
        f"{STORE}/pages/about": "page",
        f"{STORE}/search": "search",
        f"{STORE}/cart": "cart",
        f"{STORE}/account/login": "account",
        f"{STORE}/policies/refund-policy": "policy",
        f"{STORE}/something-else": "other",
    }
    for url, expected in cases.items():
        assert template_from_url(url) == expected, url


def test_markup_beats_the_url_when_the_two_disagree() -> None:
    """A theme rendering the product template at a vanity URL is still product."""
    dom = _body('data-template="product"')
    assert detect_template(f"{STORE}/shop/the-linen-one", dom) == "product"


def test_the_url_is_the_fallback_when_markup_says_nothing() -> None:
    assert detect_template(f"{STORE}/products/linen-shirt", _body()) == "product"
    assert detect_template(f"{STORE}/products/linen-shirt") == "product"


# --- heading outline ---------------------------------------------------------------


def test_the_outline_is_returned_in_document_order() -> None:
    dom = parse_html("<body><h1>Title</h1><h2>One</h2><h3>Deep</h3><h2>Two</h2></body>")
    headings, count, skips = extract_headings(dom)
    assert [(h["level"], h["text"]) for h in headings] == [
        (1, "Title"), (2, "One"), (3, "Deep"), (2, "Two"),
    ]
    assert count == 4 and skips == 0


def test_skipping_a_level_downward_is_counted() -> None:
    """H1 -> H3 is the break that h1_count and h2_count cannot show."""
    dom = parse_html("<body><h1>Title</h1><h3>Jumped</h3></body>")
    headings, count, skips = extract_headings(dom)
    assert skips == 1 and count == 2
    assert [h["level"] for h in headings] == [1, 3]


def test_returning_up_any_number_of_levels_is_normal() -> None:
    dom = parse_html("<body><h1>A</h1><h2>B</h2><h3>C</h3><h4>D</h4><h1>E</h1></body>")
    _, _, skips = extract_headings(dom)
    assert skips == 0


def test_heading_text_is_collapsed_and_trimmed() -> None:
    dom = parse_html("<body><h2>  spaced   out  " + chr(10) + " heading </h2></body>")
    headings, _, _ = extract_headings(dom)
    assert headings[0]["text"] == "spaced out heading"


def test_the_outline_is_capped_but_the_count_is_not() -> None:
    dom = parse_html("<body>" + "".join(f"<h2>H{i}</h2>" for i in range(MAX_HEADINGS_KEPT + 40)) + "</body>")
    headings, count, _ = extract_headings(dom)
    assert len(headings) == MAX_HEADINGS_KEPT
    assert count == MAX_HEADINGS_KEPT + 40


def test_a_page_with_no_headings_is_not_an_error() -> None:
    assert extract_headings(parse_html("<body><p>text</p></body>")) == ([], 0, 0)


# --- page facts ---------------------------------------------------------------------


HTML = """<html lang="en"><head><title>Linen Shirt</title></head>
<body data-template="product">
  <nav><a href="/">Home</a><p>navigation chrome that should not be stored</p></nav>
  <h1>Linen Shirt</h1>
  <h3>Skipped a level</h3>
  <p>A breathable shirt made from European flax, cut for everyday wear.</p>
  <footer><p>footer chrome that should not be stored</p></footer>
</body></html>"""


def test_page_facts_carry_the_outline_the_template_and_the_text() -> None:
    facts = extract_page_facts(parse_html(HTML), url=f"{STORE}/products/linen-shirt", text_chars=8000)
    assert facts["template"] == "product"
    assert facts["heading_count"] == 2
    assert facts["heading_skips"] == 1
    assert [h["level"] for h in facts["headings"]] == [1, 3]
    text = facts["text_content"]
    assert "European flax" in text
    assert "navigation chrome" not in text  # nav/footer are stripped
    assert "footer chrome" not in text


def test_text_storage_can_be_switched_off() -> None:
    facts = extract_page_facts(parse_html(HTML), url=f"{STORE}/products/x", text_chars=0)
    assert facts["text_content"] == ""
    assert facts["heading_count"] == 2  # the outline is not affected


def test_text_content_is_capped() -> None:
    body = "<body><p>" + ("word " * 5000) + "</p></body>"
    facts = extract_page_facts(parse_html(body), url=f"{STORE}/x", text_chars=200)
    assert 0 < len(facts["text_content"]) <= 200


def test_the_template_is_blank_when_no_url_is_supplied() -> None:
    """Facts extracted without a URL still work; only the template is unknowable."""
    facts = extract_page_facts(parse_html(HTML))
    assert facts["template"] == ""
    assert facts["heading_count"] == 2
