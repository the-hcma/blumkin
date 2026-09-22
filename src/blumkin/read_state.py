"""Per-profile record of when a calendar event was last freshly read.

``calendar.accept`` / ``calendar.decline`` / ``calendar.tentative`` /
``calendar.cancel`` must refuse to act on a single ``--event-id`` until the
agent has looked at that event's current state (attendees, time,
cancellation status) via ``calendar.get`` within
``preferences.rsvp_freshness_seconds`` - see issue #365. That freshness gate
needs to know *when* the event was last read, so ``calendar.get`` records a
timestamp here, keyed by the event id blumkin already hands back to the
caller.

This is deliberately a separate store from ``blumkin.compose_state``: that
module's records mean "an artifact was composed/drafted and must now sit for
a cooldown before emit", the opposite direction of "an event was read and
that read is only good for a limited window before acting on it again". Same
underlying lock-file-guarded atomic-JSON-write mechanics as
``compose_state.py`` (duplicated here rather than shared, since threading a
second semantic through that already-reviewed module was judged more
confusing than a small amount of repeated plumbing), but a distinct on-disk
file and API so the two concepts can never be conflated by a caller reading
the wrong module's docstring.

The file lives next to the token cache under ``~/.config/blumkin/`` (never
committed) and is per-profile. A missing record for a given event id (never
read through this install, or the record was pruned/lost) is treated as
"unknown" - ``seconds_since_read`` returns ``None`` and the freshness gate
raises (fails *closed*, unlike the compose cooldown gate) since acting on
unprovably-stale event data is exactly what issue #365 asks to prevent.

Every write goes through ``_locked_update``: a cross-process lock file guards
the load-modify-save transaction so two concurrent MCP sessions reading
different event ids cannot clobber each other's timestamp, and the
replacement file is written atomically and durably (temp file, ``fsync``,
rename).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from blumkin.config import BlumkinConfig

# Floor for how long entries are kept - the effective prune window is
# max(this, preferences.rsvp_freshness_seconds), so a configured freshness
# window longer than this floor cannot be starved by the prune (mirrors
# compose_state.py's _MAX_ENTRY_AGE_SECONDS reasoning).
_MAX_ENTRY_AGE_SECONDS = 24 * 60 * 60


def clear_read(config: BlumkinConfig, event_id: str) -> None:
    """Drop the recorded read timestamp for ``event_id`` (e.g. after it is cancelled)."""
    if not event_id:
        return

    def _pop(entries: dict[str, dict[str, Any]]) -> None:
        entries.pop(event_id, None)

    _locked_update(config, _pop)


def record_read(config: BlumkinConfig, event_id: str) -> None:
    """Stamp ``event_id`` as freshly read *now*; a re-read resets the clock."""
    if not event_id:
        return
    entry = {"read_at": _now_iso()}
    _locked_update(config, lambda entries: entries.__setitem__(event_id, entry))


def seconds_since_read(config: BlumkinConfig, event_id: str) -> float | None:
    """Seconds elapsed since ``event_id`` was last read via ``calendar.get``, or ``None``."""
    entry = _load(config).get(event_id)
    if entry is None:
        return None
    raw = entry.get("read_at")
    if not isinstance(raw, str):
        return None
    try:
        read_at = datetime.fromisoformat(raw)
        return (datetime.now(UTC) - read_at).total_seconds()
    except ValueError, TypeError:
        # A naive (tz-less) timestamp parses fine but cannot be subtracted from
        # an aware ``now()`` - treat it the same as any other unprovable value.
        return None


def _load(config: BlumkinConfig) -> dict[str, dict[str, Any]]:
    path = config.read_state_path
    try:
        raw = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    max_age = max(_MAX_ENTRY_AGE_SECONDS, config.preferences.rsvp_freshness_seconds)
    cutoff = datetime.now(UTC)
    fresh: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        read_at = entry.get("read_at")
        if not isinstance(read_at, str):
            continue
        try:
            age = (cutoff - datetime.fromisoformat(read_at)).total_seconds()
        except ValueError, TypeError:
            continue
        if age <= max_age:
            fresh[key] = entry
    return fresh


def _lock(lock_file: Any) -> None:
    if sys.platform == "win32":
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(lock_file, fcntl.LOCK_EX)


def _locked_update(
    config: BlumkinConfig, mutate: Callable[[dict[str, dict[str, Any]]], None]
) -> None:
    """Run one load-mutate-save transaction under a cross-process advisory lock.

    See ``compose_state._locked_update`` - identical reasoning, separate file.
    """
    path = config.read_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w") as lock_file:
        _lock(lock_file)
        try:
            entries = _load(config)
            mutate(entries)
            _save(config, entries)
        finally:
            _unlock(lock_file)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _save(config: BlumkinConfig, entries: dict[str, dict[str, Any]]) -> None:
    """Write ``entries`` atomically and durably - see ``compose_state._save``."""
    path = config.read_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = entries
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_file:
            tmp_file.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
        if sys.platform != "win32":
            # Windows has no directory-fd concept and metadata durability there is
            # a platform-level guarantee, not something to fsync by hand here.
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)


def _unlock(lock_file: Any) -> None:
    if sys.platform == "win32":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
