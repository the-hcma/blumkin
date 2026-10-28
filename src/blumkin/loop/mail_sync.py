"""Per-profile mail sync state for the loop's incremental polling (issue #525).

One small JSON file per mailbox profile under `<config dir>/loop/mail-sync/` (directory
`0700`, file `0600`, written atomically) holds the provider's opaque sync token, which mode
the last poll used (`delta` for an incremental read, `scan` when it had to list the newest
messages) and when it last ran. The token is private to the runner: listings for `loop
status` never include it. The file name is a hash of the profile name, so a profile name can
never escape the folder. A file that is damaged, or was written for another provider, counts
as no state, which makes the next poll take a fresh token.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir, write_private

_DIRNAME = "mail-sync"
_LOOP_DIRNAME = "loop"
_MODES = frozenset({"delta", "scan"})


@dataclass(frozen=True, slots=True)
class SyncState:
    """What the runner remembers about one mailbox profile between polls."""

    last_poll: str
    mode: str
    provider: str
    token: str


def clear_sync_state(config_dir: Path, profile: str) -> None:
    """Forget the profile's token (the next poll takes a fresh one)."""
    _path(config_dir, profile).unlink(missing_ok=True)


def list_sync_states(config_dir: Path) -> list[dict[str, Any]]:
    """Each profile's sync mode and last poll, without the token."""
    directory = config_dir / _LOOP_DIRNAME / _DIRNAME
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        data = _read(path)
        if data is None:
            continue
        found.append(
            {
                "last_poll": data["last_poll"],
                "mode": data["mode"],
                "profile": data["profile"],
                "provider": data["provider"],
            }
        )
    return sorted(found, key=lambda entry: entry["profile"])


def load_sync_state(config_dir: Path, profile: str, *, provider: str) -> SyncState | None:
    """The saved state for `profile`, or None when there is none for this provider."""
    data = _read(_path(config_dir, profile))
    if data is None or data["provider"] != provider or data["profile"] != profile:
        return None
    return SyncState(
        last_poll=data["last_poll"],
        mode=data["mode"],
        provider=data["provider"],
        token=data["token"],
    )


def save_sync_state(
    config_dir: Path, profile: str, *, provider: str, token: str, mode: str
) -> None:
    """Remember `token` for `profile` (atomic, private)."""
    if mode not in _MODES:
        raise ValueError(f"Expected a sync mode of delta or scan, got {mode!r}")
    if not token:
        raise ValueError("Expected a non-empty sync token, got none")
    try:
        directory = ensure_private_dir(ensure_private_dir(config_dir / _LOOP_DIRNAME) / _DIRNAME)
    except BrowserProfileError as exc:
        raise OSError(str(exc)) from exc
    payload = {
        "last_poll": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "profile": profile,
        "provider": provider,
        "token": token,
    }
    write_private(directory / _file_name(profile), json.dumps(payload, sort_keys=True))


def _file_name(profile: str) -> str:
    return hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16] + ".json"


def _path(config_dir: Path, profile: str) -> Path:
    return config_dir / _LOOP_DIRNAME / _DIRNAME / _file_name(profile)


def _read(path: Path) -> dict[str, str] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(data, dict):
        return None
    wanted = ("last_poll", "mode", "profile", "provider", "token")
    if not all(isinstance(data.get(key), str) and data[key] for key in wanted):
        return None
    if data["mode"] not in _MODES:
        return None
    return {key: data[key] for key in wanted}
