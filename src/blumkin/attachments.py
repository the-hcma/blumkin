"""Filesystem helpers shared by mail and chat attachment downloads.

Attachment names are attacker-controlled (set by the sender), so every path here
sanitizes names, refuses to escape the output directory, and never overwrites an
existing entry.
"""

from __future__ import annotations

import re
from pathlib import Path


def existing_entry_names(directory: Path) -> set[str]:
    """Names of every entry in ``directory`` (files and directories)."""
    return {path.name for path in directory.iterdir()}


def out_is_directory_intent(out: str, out_path: Path) -> bool:
    """True when ``--out`` names a directory (trailing separator or existing dir)."""
    if out.rstrip().endswith(("/", "\\")):
        return True
    return out_path.exists() and out_path.is_dir()


def prepare_download_directory(out: str, *, flag: str = "--all") -> Path:
    out_path = Path(out)
    if out_path.exists() and not out_path.is_dir():
        raise ValueError(f"--out must be a directory when using {flag}")
    out_path.mkdir(parents=True, exist_ok=True)
    if not out_path.is_dir():
        raise ValueError(f"--out must be a directory when using {flag}")
    return out_path


def resolve_attachment_dest(out_dir: Path, filename: str) -> Path:
    dest = (out_dir / filename).resolve()
    if not dest.is_relative_to(out_dir.resolve()):
        raise ValueError(f"invalid attachment filename: {filename}")
    return dest


def resolve_single_download_dest(out: str, filename: str) -> Path:
    """Destination for a one-attachment download, where ``--out`` may name a file or a directory."""
    out_path = Path(out)
    if not out_is_directory_intent(out, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return out_path
    if not out_path.exists():
        out_path.mkdir(parents=True, exist_ok=True)
    if not out_path.is_dir():
        raise ValueError("--out must be a directory")
    unique = unique_filename(sanitize_attachment_filename(filename), existing_entry_names(out_path))
    return resolve_attachment_dest(out_path, unique)


def sanitize_attachment_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\- ()]", "_", name.strip()) or "attachment"
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    if cleaned in {".", ".."} or cleaned.strip(".") == "":
        return "attachment"
    return cleaned


def unique_filename(name: str, used: set[str]) -> str:
    """Return ``name`` or a ``_2``-suffixed variant not already in ``used``.

    Comparison is case-folded because macOS (APFS) and Windows collide on case.
    """
    folded_used = {entry.casefold() for entry in used}
    if name.casefold() not in folded_used:
        used.add(name)
        return name
    stem = Path(name).stem or "attachment"
    suffix = Path(name).suffix
    index = 2
    while True:
        candidate = f"{stem}_{index}{suffix}"
        if candidate.casefold() not in folded_used:
            used.add(candidate)
            folded_used.add(candidate.casefold())
            return candidate
        index += 1
