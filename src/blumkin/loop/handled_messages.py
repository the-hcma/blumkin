"""Durable set of mailbox message ids the loop has already acted on (issue #503).

Keys are ``{profile}:{message_id}`` so the same Graph/Gmail id in two profiles
cannot collide. Restart and re-poll never enqueue a handled message again.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir

_HANDLED_FILENAME = "handled-messages.json"
_LOCK_FILENAME = "handled-messages.lock"
_STORE_DIRNAME = "loop"


class LoopHandledError(ValueError):
    """The handled-message store on disk is not acceptable."""


def is_handled(config_dir: Path, *, profile: str, message_id: str) -> bool:
    key = _key(profile, message_id)
    with _locked(config_dir) as path:
        return key in _read(path)


def mark_handled(config_dir: Path, *, profile: str, message_id: str) -> bool:
    """Record a handled message. Returns True when the key was newly added."""
    key = _key(profile, message_id)
    with _locked(config_dir) as path:
        keys = _read(path)
        if key in keys:
            return False
        keys.add(key)
        _rewrite(path, keys)
        return True


def _ensure(config_dir: Path) -> Path:
    try:
        return ensure_private_dir(config_dir / _STORE_DIRNAME)
    except BrowserProfileError as exc:
        raise LoopHandledError(str(exc)) from exc


def _key(profile: str, message_id: str) -> str:
    if not profile.strip() or not message_id.strip():
        raise LoopHandledError("profile and message_id must be non-empty")
    return f"{profile.strip()}:{message_id.strip()}"


@contextlib.contextmanager
def _locked(config_dir: Path) -> Iterator[Path]:
    directory = _ensure(config_dir)
    fd = os.open(directory / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory / _HANDLED_FILENAME
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _no_constants(name: str) -> Any:
    raise ValueError(f"unexpected constant {name}")


def _read(path: Path) -> set[str]:
    if not path.is_file() or path.is_symlink():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_constants)
    except (OSError, ValueError) as exc:
        raise LoopHandledError(f"Expected readable handled-messages at {path}, got {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("ids"), list):
        raise LoopHandledError(f"Expected an object with an ids list in {path}")
    ids: set[str] = set()
    for entry in raw["ids"]:
        if not isinstance(entry, str) or not entry.strip():
            raise LoopHandledError(f"Expected string message keys in {path}")
        ids.add(entry.strip())
    return ids


def _rewrite(path: Path, keys: set[str]) -> None:
    tmp = path.with_suffix(".tmp")
    payload = json.dumps({"ids": sorted(keys)}, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(payload.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
