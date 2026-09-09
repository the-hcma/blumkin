"""Per-profile record of whether Outlook itself auto-inserts a signature.

Microsoft Graph exposes no API for the client-side "add a signature to new
messages / replies" setting (Outlook Settings -> Mail -> Compose and reply), so
blumkin cannot ask for it. Instead ``blumkin auth login`` (and ``blumkin
doctor``) run a one-off probe - create a throwaway draft, read it back, delete
it - and cache the answer here. ``append_mail_signature`` then skips blumkin's
own ``[mail.signature]`` when Outlook is already adding one, so a draft blumkin
leaves in the mailbox does not end up double-signed once Outlook's compose
pipeline touches it.

The file lives next to the token cache under ``~/.config/blumkin/`` (never
committed). A missing or unreadable file means "unknown" - blumkin then behaves
as it always has and appends its signature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from blumkin.config import BlumkinConfig


@dataclass(frozen=True, slots=True)
class SignatureState:
    """The cached probe result. ``detected is None`` means "never probed / unknown"."""

    checked_at: str | None = None
    detected: bool | None = None

    @property
    def suppresses_signature(self) -> bool:
        """True only when the probe positively saw Outlook add its own signature."""
        return self.detected is True


def clear_signature_state(config: BlumkinConfig) -> None:
    """Remove the cached probe result (used by ``auth logout``)."""
    path = config.mail_signature_state_path
    if path.is_file():
        path.unlink()


def load_signature_state(config: BlumkinConfig) -> SignatureState:
    """Read the cached probe result; any problem reading it is treated as "unknown".

    A result recorded under a different ``provider`` than the active one is
    ignored - the file is keyed only by profile dir, so a profile switched from
    Microsoft to Google (or back) must not carry the old provider's answer.
    """
    path = config.mail_signature_state_path
    try:
        raw = json.loads(path.read_text())
    except OSError, ValueError:
        return SignatureState()
    if not isinstance(raw, dict):
        return SignatureState()
    provider = raw.get("provider")
    if isinstance(provider, str) and provider and provider != config.provider.value:
        return SignatureState()
    detected = raw.get("outlook_signature_detected")
    checked_at = raw.get("checked_at")
    return SignatureState(
        checked_at=checked_at if isinstance(checked_at, str) else None,
        detected=detected if isinstance(detected, bool) else None,
    )


def record_signature_state(config: BlumkinConfig, *, detected: bool | None) -> SignatureState:
    """Persist a probe result. ``detected is None`` (probe could not run) is a no-op."""
    if detected is None:
        return load_signature_state(config)
    state = SignatureState(
        checked_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        detected=detected,
    )
    payload: dict[str, Any] = {
        "checked_at": state.checked_at,
        "outlook_signature_detected": state.detected,
        "provider": config.provider.value,
    }
    path = config.mail_signature_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return state
