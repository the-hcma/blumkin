"""``email-context.md`` - operator-curated name/alias -> address + notes.

An optional plain-markdown file in the config dir (and, merged on top, the active
profile's subdir). blumkin never writes it. It is consumed only by the
``people.context`` read skill: recipient resolution is the agent's job - it
fuzzy-matches this data, confirms the address with the user, then calls
mail/calendar with a real SMTP address. blumkin does no name -> address
substitution of its own.

Two row forms are accepted in the same file:

    | Name | Aliases  | Email           | Notes                         |
    |------|----------|-----------------|-------------------------------|
    | Sam  | sammy, S | sam@example.com | Colleague on the Foo project. |

    - Sam (sammy, S) <sam@example.com> - colleague on the Foo project
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.config import BlumkinConfig
from blumkin.output import emit_warning, sanitize_terminal

CONTEXT_FILENAME = "email-context.md"


@dataclass(frozen=True, slots=True)
class Contact:
    """One merged entry. ``sources`` lists every file it came from; ``conflict`` is
    set when the same name/alias carried a different address across files."""

    aliases: tuple[str, ...]
    conflict: bool
    email: str
    name: str
    notes: str
    sources: tuple[str, ...]


def format_people_context_human(payload: dict[str, Any]) -> list[str]:
    contacts = payload.get("contacts") or []
    if not contacts:
        return [f"(no contacts - no {CONTEXT_FILENAME}, or nothing matched)"]
    lines: list[str] = []
    for contact in contacts:
        alias = f" ({', '.join(contact['aliases'])})" if contact.get("aliases") else ""
        lines.append(sanitize_terminal(f"{contact['name']}{alias} <{contact['email']}>"))
        if contact.get("notes"):
            lines.append(sanitize_terminal(f"  {contact['notes']}"))
        if contact.get("conflict"):
            lines.append(f"  ! conflicting entries across: {', '.join(contact['sources'])}")
    return lines


def load_context(config: BlumkinConfig) -> list[Contact]:
    """Every contact, config-dir file first then the profile file merged on top."""
    merged: dict[str, Contact] = {}
    for path in locate_operator_files(config, CONTEXT_FILENAME):
        for row in _parse(path):
            _merge(merged, row, path)
    return sorted(merged.values(), key=lambda contact: contact.name.lower())


def locate_operator_files(config: BlumkinConfig, filename: str) -> list[Path]:
    """Existing operator-config files for ``filename``, base (config dir) first,
    then the active profile's dir. De-duplicated - a legacy-flat config makes the
    two directories the same path."""
    found: list[Path] = []
    for directory in (config.config_dir, config.profile_dir):
        candidate = directory / filename
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


async def people_context(*, config: BlumkinConfig, name: str | None = None) -> dict[str, Any]:
    """``people.context`` handler - the whole blumkin surface for the file."""
    contacts = load_context(config)
    if name is not None and name.strip():
        needle = name.strip().lower()
        contacts = [
            contact
            for contact in contacts
            if contact.name.lower() == needle
            or any(alias.lower() == needle for alias in contact.aliases)
        ]
    return {
        "ok": True,
        "contacts": [
            {
                "aliases": list(contact.aliases),
                "conflict": contact.conflict,
                "email": contact.email,
                "name": contact.name,
                "notes": contact.notes,
                "sources": list(contact.sources),
            }
            for contact in contacts
        ],
    }


@dataclass(frozen=True, slots=True)
class _Row:
    aliases: tuple[str, ...]
    email: str
    name: str
    notes: str = ""


_BULLET_RE = re.compile(
    r"""^\s*[-*+]\s+
        (?P<name>[^<(]+?)\s*
        (?:\((?P<aliases>[^)]*)\))?\s*
        <(?P<email>[^>]+)>\s*
        (?:[-–—:]\s*(?P<notes>.*\S))?\s*$""",
    re.VERBOSE,
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _bullet_line(line: str, path: Path) -> _Row | None:
    match = _BULLET_RE.match(line)
    if match is None:
        return None
    email = match["email"].strip()
    if not _EMAIL_RE.match(email):
        emit_warning(f"{path}: bullet entry has no valid email, skipped: {line}")
        return None
    return _Row(
        aliases=_split_aliases(match["aliases"] or ""),
        email=email,
        name=match["name"].strip(),
        notes=(match["notes"] or "").strip(),
    )


def _merge(merged: dict[str, Contact], row: _Row, path: Path) -> None:
    key = row.name.lower()
    src = str(path)
    existing = merged.get(key)
    if existing is None:
        merged[key] = Contact(
            aliases=row.aliases,
            conflict=False,
            email=row.email,
            name=row.name,
            notes=row.notes,
            sources=(src,),
        )
        return
    same_email = existing.email.lower() == row.email.lower()
    merged[key] = Contact(
        aliases=tuple(dict.fromkeys((*existing.aliases, *row.aliases))),
        conflict=existing.conflict or not same_email,
        email=existing.email,
        name=existing.name,
        notes=existing.notes or row.notes,
        sources=(*existing.sources, src),
    )


def _parse(path: Path) -> list[_Row]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        emit_warning(f"could not read {path}: {exc}")
        return []
    rows: list[_Row] = []
    header: dict[str, int] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("|"):
            header, row = _table_line(line, header, path)
            if row is not None:
                rows.append(row)
            continue
        if line[:1] in "-*+" and line[1:2] == " ":
            row = _bullet_line(line, path)
            if row is not None:
                rows.append(row)
    return rows


def _split_aliases(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[;,]", value) if part.strip())


def _table_line(
    line: str, header: dict[str, int] | None, path: Path
) -> tuple[dict[str, int] | None, _Row | None]:
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    if all(set(cell) <= set("-: ") for cell in cells):
        return header, None  # |---|---| divider
    lowered = [cell.lower() for cell in cells]
    if header is None:
        if "name" in lowered and "email" in lowered:
            return {col: i for i, col in enumerate(lowered)}, None
        emit_warning(f"{path}: table row before a Name|Email header, skipped: {line}")
        return None, None
    name = cells[header["name"]].strip() if header["name"] < len(cells) else ""
    email = cells[header["email"]].strip() if header["email"] < len(cells) else ""
    if not name:
        return header, None
    if not _EMAIL_RE.match(email):
        emit_warning(f"{path}: row for {name!r} has no valid email, skipped")
        return header, None
    aliases = (
        cells[header["aliases"]] if "aliases" in header and header["aliases"] < len(cells) else ""
    )
    notes = cells[header["notes"]] if "notes" in header and header["notes"] < len(cells) else ""
    return header, _Row(
        aliases=_split_aliases(aliases), email=email, name=name, notes=notes.strip()
    )
