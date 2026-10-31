"""Narrow, shorten and page an ARIA snapshot for `browser read` (issue #606).

A long form's snapshot can pass the read cap before the control that matters (a dialog's own
`OK`) is reached, mostly because every combobox lists all its options. These are plain text
transformations on the snapshot already read, so none of them touches the page.
"""

from __future__ import annotations

import re
from typing import NamedTuple

#: One snapshot line: indentation, `- `, then the node (a role, an optional quoted name, then
#: attributes or text). Playwright wraps the whole node in single quotes, doubling any single
#: quote inside, when the name holds a colon or other YAML-special text.
_LINE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_NODE = re.compile(r'^(?P<role>[A-Za-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_ROLE = re.compile(r"[A-Za-z]+")
#: The bracketed state attributes right after a node's name (`[selected] [expanded]`), before
#: any `:` and inline text, which a page controls.
_ATTRIBUTES = re.compile(r"^(?:\s*\[[^\]]*\])*")
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
