"""``email-context.md`` - operator-curated name/alias -> address + notes.

An optional plain-markdown file in the config dir; the active profile's copy
(``profiles/<name>/email-context.md``) is read too and their entries combined.
blumkin never writes it. It is consumed only by the ``people.context`` read
skill: recipient resolution is the agent's job - it
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
    """One resolved entry. ``sources`` lists every file it came from; ``conflict``
    is set when this name has more than one variant (a different address, or two
    different non-empty notes) - the caller should show all and let the operator
    reconcile, never pick one."""

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
    """Every contact from the config-dir file merged with the active profile's file.

    Rows for one name that agree (same address; notes fill a blank or match) are
    merged. Rows that clash - a different address, or two different non-empty
    notes - are kept as **separate** entries, all flagged ``conflict: true`` so
    the caller can show both and the operator can reconcile the files. blumkin
    never picks one.
    """
    variants: dict[str, list[_Acc]] = {}
    for path in locate_operator_files(config, CONTEXT_FILENAME):
        for row in _parse(path):
            _absorb(variants.setdefault(row.name.lower(), []), row, str(path))
    contacts: list[Contact] = []
    for accs in variants.values():
        clash = len(accs) > 1
        contacts.extend(
            Contact(
                aliases=tuple(acc.aliases),
                conflict=clash or acc.note_clash,
                email=acc.email,
                name=acc.name,
                notes=acc.notes,
                sources=tuple(acc.sources),
            )
            for acc in accs
        )
    return sorted(contacts, key=lambda contact: (contact.name.lower(), contact.email.lower()))


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


@dataclass(slots=True)
class _Acc:
    """A mutable accumulator for one (name, address) while merging files."""

    aliases: list[str]
    email: str
    name: str
    note_clash: bool
    notes: str
    sources: list[str]


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


def _absorb(accs: list[_Acc], row: _Row, src: str) -> None:
    for acc in accs:
        if acc.email.lower() != row.email.lower():
            continue
        acc.aliases = list(dict.fromkeys((*acc.aliases, *row.aliases)))
        acc.sources.append(src)
        if not acc.notes:
            acc.notes = row.notes
        elif row.notes and row.notes != acc.notes:
            acc.note_clash = True
        return
    accs.append(
        _Acc(
            aliases=list(row.aliases),
            email=row.email,
            name=row.name,
            note_clash=False,
            notes=row.notes,
            sources=[src],
        )
    )


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


def _parse(path: Path) -> list[_Row]:
    try:
        # utf-8-sig strips a BOM (Windows editors add one, and U+FEFF is not
        # whitespace so it would break the first header row). errors="replace"
        # keeps a cp1252/UTF-16 file from aborting the whole skill; a byte it had
        # to replace is warned about, and any row that ends up malformed is
        # skipped with its own warning.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        emit_warning(f"could not read {path}: {exc}")
        return []
    if "�" in text:
        emit_warning(f"{path}: not valid UTF-8; some characters were replaced")
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
    # A row that names both columns and holds no address is a (possibly second)
    # header - re-detect it so a later table with different columns is not parsed
    # with the first table's indices.
    if {"name", "email"} <= set(lowered) and not any("@" in cell for cell in cells):
        return {col: i for i, col in enumerate(lowered)}, None
    if header is None:
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
