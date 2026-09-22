"""Per-profile record of when a draft/composed artifact was last written.

``emit`` skills (``mail.send-draft``, ``chat.send``, ``chat.edit``) must refuse
to act until the artifact has sat composed for at least
``preferences.confirm_cooldown_seconds``. That check needs to know *when* the
artifact was composed, so every skill that produces or edits one
(``mail.draft`` / ``mail.reply`` / ``mail.forward`` / ``mail.update-draft`` /
``chat.draft`` / ``chat.edit-draft``) records a timestamp here, keyed by the
artifact id blumkin already hands back to the caller.

Mail's server-side drafts already hold their own content, so mail only ever
records a bare timestamp. Teams/Graph has no server-side chat-draft concept,
so ``chat.draft`` / ``chat.edit-draft`` also stash the composed text and
resolved target as ``content`` - the only place that content lives before
``chat.send`` / ``chat.edit`` reads it back and emits.

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

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from blumkin.config import BlumkinConfig

# Floor for how long entries are kept - the effective prune window is
# max(this, preferences.confirm_cooldown_seconds), so a configured cooldown
# longer than this floor cannot be starved by the prune (issue #365 review).
# A composed artifact that has sat past the effective window either went out
# through another path or was abandoned; either way it no longer needs to
# gate anything, so there is no reason to keep it.
_MAX_ENTRY_AGE_SECONDS = 24 * 60 * 60


def clear_composed(config: BlumkinConfig, artifact_id: str) -> None:
    """Drop the recorded timestamp (and any stashed content) for ``artifact_id``
    (after a successful emit/delete)."""
    if not artifact_id:
        return

    def _pop(entries: dict[str, dict[str, Any]]) -> None:
        entries.pop(artifact_id, None)

    _locked_update(config, _pop)


def composed_content(config: BlumkinConfig, artifact_id: str) -> Any | None:
    """The ``content`` stashed by ``record_composed(..., content=...)`` for
    ``artifact_id``, or ``None`` if it was never recorded with content (mail's
    server-side drafts hold their own content and never pass one) or the
    record has since been pruned/cleared."""
    entry = _load(config).get(artifact_id)
    return entry.get("content") if entry is not None else None


def record_composed(config: BlumkinConfig, artifact_id: str, *, content: Any | None = None) -> None:
    """Stamp ``artifact_id`` as composed *now*; a re-compose/edit resets the clock.

    ``content`` is opaque JSON-serializable data a compose-only skill (``chat.draft``,
    ``chat.edit-draft``) needs its emit counterpart to read back later, since
    Teams/Graph holds no server-side draft of its own. Mail leaves it unset - a
    Graph/Gmail draft already holds its own content.
    """
    if not artifact_id:
        return
    entry = {"composed_at": _now_iso(), "content": content}
    _locked_update(config, lambda entries: entries.__setitem__(artifact_id, entry))


def seconds_since_composed(config: BlumkinConfig, artifact_id: str) -> float | None:
    """Seconds elapsed since ``artifact_id`` was last recorded, or ``None`` if unknown."""
    entry = _load(config).get(artifact_id)
    if entry is None:
        return None
    raw = entry.get("composed_at")
    if not isinstance(raw, str):
        return None
    try:
        composed_at = datetime.fromisoformat(raw)
        return (datetime.now(UTC) - composed_at).total_seconds()
    except ValueError, TypeError:
        # A naive (tz-less) timestamp parses fine but cannot be subtracted from
        # an aware ``now()`` - treat it the same as any other unprovable value.
        return None


def _load(config: BlumkinConfig) -> dict[str, dict[str, Any]]:
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
    fresh: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        # Pre-#365-chat-split entries were a bare ISO string (timestamp only, no
        # content) - normalize on read so a state file written by an older
        # install does not get silently dropped.
        entry = {"composed_at": value, "content": None} if isinstance(value, str) else value
        if not isinstance(entry, dict):
            continue
        composed_at = entry.get("composed_at")
        if not isinstance(composed_at, str):
            continue
        try:
            age = (cutoff - datetime.fromisoformat(composed_at)).total_seconds()
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _save(config: BlumkinConfig, entries: dict[str, dict[str, Any]]) -> None:
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
