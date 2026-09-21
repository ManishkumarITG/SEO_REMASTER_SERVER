"""SEO gap flags. Pure functions, no network, no DB.

The headline test is the default-title one: a store with zero SEO titles set has
zero empty <title>s, so a naive check reports nothing wrong.
"""

from __future__ import annotations

from services.analyzers.on_page import (
    META_DESC_MAX_CHARS,
    META_DESC_MIN_CHARS,
    TITLE_MAX_CHARS,
    TITLE_MIN_CHARS,
)
from services.stores.models import CatalogItem
from services.stores.seo_gaps import (
    SEO_GAP_FLAGS,
    build_catalog_record,
    catalog_gaps,
    content_hash,
    infer_shop_suffix,
    is_default_title,
    item_gaps,
)


def _product(**overrides) -> CatalogItem:
    base = dict(
        kind="product", handle="linen-shirt", url="https://shop.test/products/linen-shirt",
        name="Linen Shirt", description="A breathable linen shirt for warm days, cut generously.",
        meta_title="Linen Shirt | Breathable Summer Shirt for Men | Ember & Oak",
        meta_description="A breathable linen shirt for warm days, cut generously with a relaxed fit and natural buttons.",
        featured_image_url="https://cdn.test/a.jpg", featured_image_alt="Linen shirt, front", enriched=True,
    )
    base.update(overrides)
    return CatalogItem(**base)


def test_a_clean_product_has_no_gaps() -> None:
    assert item_gaps(_product()) == []


def test_a_missing_meta_description_is_flagged() -> None:
    assert "meta_description_missing" in item_gaps(_product(meta_description=""))


def test_boundary_lengths_are_not_flagged_but_one_off_is() -> None:
    assert "meta_title_short" not in item_gaps(_product(meta_title="x" * TITLE_MIN_CHARS))
    assert "meta_title_long" not in item_gaps(_product(meta_title="x" * TITLE_MAX_CHARS))
    assert "meta_title_short" in item_gaps(_product(meta_title="x" * (TITLE_MIN_CHARS - 1)))
    assert "meta_title_long" in item_gaps(_product(meta_title="x" * (TITLE_MAX_CHARS + 1)))
    assert "meta_description_short" in item_gaps(_product(meta_description="x" * (META_DESC_MIN_CHARS - 1)))
    assert "meta_description_long" in item_gaps(_product(meta_description="x" * (META_DESC_MAX_CHARS + 1)))


def test_a_meta_title_that_is_just_the_product_name_plus_shop_suffix_is_flagged_default() -> None:
    """The headline number. Every page has a non-empty <title>; this is what makes it honest."""
    item = _product(meta_title="Linen Shirt | Ember & Oak")
    assert "meta_title_is_default" in item_gaps(item, shop_suffix="ember & oak")
    assert "meta_title_missing" not in item_gaps(item, shop_suffix="ember & oak")


def test_a_custom_seo_title_is_not_flagged_default() -> None:
    item = _product(meta_title="Breathable Linen Shirt for Hot Summers | Ember & Oak")
    assert "meta_title_is_default" not in item_gaps(item, shop_suffix="ember & oak")


def test_default_detection_is_case_and_separator_insensitive() -> None:
    assert is_default_title("linen shirt – Ember & Oak", "Linen Shirt", "ember & oak")
    assert is_default_title("Linen Shirt - Ember & Oak", "Linen Shirt", "ember & oak")
    assert is_default_title("Linen Shirt", "Linen Shirt", "")
    assert not is_default_title("Linen Shirt Deluxe | Ember & Oak", "Linen Shirt", "ember & oak")


def test_shop_suffix_is_inferred_from_a_majority_of_titles() -> None:
    titles = [f"Product {i} | Ember & Oak" for i in range(8)] + ["Odd one out"]
    assert infer_shop_suffix(titles) == "ember & oak"


def test_no_suffix_is_inferred_without_a_majority() -> None:
    assert infer_shop_suffix(["A | X", "B | Y", "C | Z", "D"]) == ""


def test_image_alt_missing_is_distinct_from_image_missing() -> None:
    assert "image_alt_missing" in item_gaps(_product(featured_image_alt=""))
    assert "image_missing" not in item_gaps(_product(featured_image_alt=""))
    gaps = item_gaps(_product(featured_image_url="", featured_image_alt=""))
    assert "image_missing" in gaps and "image_alt_missing" not in gaps


def test_an_unenriched_item_reports_why_instead_of_meta_flags() -> None:
    assert item_gaps(_product(enriched=False)) == ["not_enriched"]
    assert item_gaps(_product(enriched=False, enrich_error="HTTP 500")) == ["page_unreachable"]
    assert item_gaps(_product(enriched=False, enrich_error="skipped_by_robots")) == ["skipped_by_robots"]


def test_collections_are_flagged_for_being_empty_not_for_missing_price() -> None:
    col = CatalogItem(kind="collection", handle="summer", url="https://shop.test/collections/summer",
                      name="Summer", products_count=0, enriched=True,
                      meta_title="Summer Linen, Cotton and Light Layers | Ember & Oak",
                      meta_description="x" * 100, description="d", featured_image_url="i", featured_image_alt="a")
    gaps = item_gaps(col)
    assert gaps == ["collection_empty"]


def test_canonical_pointing_elsewhere_is_flagged() -> None:
    assert "canonical_mismatch" in item_gaps(_product(canonical_url="https://shop.test/products/other"))
    assert "canonical_mismatch" not in item_gaps(_product(canonical_url="https://www.shop.test/products/linen-shirt/"))


def test_duplicates_are_flagged_across_the_catalog_in_one_pass() -> None:
    items = [
        _product(handle="a", meta_title="Same Title Everywhere For Every Product | Shop"),
        _product(handle="b", meta_title="Same Title Everywhere For Every Product | Shop"),
        _product(handle="c", meta_title="A Genuinely Unique Title For This Product | Shop"),
    ]
    catalog_gaps(items)
    assert "duplicate_meta_title" in items[0].gaps and "duplicate_meta_title" in items[1].gaps
    assert "duplicate_meta_title" not in items[2].gaps


def test_catalog_gaps_infers_the_suffix_and_applies_default_detection() -> None:
    items = [_product(handle=f"p{i}", name=f"Product {i}", meta_title=f"Product {i} | Ember & Oak") for i in range(5)]
    items.append(_product(handle="custom", name="Special", meta_title="Hand-Sewn Special Edition Linen | Ember & Oak"))
    suffix = catalog_gaps(items)
    assert suffix == "ember & oak"
    assert all("meta_title_is_default" in i.gaps for i in items[:5])
    assert "meta_title_is_default" not in items[5].gaps


def test_every_emitted_flag_is_in_the_documented_vocabulary() -> None:
    variants = [
        _product(), _product(meta_title=""), _product(meta_description=""), _product(enriched=False),
        _product(featured_image_url=""), _product(canonical_url="https://x/y"),
        _product(enriched=False, enrich_error="boom"), _product(meta_title="Linen Shirt"),
    ]
    catalog_gaps(variants)
    for item in variants:
        assert set(item.gaps) <= set(SEO_GAP_FLAGS), item.gaps


def test_content_hash_ignores_timestamps_and_changes_with_meta() -> None:
    a = _product()
    b = _product()
    assert content_hash(a) == content_hash(b)
    assert content_hash(_product(meta_description="different")) != content_hash(a)


def test_build_catalog_record_carries_flags_count_excerpt_and_identity() -> None:
    item = _product(description="word " * 100, meta_description="")
    item.gaps = item_gaps(item)
    rec = build_catalog_record(item, store_domain="shop.test", store_url="https://shop.test/", crawl_id="c1",
                               source="products_json")
    assert rec["store_domain"] == "shop.test" and rec["kind"] == "product" and rec["handle"] == "linen-shirt"
    assert rec["seo_flags"] == ["meta_description_missing"] and rec["seo_gap_count"] == 1
    assert len(rec["description_excerpt"]) <= 200 and rec["content_hash"]
    assert "gaps" not in rec
