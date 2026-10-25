"""File-backed work queue for the foreground loop runner (issue #501).

Cron (#502) and mail (#503) feeders enqueue trigger names here. `run_foreground`
drains the queue and dispatches each name through `run_trigger`. The same task
instance is still serialized by the runner's instance lock.
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

_LOCK_FILENAME = "queue.lock"
_QUEUE_FILENAME = "queue.jsonl"
_STORE_DIRNAME = "loop"


class LoopQueueError(ValueError):
    """The work queue on disk is not acceptable."""


def drain_queue(config_dir: Path) -> list[str]:
    """Remove and return every pending trigger name (FIFO), under an exclusive lock."""
    directory = _ensure(config_dir)
    with _locked(directory):
        path = directory / _QUEUE_FILENAME
        if not path.is_file() or path.is_symlink():
            return []
        names = _read_names(path)
        _rewrite(path, [])
        return names


def enqueue(config_dir: Path, trigger: str) -> None:
    """Append one trigger name to the work queue."""
    directory = _ensure(config_dir)
    with _locked(directory):
        path = directory / _QUEUE_FILENAME
        names = _read_names(path) if path.is_file() and not path.is_symlink() else []
        names.append(trigger)
        _rewrite(path, names)


def peek_queue(config_dir: Path) -> list[str]:
    """Pending trigger names without removing them."""
    directory = config_dir / _STORE_DIRNAME
    path = directory / _QUEUE_FILENAME
    if not path.is_file() or path.is_symlink():
        return []
    with _locked(directory):
        return _read_names(path)


def _ensure(config_dir: Path) -> Path:
    try:
        return ensure_private_dir(config_dir / _STORE_DIRNAME)
    except BrowserProfileError as exc:
        raise LoopQueueError(str(exc)) from exc


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    try:
        ensure_private_dir(directory)
    except BrowserProfileError as exc:
        raise LoopQueueError(str(exc)) from exc
    fd = os.open(directory / _LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _no_constants(name: str) -> Any:
    raise ValueError(f"unexpected constant {name}")


def _read_names(path: Path) -> list[str]:
    names: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LoopQueueError(f"Expected a readable work queue at {path}, got {exc}") from exc
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line, parse_constant=_no_constants)
            if not isinstance(entry, dict) or not isinstance(entry.get("trigger"), str):
                raise ValueError("not a trigger object")
            names.append(entry["trigger"])
        except (ValueError, TypeError) as exc:
            raise LoopQueueError(
                f"Expected a valid queue entry on line {number}, got {type(exc).__name__}"
            ) from exc
    return names


def _rewrite(path: Path, names: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        for name in names:
            line = json.dumps({"trigger": name}, sort_keys=True) + "\n"
            payload = line.encode("utf-8")
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
