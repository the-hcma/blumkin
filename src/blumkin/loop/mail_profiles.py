"""Per-mailbox profile pause state for the loop mail feeder (issue #503).

When silent credential renewal fails, that profile pauses; others keep running.
``blumkin loop init`` for the profile clears the pause after a successful login.
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
from blumkin.loop.mail_sync import clear_sync_state

_LOCK_FILENAME = "mail-profiles.lock"
_PAUSED_FILENAME = "mail-profiles-paused.json"
_STORE_DIRNAME = "loop"


class LoopMailProfileError(ValueError):
    """Mailbox profile pause state on disk is not acceptable."""


def is_profile_paused(config_dir: Path, profile: str) -> bool:
    return profile.strip() in list_paused_profiles(config_dir)


def list_paused_profiles(config_dir: Path) -> frozenset[str]:
    with _locked(config_dir) as path:
        return frozenset(_read(path))


def pause_profile(config_dir: Path, profile: str, *, reason: str = "renewal_failed") -> bool:
    """Pause one mailbox profile. Returns True when newly paused."""
    name = profile.strip()
    if not name:
        raise LoopMailProfileError("profile name must be non-empty")
    with _locked(config_dir) as path:
        paused = _read(path)
        if name in paused:
            return False
        paused[name] = reason
        _rewrite(path, paused)
        # A token kept across a pause would bring back a backlog on resume.
        clear_sync_state(config_dir, name)
        return True


def resume_profile(config_dir: Path, profile: str) -> bool:
    """Clear a mailbox profile pause. Returns True when it was paused."""
    name = profile.strip()
    if not name:
        raise LoopMailProfileError("profile name must be non-empty")
    with _locked(config_dir) as path:
        paused = _read(path)
        if name not in paused:
            return False
        del paused[name]
        _rewrite(path, paused)
        clear_sync_state(config_dir, name)
        return True


def _ensure(config_dir: Path) -> Path:
    try:
        return ensure_private_dir(config_dir / _STORE_DIRNAME)
    except BrowserProfileError as exc:
        raise LoopMailProfileError(str(exc)) from exc


@contextlib.contextmanager
def _locked(config_dir: Path) -> Iterator[Path]:
    directory = _ensure(config_dir)
    fd = os.open(directory / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory / _PAUSED_FILENAME
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _no_constants(name: str) -> Any:
    raise ValueError(f"unexpected constant {name}")


def _read(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_constants)
    except (OSError, ValueError) as exc:
        raise LoopMailProfileError(
            f"Expected readable mail profile state at {path}, got {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise LoopMailProfileError(f"Expected an object in {path}")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise LoopMailProfileError(f"Expected string profile names in {path}")
        out[key.strip()] = str(value) if value is not None else "paused"
    return out


def _rewrite(path: Path, paused: dict[str, str]) -> None:
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(paused, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(payload.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
