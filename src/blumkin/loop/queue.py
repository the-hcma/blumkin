"""File-backed work queue for the foreground loop runner (issue #501).

Cron (#502) and mail (#503) feeders enqueue entries here. `run_foreground` drains the
queue and dispatches each entry through `run_trigger`. An entry carries the trigger
name plus optional mail context (message id, profile, subject) so a handler can find
the intake folder and the instance resolver can read the subject (issue #504). The
same task instance is still serialized by the runner's instance lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir
from blumkin.loop.types import RunContext

MAX_SUBJECT_CHARS = 512

_LOCK_FILENAME = "queue.lock"
_QUEUE_FILENAME = "queue.jsonl"
_STORE_DIRNAME = "loop"


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One pending run: the trigger name and, for mail triggers, where it came from."""

    trigger: str
    message_id: str | None = None
    profile: str | None = None
    subject: str | None = None

    def as_context(self) -> RunContext:
        """The mail context a runner hands to the handler (config dir and instance come later)."""
        return RunContext(message_id=self.message_id, profile=self.profile, subject=self.subject)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"trigger": self.trigger}
        for key in ("message_id", "profile", "subject"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


class LoopQueueError(ValueError):
    """The work queue on disk is not acceptable."""


def drain_queue(config_dir: Path) -> list[QueueEntry]:
    """Remove and return every pending entry (FIFO), under an exclusive lock."""
    directory = _ensure(config_dir)
    with _locked(directory):
        path = directory / _QUEUE_FILENAME
        if not path.is_file() or path.is_symlink():
            return []
        entries = _read_entries(path)
        _rewrite(path, [])
        return entries


def enqueue(config_dir: Path, entry: QueueEntry) -> None:
    """Append one entry to the work queue (an over-long subject is cut)."""
    directory = _ensure(config_dir)
    if entry.subject is not None and len(entry.subject) > MAX_SUBJECT_CHARS:
        entry = QueueEntry(
            message_id=entry.message_id,
            profile=entry.profile,
            subject=entry.subject[:MAX_SUBJECT_CHARS],
            trigger=entry.trigger,
        )
    with _locked(directory):
        path = directory / _QUEUE_FILENAME
        entries = _read_entries(path) if path.is_file() and not path.is_symlink() else []
        entries.append(entry)
        _rewrite(path, entries)


def peek_queue(config_dir: Path) -> list[QueueEntry]:
    """Pending entries without removing them."""
    directory = config_dir / _STORE_DIRNAME
    path = directory / _QUEUE_FILENAME
    if not path.is_file() or path.is_symlink():
        return []
    with _locked(directory):
        return _read_entries(path)


def requeue_front(config_dir: Path, entries: list[QueueEntry]) -> None:
    """Put undispatched entries back at the head of the work queue."""
    if not entries:
        return
    directory = _ensure(config_dir)
    with _locked(directory):
        path = directory / _QUEUE_FILENAME
        pending = _read_entries(path) if path.is_file() and not path.is_symlink() else []
        _rewrite(path, [*entries, *pending])


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


def _optional_string(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} is not a string")
    return value


def _read_entries(path: Path) -> list[QueueEntry]:
    entries: list[QueueEntry] = []
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
            entries.append(
                QueueEntry(
                    message_id=_optional_string(entry, "message_id"),
                    profile=_optional_string(entry, "profile"),
                    subject=_optional_string(entry, "subject"),
                    trigger=entry["trigger"],
                )
            )
        except (ValueError, TypeError) as exc:
            raise LoopQueueError(
                f"Expected a valid queue entry on line {number}, got {type(exc).__name__}"
            ) from exc
    return entries


def _rewrite(path: Path, entries: list[QueueEntry]) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        for entry in entries:
            line = json.dumps(entry.as_dict(), sort_keys=True) + "\n"
            payload = line.encode("utf-8")
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
