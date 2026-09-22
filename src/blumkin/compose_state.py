"""Per-profile record of when a draft/composed artifact was last written.

``emit`` skills (``mail.send-draft``, and future compose/emit counterparts for
chat and calendar - see issue #365) must refuse to act until the artifact has
sat composed for at least ``preferences.confirm_cooldown_seconds``. That check
needs to know *when* the artifact was composed, so every skill that produces or
edits one (``mail.draft`` / ``mail.reply`` / ``mail.forward`` / ``mail.update-draft``)
records a timestamp here, keyed by the artifact id blumkin already hands back to
the caller.

The file lives next to the token cache under ``~/.config/blumkin/`` (never
committed) and is per-profile. A missing record for a given id (never composed
through this install, or the record was pruned/lost) is treated as "unknown" -
``seconds_since_composed`` returns ``None`` and the cooldown gate fails open
rather than blocking forever on state it cannot prove.

Every write goes through ``_locked_update``: a cross-process lock file guards
the load-modify-save transaction so two concurrent MCP sessions writing
different artifact ids cannot clobber each other's timestamp (issue #365
review), and the replacement file is written atomically and durably (temp
file, ``fsync``, rename).
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

from blumkin.config import BlumkinConfig

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Floor for how long entries are kept - the effective prune window is
# max(this, preferences.confirm_cooldown_seconds), so a configured cooldown
# longer than this floor cannot be starved by the prune (issue #365 review).
# A composed artifact that has sat past the effective window either went out
# through another path or was abandoned; either way it no longer needs to
# gate anything, so there is no reason to keep it.
_MAX_ENTRY_AGE_SECONDS = 24 * 60 * 60


def clear_composed(config: BlumkinConfig, artifact_id: str) -> None:
    """Drop the recorded timestamp for ``artifact_id`` (after a successful emit/delete)."""
    if not artifact_id:
        return

    def _pop(entries: dict[str, str]) -> None:
        entries.pop(artifact_id, None)

    _locked_update(config, _pop)


def record_composed(config: BlumkinConfig, artifact_id: str) -> None:
    """Stamp ``artifact_id`` as composed *now*; a re-compose/edit resets the clock."""
    if not artifact_id:
        return
    _locked_update(config, lambda entries: entries.__setitem__(artifact_id, _now_iso()))


def seconds_since_composed(config: BlumkinConfig, artifact_id: str) -> float | None:
    """Seconds elapsed since ``artifact_id`` was last recorded, or ``None`` if unknown."""
    raw = _load(config).get(artifact_id)
    if raw is None:
        return None
    try:
        composed_at = datetime.fromisoformat(raw)
        return (datetime.now(UTC) - composed_at).total_seconds()
    except ValueError, TypeError:
        # A naive (tz-less) timestamp parses fine but cannot be subtracted from
        # an aware ``now()`` - treat it the same as any other unprovable value.
        return None


def _load(config: BlumkinConfig) -> dict[str, str]:
    path = config.compose_state_path
    try:
        raw = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    # The prune window must outlive the configured cooldown, or a draft composed
    # longer ago than _MAX_ENTRY_AGE_SECONDS but less than confirm_cooldown_seconds
    # would get pruned first and then fail open (issue #365 review).
    max_age = max(_MAX_ENTRY_AGE_SECONDS, config.preferences.confirm_cooldown_seconds)
    cutoff = datetime.now(UTC)
    fresh: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            composed_at = datetime.fromisoformat(value)
            age = (cutoff - composed_at).total_seconds()
        except ValueError, TypeError:
            continue
        if age <= max_age:
            fresh[key] = value
    return fresh


def _locked_update(config: BlumkinConfig, mutate: Callable[[dict[str, str]], None]) -> None:
    """Run one load-mutate-save transaction under a cross-process advisory lock.

    Two MCP sessions (each its own process) can otherwise both load the state
    file, mutate different artifact ids, and let the last writer's ``_save``
    discard the other's timestamp - silently defeating the cooldown for the
    discarded id (issue #365 review). The lock file is separate from the state
    file itself so a reader never has to take the lock just to ``_load``. Uses
    ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows.
    """
    path = config.compose_state_path
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


def _lock(lock_file: Any) -> None:
    if sys.platform == "win32":
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(lock_file, fcntl.LOCK_EX)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _save(config: BlumkinConfig, entries: dict[str, str]) -> None:
    """Write ``entries`` atomically and durably: a crash must never observe a torn
    write, and ``fsync`` before the rename means a crash right after this call
    cannot silently lose the just-recorded compose timestamp (a lost timestamp
    would make the cooldown gate fail open on the next run - issue #365 review)."""
    path = config.compose_state_path
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
