"""Attachment intake into a trigger-scoped managed folder (issue #503).

Downloads use the existing provider attachment APIs and
``sanitize_attachment_filename``. Filenames are never used as paths or commands.
Message directory names are a SHA-256 of the Graph message id so Base64 characters
that would collapse under sanitization cannot collide two messages into one folder.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from blumkin.attachments import sanitize_attachment_filename
from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir

_INTAKE_DIRNAME = "intake"
_STORE_DIRNAME = "loop"

DownloadFn = Callable[..., Awaitable[dict[str, Any]]]


class LoopIntakeError(ValueError):
    """Attachment intake could not complete."""


def intake_dir(config_dir: Path, *, trigger: str, message_id: str) -> Path:
    """Private directory for one message's attachments under the loop store.

    The message segment is `sha256(message_id)` (hex). A legacy directory named with
    `sanitize_attachment_filename(message_id)` is reused when the hashed path does not
    exist yet, so in-flight intakes from before the hash key keep working.
    """
    safe_trigger = sanitize_attachment_filename(trigger)
    hashed = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    try:
        root = ensure_private_dir(config_dir / _STORE_DIRNAME / _INTAKE_DIRNAME)
        trigger_root = ensure_private_dir(root / safe_trigger)
    except BrowserProfileError as exc:
        raise LoopIntakeError(str(exc)) from exc
    hashed_path = trigger_root / hashed
    if hashed_path.is_dir() and not hashed_path.is_symlink():
        return hashed_path
    legacy = trigger_root / sanitize_attachment_filename(message_id)
    if legacy.is_dir() and not legacy.is_symlink() and legacy.name != hashed:
        return legacy
    try:
        return ensure_private_dir(hashed_path)
    except BrowserProfileError as exc:
        raise LoopIntakeError(str(exc)) from exc


async def save_attachments(
    *,
    config_dir: Path,
    trigger: str,
    message_id: str,
    download: DownloadFn,
    attachment_types: tuple[str, ...] = (),
    config: Any = None,
) -> list[str]:
    """Download attachments for ``message_id``; return absolute saved paths.

    When ``attachment_types`` is non-empty, only files whose suffix matches
    (case-insensitive, with or without a leading dot) are kept; others are removed.
    """
    destination = intake_dir(config_dir, trigger=trigger, message_id=message_id)
    payload = await download(
        message_id=message_id, out=str(destination), download_all=True, config=config
    )
    saved = payload.get("saved") or []
    allowed = {(ext if ext.startswith(".") else f".{ext}").lower() for ext in attachment_types}
    paths: list[str] = []
    for entry in saved:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("saved_path")
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw)
        if allowed and path.suffix.lower() not in allowed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if path.is_file():
            paths.append(str(path.resolve()))
    return paths
