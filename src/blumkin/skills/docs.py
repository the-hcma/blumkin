"""`docs create` shared layer: the Markdown subset and its provider-neutral block model.

Both provider backends render the same :class:`DocBlock` list - Google via
``documents.batchUpdate``, Microsoft via ``python-docx`` - so one authoring
format (and one test fixture) covers both. Constructs outside the subset degrade
to plain text; nothing here raises on unrecognised Markdown.

Supported subset: ATX headings (``#``-``######``), paragraphs, ``**bold**`` /
``*italic*`` / ``` `code` ``` / ``[text](url)`` inline, ``-``/``*``/``+`` and
``1.`` lists (one nesting level), fenced code blocks, ``---`` horizontal rules,
and pipe tables. Tables render as a fenced-style monospace grid in v1 (see
issue #194 phase 4 for native tables).
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DocBlockKind = Literal["heading", "paragraph", "bullet", "number", "code", "rule", "table"]

# A whole authored document is held in memory (and, for Microsoft, turned into an
# in-memory .docx); cap the input so a runaway `--body-file` cannot exhaust it.
MAX_BODY_BYTES = 1_000_000


class DocBodyError(ValueError):
    """`--body` / `--body-file` was missing, unreadable, or too large."""


@dataclass(frozen=True, slots=True)
class DocSpan:
    """A run of text with uniform inline formatting."""

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    link: str | None = None


@dataclass(frozen=True, slots=True)
class DocBlock:
    """One block-level element. ``spans`` carries inline content for text blocks;
    ``code_text`` holds a fenced block verbatim; ``rows`` holds a table's cells."""

    kind: DocBlockKind
    spans: tuple[DocSpan, ...] = ()
    level: int = 0
    code_text: str = ""
    rows: tuple[tuple[tuple[DocSpan, ...], ...], ...] = field(default=())


def format_docs_create_human(payload: dict[str, Any]) -> list[str]:
    document = payload.get("document") or {}
    lines = [f"Document created: {document.get('name')!r} ({document.get('format')})"]
    if document.get("folder"):
        lines.append(f"  folder: {document['folder']}")
    lines.append(f"  id={document.get('id')}")
    lines.append(f"  {document.get('web_url')}")
    return lines


def parse_body(body: str, *, body_format: str = "markdown") -> list[DocBlock]:
    """Parse authored content into blocks. ``body_format`` is ``markdown`` or ``text``."""
    fmt = body_format.strip().lower()
    if fmt == "text":
        return _parse_plain_text(body)
    if fmt == "markdown":
        return parse_markdown(body)
    raise DocBodyError(f"unknown --format {body_format!r} (expected markdown or text)")


def parse_markdown(source: str) -> list[DocBlock]:
    """Parse the supported Markdown subset into a block list."""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[DocBlock] = []
    para: list[str] = []
    index = 0
    total = len(lines)

    def flush_paragraph() -> None:
        if not para:
            return
        text = " ".join(part.strip() for part in para).strip()
        para.clear()
        if text:
            blocks.append(DocBlock(kind="paragraph", spans=parse_inline(text)))

    while index < total:
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            index += 1
            buffer: list[str] = []
            while index < total and lines[index].strip() != "```":
                buffer.append(lines[index])
                index += 1
            index += 1  # consume the closing fence (or fall off the end)
            blocks.append(DocBlock(kind="code", code_text="\n".join(buffer)))
            continue

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if _RULE_RE.fullmatch(stripped):
            flush_paragraph()
            blocks.append(DocBlock(kind="rule"))
            index += 1
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            blocks.append(
                DocBlock(
                    kind="heading",
                    level=len(heading.group(1)),
                    spans=parse_inline(heading.group(2).strip()),
                )
            )
            index += 1
            continue

        if "|" in line and index + 1 < total and _is_table_separator(lines[index + 1]):
            flush_paragraph()
            rows = [_split_table_row(line)]
            index += 2
            while index < total and lines[index].strip() and "|" in lines[index]:
                rows.append(_split_table_row(lines[index]))
                index += 1
            blocks.append(
                DocBlock(
                    kind="table",
                    rows=tuple(tuple(parse_inline(cell) for cell in row) for row in rows),
                )
            )
            continue

        item = _LIST_RE.match(line)
        if item:
            flush_paragraph()
            indent = len(item.group(1).expandtabs(4))
            ordered = item.group(2)[0].isdigit()
            blocks.append(
                DocBlock(
                    kind="number" if ordered else "bullet",
                    level=1 if indent >= 2 else 0,
                    spans=parse_inline(item.group(3).strip()),
                )
            )
            index += 1
            continue

        para.append(line)
        index += 1

    flush_paragraph()
    return blocks


def parse_inline(text: str, *, bold: bool = False, italic: bool = False) -> tuple[DocSpan, ...]:
    """Split one line of text into formatted spans (recurses for nested emphasis)."""
    spans: list[DocSpan] = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > pos:
            spans.append(DocSpan(text[pos : match.start()], bold=bold, italic=italic))
        if match.group("code") is not None:
            spans.append(DocSpan(match.group("codet"), bold=bold, italic=italic, code=True))
        elif match.group("link") is not None:
            spans.append(
                DocSpan(match.group("ltext"), bold=bold, italic=italic, link=match.group("lurl"))
            )
        elif match.group("bi") is not None:
            spans.extend(parse_inline(match.group("bit"), bold=True, italic=True))
        elif match.group("b") is not None:
            spans.extend(parse_inline(match.group("bt"), bold=True, italic=italic))
        elif match.group("b2") is not None:
            spans.extend(parse_inline(match.group("b2t"), bold=True, italic=italic))
        elif match.group("i") is not None:
            spans.extend(parse_inline(match.group("it"), bold=bold, italic=True))
        elif match.group("i2") is not None:
            spans.extend(parse_inline(match.group("i2t"), bold=bold, italic=True))
        pos = match.end()
    if pos < len(text):
        spans.append(DocSpan(text[pos:], bold=bold, italic=italic))
    return _coalesce(spans)


def read_body(body: str | None, body_file: str | None) -> str:
    """Resolve exactly one of ``--body`` / ``--body-file`` into a string."""
    if (body is None) == (body_file is None):
        raise DocBodyError("exactly one of --body or --body-file is required")
    if body_file is not None:
        path = Path(body_file)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise DocBodyError(f"cannot read --body-file {path}: {exc}") from exc
        if len(raw) > MAX_BODY_BYTES:
            raise DocBodyError(f"--body-file {path} is larger than {MAX_BODY_BYTES} bytes")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocBodyError(f"--body-file {path} is not valid UTF-8: {exc}") from exc
    text = str(body)
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise DocBodyError(f"--body is larger than {MAX_BODY_BYTES} bytes")
    return text


_SAFE_LINK_SCHEME = re.compile(r"(?i)^(?:https?:|mailto:|tel:)")


def _span_to_html(span: DocSpan) -> str:
    text = _html.escape(span.text)
    if span.code:
        text = f"<code>{text}</code>"
    else:
        if span.bold:
            text = f"<strong>{text}</strong>"
        if span.italic:
            text = f"<em>{text}</em>"
    if span.link and _SAFE_LINK_SCHEME.match(span.link):
        text = f'<a href="{_html.escape(span.link, quote=True)}">{text}</a>'
    return text


def spans_to_html(spans: tuple[DocSpan, ...]) -> str:
    return "".join(_span_to_html(span) for span in spans)


def _table_to_html(rows: tuple[tuple[tuple[DocSpan, ...], ...], ...]) -> str:
    if not rows:
        return ""
    header, *body = rows
    cells = "".join(f"<th>{spans_to_html(cell)}</th>" for cell in header)
    out = [f"<thead><tr>{cells}</tr></thead>"]
    if body:
        rows_html = "".join(
            "<tr>" + "".join(f"<td>{spans_to_html(cell)}</td>" for cell in row) + "</tr>"
            for row in body
        )
        out.append(f"<tbody>{rows_html}</tbody>")
    return f"<table>{''.join(out)}</table>"


def render_email_html(blocks: list[DocBlock]) -> str:
    """Render parsed blocks as a self-contained HTML fragment for an email body.

    Semantic tags only, no stylesheet - Gmail and Outlook both render bare
    ``<p>`` / ``<ul>`` / ``<strong>`` cleanly, and an inline style block is what
    gets stripped. Lists use one nesting level, matching :func:`parse_markdown`.
    """
    out: list[str] = []
    open_lists: list[str] = []

    def close_to(depth: int) -> None:
        while len(open_lists) > depth:
            out.append(f"</{open_lists.pop()}>")

    for block in blocks:
        if block.kind in ("bullet", "number"):
            tag = "ul" if block.kind == "bullet" else "ol"
            depth = 2 if block.level else 1
            if len(open_lists) >= depth and open_lists[depth - 1] != tag:
                close_to(depth - 1)
            close_to(depth)
            while len(open_lists) < depth:
                out.append(f"<{tag}>")
                open_lists.append(tag)
            out.append(f"<li>{spans_to_html(block.spans)}</li>")
            continue
        close_to(0)
        if block.kind == "heading":
            level = min(max(block.level, 1), 6)
            out.append(f"<h{level}>{spans_to_html(block.spans)}</h{level}>")
        elif block.kind == "paragraph":
            out.append(f"<p>{spans_to_html(block.spans)}</p>")
        elif block.kind == "code":
            out.append(f"<pre><code>{_html.escape(block.code_text)}</code></pre>")
        elif block.kind == "rule":
            out.append("<hr>")
        elif block.kind == "table":
            out.append(_table_to_html(block.rows))
    close_to(0)
    return "".join(out)


def table_to_text(rows: tuple[tuple[tuple[DocSpan, ...], ...], ...]) -> str:
    """Render a parsed table as a padded monospace grid (v1 fallback).

    Ragged rows (a body row with more or fewer cells than the header) are padded
    or truncated to the widest row so the grid never raises on uneven input.
    """
    grid = [[spans_to_text(cell) for cell in row] for row in rows]
    if not grid:
        return ""
    ncols = max(len(row) for row in grid)
    grid = [row + [""] * (ncols - len(row)) for row in grid]
    widths = [max(len(row[col]) for row in grid) for col in range(ncols)]
    lines = [" | ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)) for row in grid]
    if len(lines) > 1:
        lines.insert(1, "-+-".join("-" * width for width in widths))
    return "\n".join(lines)


def spans_to_text(spans: tuple[DocSpan, ...]) -> str:
    return "".join(span.text for span in spans)


_HEADING_RE = re.compile(r"(#{1,6})\s+(.*)")
_INLINE_RE = re.compile(
    r"(?P<code>`(?P<codet>[^`]+)`)"
    r"|(?P<link>\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\))"
    r"|(?P<bi>\*\*\*(?P<bit>.+?)\*\*\*)"
    r"|(?P<b>\*\*(?P<bt>.+?)\*\*)"
    r"|(?P<b2>__(?P<b2t>.+?)__)"
    r"|(?P<i>(?<![\w*])\*(?P<it>[^*\s](?:[^*]*[^*\s])?)\*)"
    r"|(?P<i2>(?<![\w_])_(?P<i2t>[^_\s](?:[^_]*[^_\s])?)_)"
)
_LIST_RE = re.compile(r"(\s*)([-*+]|\d+[.)])\s+(.*)")
_RULE_RE = re.compile(r"(-{3,}|\*{3,}|_{3,})")


def _coalesce(spans: list[DocSpan]) -> tuple[DocSpan, ...]:
    merged: list[DocSpan] = []
    for span in spans:
        if not span.text:
            continue
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.bold == span.bold
            and prev.italic == span.italic
            and prev.code == span.code
            and prev.link == span.link
        ):
            merged[-1] = DocSpan(
                prev.text + span.text,
                bold=prev.bold,
                italic=prev.italic,
                code=prev.code,
                link=prev.link,
            )
        else:
            merged.append(span)
    return tuple(merged)


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in _split_table_row_raw(line)]
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells)


def _parse_plain_text(source: str) -> list[DocBlock]:
    blocks: list[DocBlock] = []
    for chunk in re.split(r"\n\s*\n", source.replace("\r\n", "\n").replace("\r", "\n")):
        text = " ".join(part.strip() for part in chunk.splitlines() if part.strip())
        if text:
            blocks.append(DocBlock(kind="paragraph", spans=(DocSpan(text),)))
    return blocks


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in _split_table_row_raw(line)]


def _split_table_row_raw(line: str) -> list[str]:
    trimmed = line.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    return trimmed.split("|")
