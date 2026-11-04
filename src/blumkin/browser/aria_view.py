"""Narrow, shorten and page an ARIA snapshot for `browser read` (issue #606).

A long form's snapshot can pass the read cap before the control that matters (a dialog's own
`OK`) is reached, mostly because every combobox lists all its options. These are plain text
transformations on the snapshot already read, so none of them touches the page.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

#: One snapshot line: indentation, `- `, then the node (a role, an optional quoted name, then
#: attributes or text). Playwright wraps the whole node in single quotes, doubling any single
#: quote inside, when the name holds a colon or other YAML-special text.
_LINE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_NODE = re.compile(r'^(?P<role>[A-Za-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_ROLE = re.compile(r"[A-Za-z]+")
_TEXT_LINE = re.compile(r"^ *- text: (?P<text>.*)$")
#: The bracketed state attributes right after a node's name (`[selected] [expanded]`), before
#: any `:` and inline text, which a page controls.
_ATTRIBUTES = re.compile(r"^(?:\s*\[[^\]]*\])*")
#: Roles a grid or table can have, and the controls whose place `grid_view` numbers.
_GRID_ROLES = frozenset({"grid", "table", "treegrid"})
_FIELD_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "textbox",
    }
)
_CELL_ROLES = frozenset({"cell", "columnheader", "gridcell", "rowheader"})
#: Controls whose children are option lists a summary can stand in for.
_OPTION_PARENTS = frozenset({"combobox", "listbox"})
#: How many names a "nothing matched" error lists, and how long each may be.
_LISTED = 10
_NAME_CHARS = 60


def collapse_options(snapshot: str) -> str:
    """Replace each combobox or listbox that only holds options by a one-line summary.

    `- combobox "Type":` with 27 `- option` children becomes
    `- combobox "Type" (27 options, selected "Lodging")`. A control with any other kind of
    child is left as it is.
    """
    lines = snapshot.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out: list[str] = []
    index = 0
    while index < len(lines):
        match = _parse(lines[index])
        if match is None or match.role not in _OPTION_PARENTS:
            out.append(lines[index])
            index += 1
            continue
        end = _subtree_end(lines, index)
        children = [_parse(line) for line in lines[index + 1 : end] if line.strip()]
        options = [child for child in children if child is not None]
        if (
            not options
            or len(options) != len(children)
            or any(child.role != "option" for child in options)
            or len({child.indent for child in options}) != 1
        ):
            # Not an option-only list: keep the line, and keep walking its children, so an
            # option-only list nested inside it is still summarized.
            out.append(lines[index])
            index += 1
            continue
        selected = [_quote(child.name) for child in options if child.selected]
        shown = ", ".join(selected) if selected else None
        noun = "option" if len(options) == 1 else "options"
        summary = f"{len(options)} {noun}, " + (f"selected {shown}" if shown else "none selected")
        head = lines[index].rstrip()
        head = head[:-1] if head.endswith(":") else head
        out.append(f"{head} ({summary})")
        index = end
    return "\n".join(out) + ("\n" if snapshot.endswith("\n") else "")


def parse_within(spec: str) -> tuple[str, str | None]:
    """`ROLE` or `ROLE:NAME` as (role, name); the name may itself hold colons."""
    role, _, name = spec.partition(":")
    role = role.strip().lower()
    if not _ROLE.fullmatch(role):
        raise ValueError(f"Expected --within as ROLE or ROLE:NAME (a dialog, say), got {spec!r}")
    return role, (name.strip() or None)


def scope(snapshot: str, role: str, name: str | None = None) -> tuple[str, int]:
    """The first node of `role` (and exact accessible `name`, when given), with its children.

    Also returns how many nodes matched, since a page can hold several (a stale dialog next to
    the open one) and only the first is kept. Raises `LookupError`, naming the names that role
    does have, when nothing matches.
    """
    lines = snapshot.split("\n")
    seen: list[str] = []
    first = ""
    matches = 0
    for index, line in enumerate(lines):
        node = _parse(line)
        if node is None or node.role != role:
            continue
        if name is None or node.name == name:
            matches += 1
            if matches == 1:
                block = lines[index : _subtree_end(lines, index)]
                first = "\n".join(item[node.indent :] for item in block).rstrip() + "\n"
        elif node.name is not None:
            seen.append(node.name)
    if matches:
        return first, matches
    asked = f"a {role}" if name is None else f"a {role} named {name!r}"
    listed = ", ".join(repr(_one_line(item)) for item in seen[:_LISTED])
    more = f" (and {len(seen) - _LISTED} more)" if len(seen) > _LISTED else ""
    raise LookupError(f"Expected {asked}, got {listed + more if seen else 'none'}")


def grid_view(
    snapshot: str, role: str | None = None, name: str | None = None, *, max_rows: int = 200
) -> dict[str, Any]:
    """Rows, columns and the fields in each cell of the first grid (or table) in `snapshot`.

    A grid renders as one blob of text, so a caller cannot tell which `Project` field is on
    which row. Each field here carries `nth`: its 1-based place among every field of that role
    and name on the page, which is what `--role ROLE --nth N` counts in `fill`, `press`,
    `select` and `options`. Field values are never shown (the snapshot has none), and a cell
    that holds a field is not given its own name, which would repeat the value. Raises
    `LookupError`, naming the grids there are, when nothing matches.
    """
    lines = snapshot.split("\n")
    nodes = [_parse(line) for line in lines]
    counts: dict[tuple[str, str], int] = {}
    nth: dict[int, int] = {}
    for index, node in enumerate(nodes):
        if node is not None and node.role in _FIELD_ROLES:
            key = (node.role, node.name or "")
            counts[key] = counts.get(key, 0) + 1
            nth[index] = counts[key]
    kinds = {role} if role else _GRID_ROLES
    found = [
        index
        for index, node in enumerate(nodes)
        if node is not None and node.role in kinds and (name is None or node.name == name)
    ]
    if not found:
        listed = ", ".join(
            repr(_one_line(node.name or node.role))
            for node in nodes
            if node is not None and node.role in _GRID_ROLES
        )
        asked = "a grid or table" if role is None else f"a {role}"
        raise LookupError(f"Expected {asked}, got {listed or 'none'}")
    top = found[0]
    end = _subtree_end(lines, top)
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    truncated = False
    for index in range(top + 1, end):
        node = nodes[index]
        if node is None or node.role != "row":
            continue
        cells = _cells(lines, nodes, nth, index)
        headers = [cell for cell in cells if cell["header"]]
        if headers and not columns:
            columns = [str(cell["text"]) for cell in cells]
            continue
        if len(rows) >= max_rows:
            truncated = True
            break
        label = next((str(c["text"]) for c in cells if c["row_header"]), "")
        rows.append(
            {
                "row": len(rows) + 1,
                "label": label,
                "cells": [
                    {
                        "column": columns[position] if position < len(columns) else position + 1,
                        "text": cell["text"],
                        "fields": cell["fields"],
                    }
                    for position, cell in enumerate(cells)
                    if not cell["row_header"]
                ],
            }
        )
    top_node = nodes[top]
    assert top_node is not None
    return {
        "columns": columns,
        "grid": {"name": _one_line(top_node.name or ""), "role": top_node.role},
        "matches": len(found),
        "rows": rows,
        "truncated": truncated,
    }


def window(text: str, offset: int, limit: int) -> tuple[str, bool, int | None]:
    """`text[offset:offset+limit]`, whether more follows, and where the next window starts."""
    if offset < 0:
        raise ValueError(f"Expected an offset that is not negative, got {offset}")
    if limit < 1:
        raise ValueError(f"Expected a limit of at least 1, got {limit}")
    piece = text[offset : offset + limit]
    end = offset + len(piece)
    more = end < len(text)
    return piece, more, (end if more else None)


class _Node(NamedTuple):
    """The parts of one snapshot line that `scope` and `collapse_options` look at."""

    indent: int
    role: str
    name: str | None
    selected: bool


def _parse(line: str) -> _Node | None:
    """The node a snapshot line describes, or None for a line that is not one (`- /url:`)."""
    match = _LINE.match(line)
    if match is None:
        return None
    body = match["body"]
    rest_after_key = ""
    if body.startswith("'"):
        end = _closing_quote(body)
        if end is None:
            return None
        rest_after_key = body[end + 1 :]
        body = body[1:end].replace("''", "'")
    node = _NODE.match(body)
    if node is None:
        return None
    rest = node["rest"] + rest_after_key
    return _Node(
        indent=len(match["indent"]),
        role=node["role"].lower(),
        name=_unquote(node["name"]),
        selected="[selected]" in _attributes(rest),
    )


def _cells(
    lines: list[str], nodes: list[_Node | None], nth: dict[int, int], row: int
) -> list[dict[str, Any]]:
    """The cells that sit directly in the row at `row`, each with its fields and plain text."""
    row_node = nodes[row]
    assert row_node is not None
    end = _subtree_end(lines, row)
    cells: list[dict[str, Any]] = []
    index = row + 1
    while index < end:
        node = nodes[index]
        if node is None or node.role not in _CELL_ROLES or node.indent <= row_node.indent:
            index += 1
            continue
        cell_end = _subtree_end(lines, index)
        fields = [
            {"name": _one_line(inner.name or ""), "nth": nth[at], "role": inner.role}
            for at in range(index + 1, cell_end)
            if (inner := nodes[at]) is not None and inner.role in _FIELD_ROLES
        ]
        # A cell's own name repeats the value of a field inside it, which is never shown, so a
        # cell with anything inside is described by its plain text children alone (and the
        # text a custom field shows as its value is skipped with that field).
        if cell_end > index + 1:
            texts: list[str] = []
            skip_until = index + 1
            for at in range(index + 1, cell_end):
                if at < skip_until:
                    continue
                inner = nodes[at]
                if inner is not None and inner.role in _FIELD_ROLES - {
                    "button",
                    "link",
                    "menuitem",
                }:
                    skip_until = _subtree_end(lines, at)
                    continue
                if (found := _TEXT_LINE.match(lines[at])) is not None:
                    texts.append(found["text"])
            text = _one_line(" ".join(texts))
        else:
            text = _one_line(node.name or "")
        cells.append(
            {
                "fields": fields,
                "header": node.role == "columnheader",
                "row_header": node.role == "rowheader",
                "text": text,
            }
        )
        index = cell_end
    return cells


def _attributes(rest: str) -> str:
    """The leading `[state]` attributes of what follows a node's name, not its inline text."""
    found = _ATTRIBUTES.match(rest)
    return found[0] if found else ""


def _closing_quote(body: str) -> int | None:
    """The index of the single quote that ends a YAML single-quoted key (`''` is a quote)."""
    index = 1
    while index < len(body):
        if body[index] == "'":
            if body[index + 1 : index + 2] != "'":
                return index
            index += 2
            continue
        index += 1
    return None


def _one_line(text: str) -> str:
    """A page-supplied name as one printable line of at most `_NAME_CHARS` characters."""
    printable = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(printable.split())[:_NAME_CHARS]


def _quote(name: str | None) -> str:
    """A name as the snapshot writes it: in double quotes, with `"` and `\\` escaped."""
    text = (name or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _subtree_end(lines: list[str], start: int) -> int:
    """The index after the node at `start` and every deeper-indented line below it."""
    base = len(lines[start]) - len(lines[start].lstrip(" "))
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip(" ")) <= base:
            break
        end += 1
    return end


def _unquote(name: str | None) -> str | None:
    """A quoted accessible name with its escaped quotes and backslashes undone."""
    if name is None:
        return None
    return re.sub(r'\\(["\\])', r"\1", name)
