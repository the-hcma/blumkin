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
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from blumkin.config import BlumkinConfig

# Entries older than this are pruned on every load/save - a composed artifact
# that has sat this long was either sent through another path or abandoned;
# either way it no longer needs to gate anything, so there's no reason to keep it.
_MAX_ENTRY_AGE_SECONDS = 24 * 60 * 60


def clear_composed(config: BlumkinConfig, artifact_id: str) -> None:
    """Drop the recorded timestamp for ``artifact_id`` (after a successful emit/delete)."""
    if not artifact_id:
        return
    entries = _load(config)
    if entries.pop(artifact_id, None) is not None:
        _save(config, entries)


def record_composed(config: BlumkinConfig, artifact_id: str) -> None:
    """Stamp ``artifact_id`` as composed *now*; a re-compose/edit resets the clock."""
    if not artifact_id:
        return
    entries = _load(config)
    entries[artifact_id] = _now_iso()
    _save(config, entries)


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
        if age <= _MAX_ENTRY_AGE_SECONDS:
            fresh[key] = value
    return fresh


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _save(config: BlumkinConfig, entries: dict[str, str]) -> None:
    path = config.compose_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = entries
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
