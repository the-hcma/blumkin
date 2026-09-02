"""pipx install detection helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

PIPX_BIN_DIR_TIMEOUT_S = 10
PIPX_UPGRADE_TIMEOUT_S = 180


def pipx_blumkin_path(*, pipx_bin: str | None = None) -> Path | None:
    """Return the pipx ``blumkin`` app path when it exists.

    Checks ``$PIPX_BIN_DIR``, then the directory pipx itself reports (so a bin
    dir set in ``pipx.ini`` / ``PIPX_HOME`` is honoured), then the
    ``~/.local/bin`` default. ``pipx_bin`` is the resolved pipx executable when
    the caller already has one.
    """
    for directory in _pipx_bin_dirs(pipx_bin):
        candidate = directory / "blumkin"
        if candidate.is_file():
            try:
                return candidate.resolve()
            except OSError:
                return candidate
    return None


def _pipx_bin_dirs(pipx_bin: str | None) -> Iterator[Path]:
    seen: set[Path] = set()
    override = os.environ.get("PIPX_BIN_DIR", "").strip()
    candidates = [
        Path(override) if override else None,
        _pipx_reported_bin_dir(pipx_bin),
        Path.home() / ".local" / "bin",
    ]
    for directory in candidates:
        if directory is not None and directory not in seen:
            seen.add(directory)
            yield directory


def _pipx_reported_bin_dir(pipx_bin: str | None) -> Path | None:
    executable = pipx_bin or shutil.which("pipx")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "environment", "--value", "PIPX_BIN_DIR"],
            capture_output=True,
            check=False,
            text=True,
            timeout=PIPX_BIN_DIR_TIMEOUT_S,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    return Path(value) if value else None
