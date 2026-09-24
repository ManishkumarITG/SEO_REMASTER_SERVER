"""A small Markdown -> HTML renderer for the subset the report prompt emits.

Not a general Markdown engine, and deliberately not a new dependency. The report
prompt asks Gemini for exactly one dialect - ATX headings, bold, unordered and
ordered lists, GitHub pipe tables, horizontal rules and paragraphs - so this
handles that dialect and escapes everything else. A full CommonMark library would
be more code to audit than the feature is worth, and every byte of the model's
output is untrusted text that ends up in a browser, so escaping is the property
that actually matters here.

Security: every run of text is HTML-escaped BEFORE any inline markup is applied,
so a model that emits `<script>` produces the literal characters, never a tag.
Inline formatting is added by inserting known-safe tags into already-escaped text.
"""

from __future__ import annotations

import html
import re

#: Inline spans, applied to already-escaped text. Bold before italic so `**x**`
#: is not eaten by the single-`*` rule. Links are title-only text -> <a>, with the
#: href escaped into an attribute; a `javascript:` scheme is dropped to plain text.
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_SAFE_HREF = re.compile(r"^(https?:|mailto:|/|#)", re.IGNORECASE)


def _inline(text: str) -> str:
    """Escape a run of text, then re-apply the inline markup as safe tags."""
    out = html.escape(text, quote=False)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)

    def link(match: re.Match[str]) -> str:
        label, href = match.group(1), html.unescape(match.group(2)).strip()
        if not _SAFE_HREF.match(href):
            return label  # an unknown or javascript: scheme becomes plain text
        return f'<a href="{html.escape(href, quote=True)}" rel="noopener">{label}</a>'

    return _LINK.sub(link, out)


def _is_table_divider(line: str) -> bool:
    """The `|---|:--:|` row that turns the line above it into a table header."""
    stripped = line.strip().strip("|")
    cells = [c.strip() for c in stripped.split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c) for c in cells)


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render_markdown(text: str) -> str:
    """Render the supported Markdown subset to an HTML fragment (no <html> wrapper)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    def close_list(stack: list[str]) -> None:
        while stack:
            out.append(f"</{stack.pop()}>")

    list_stack: list[str] = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Blank line: end any open list, separate blocks.
        if not stripped:
            close_list(list_stack)
            i += 1
            continue

        # Horizontal rule.
        if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", stripped):
            close_list(list_stack)
            out.append("<hr>")
            i += 1
            continue

        # Heading (#..######).
        heading = re.match(r"(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list(list_stack)
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            i += 1
            continue

        # Table: a row followed by a divider row.
        if "|" in line and i + 1 < n and _is_table_divider(lines[i + 1]):
            close_list(list_stack)
            header = _split_row(line)
            out.append("<table><thead><tr>")
            out.extend(f"<th>{_inline(c)}</th>" for c in header)
            out.append("</tr></thead><tbody>")
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                cells = _split_row(lines[i])
                # Pad or trim to the header width so the table is never ragged.
                cells = (cells + [""] * len(header))[: len(header)]
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        # Unordered list item.
        bullet = re.match(r"[-*+]\s+(.*)$", stripped)
        if bullet:
            if not list_stack or list_stack[-1] != "ul":
                close_list(list_stack)
                out.append("<ul>")
                list_stack.append("ul")
            out.append(f"<li>{_inline(bullet.group(1).strip())}</li>")
            i += 1
            continue

        # Ordered list item.
        number = re.match(r"\d+[.)]\s+(.*)$", stripped)
        if number:
            if not list_stack or list_stack[-1] != "ol":
                close_list(list_stack)
                out.append("<ol>")
                list_stack.append("ol")
            out.append(f"<li>{_inline(number.group(1).strip())}</li>")
            i += 1
            continue

        # Anything else: a paragraph. Consecutive plain lines join into one.
        close_list(list_stack)
        para = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"(#{1,6}\s|[-*+]\s|\d+[.)]\s|(\*\s*){3,}$|(-\s*){3,}$)", lines[i].strip()
        ) and "|" not in lines[i]:
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    close_list(list_stack)
    return "\n".join(out)
