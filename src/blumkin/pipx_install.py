"""pipx install detection helpers."""

from __future__ import annotations

import os
from pathlib import Path

PIPX_UPGRADE_TIMEOUT_S = 180


def pipx_blumkin_path() -> Path | None:
    """Return the pipx ``blumkin`` app path when it exists."""
    override = os.environ.get("PIPX_BIN_DIR", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override) / "blumkin")
    candidates.append(Path.home() / ".local" / "bin" / "blumkin")
    for candidate in candidates:
        if candidate.is_file():
            try:
                return candidate.resolve()
            except OSError:
                return candidate
    return None
