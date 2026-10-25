"""Paused-trigger state for the loop runner (issue #501).

Stored at ``<config dir>/loop/paused.json`` as a sorted list of trigger names.
A paused trigger does not run (including catch-up later); resume leaves missed
runs alone unless a later command asks for catch-up.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir

_PAUSED_FILENAME = "paused.json"
_STORE_DIRNAME = "loop"


class LoopStateError(ValueError):
    """Paused-trigger state on disk is not acceptable."""


def is_paused(config_dir: Path, trigger: str) -> bool:
    return trigger in list_paused(config_dir)


def list_paused(config_dir: Path) -> frozenset[str]:
    path = config_dir / _STORE_DIRNAME / _PAUSED_FILENAME
    if not path.is_file() or path.is_symlink():
        return frozenset()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_constants)
    except (OSError, ValueError) as exc:
        raise LoopStateError(
            f"Expected readable paused-trigger state at {path}, got {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise LoopStateError(f"Expected an object in {path}, got {type(raw).__name__}")
    names = raw.get("paused")
    if names is None:
        return frozenset()
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise LoopStateError(f"Expected paused to be a list of strings in {path}")
    return frozenset(names)


def pause_trigger(config_dir: Path, trigger: str) -> bool:
    """Mark `trigger` paused. Returns True when this call changed state."""
    current = set(list_paused(config_dir))
    if trigger in current:
        return False
    current.add(trigger)
    _write(config_dir, current)
    return True


def resume_trigger(config_dir: Path, trigger: str) -> bool:
    """Clear pause on `trigger`. Returns True when this call changed state."""
    current = set(list_paused(config_dir))
    if trigger not in current:
        return False
    current.remove(trigger)
    _write(config_dir, current)
    return True


def _no_constants(name: str) -> Any:
    raise ValueError(f"unexpected constant {name}")


def _write(config_dir: Path, paused: set[str]) -> None:
    try:
        directory = ensure_private_dir(config_dir / _STORE_DIRNAME)
    except BrowserProfileError as exc:
        raise LoopStateError(str(exc)) from exc
    path = directory / _PAUSED_FILENAME
    payload = json.dumps({"paused": sorted(paused)}, sort_keys=True) + "\n"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        data = payload.encode("utf-8")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
