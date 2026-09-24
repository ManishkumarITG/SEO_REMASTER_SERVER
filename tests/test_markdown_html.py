"""The Markdown -> HTML subset renderer: shape and, above all, escaping."""

from __future__ import annotations

from services.markdown_html import render_markdown


def test_headings_map_to_levels() -> None:
    out = render_markdown("# One\n## Two\n### Three")
    assert "<h1>One</h1>" in out
    assert "<h2>Two</h2>" in out
    assert "<h3>Three</h3>" in out


def test_bold_italic_and_code_inline() -> None:
    out = render_markdown("A **bold** and *italic* and `code` word.")
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<code>code</code>" in out


def test_unordered_and_ordered_lists() -> None:
    ul = render_markdown("- a\n- b")
    assert ul.count("<li>") == 2 and "<ul>" in ul and "</ul>" in ul
    ol = render_markdown("1. a\n2. b")
    assert "<ol>" in ol and ol.count("<li>") == 2


def test_a_pipe_table_becomes_a_real_table() -> None:
    md = "| Metric | Value |\n| --- | --- |\n| LCP | 3.1s |\n| CLS | 0.02 |"
    out = render_markdown(md)
    assert "<table>" in out and "</table>" in out
    assert out.count("<th>") == 2
    assert out.count("<tr>") == 3  # header + two body rows
    assert "<td>LCP</td>" in out and "<td>3.1s</td>" in out


def test_a_ragged_table_row_is_padded_to_the_header_width() -> None:
    md = "| A | B | C |\n| - | - | - |\n| 1 | 2 |"
    out = render_markdown(md)
    # The short row still yields three cells, so the table is never misaligned.
    assert out.count("<td>") == 3


def test_horizontal_rule_and_paragraphs() -> None:
    out = render_markdown("First para.\n\n---\n\nSecond para.")
    assert "<hr>" in out
    assert out.count("<p>") == 2


def test_consecutive_plain_lines_join_into_one_paragraph() -> None:
    out = render_markdown("line one\nline two\nline three")
    assert out.count("<p>") == 1
    assert "line one line two line three" in out


# --- escaping: the property that actually matters --------------------------------------


def test_a_script_tag_in_the_text_is_neutralised() -> None:
    out = render_markdown("## <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_raw_html_in_a_paragraph_is_escaped() -> None:
    out = render_markdown("Text with <img src=x onerror=alert(1)> in it.")
    assert "<img" not in out
    assert "&lt;img" in out


def test_raw_html_inside_a_table_cell_is_escaped() -> None:
    md = "| A | B |\n| - | - |\n| <b>x</b> | <script>y</script> |"
    out = render_markdown(md)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    # A model-authored <b> is text, not markup.
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_a_safe_link_is_kept_and_a_javascript_link_is_dropped() -> None:
    safe = render_markdown("See [the docs](https://example.com/x).")
    assert '<a href="https://example.com/x" rel="noopener">the docs</a>' in safe

    unsafe = render_markdown("Click [here](javascript:alert(1)).")
    assert "javascript:" not in unsafe
    assert "<a" not in unsafe  # the whole link collapses to its plain label


def test_a_link_href_with_quotes_cannot_break_out_of_the_attribute() -> None:
    out = render_markdown('[x](https://e.com/"onmouseover="alert(1))')
    assert 'onmouseover="alert' not in out


def test_empty_input_is_empty_output() -> None:
    assert render_markdown("") == ""
    assert render_markdown("\n\n  \n") == ""
