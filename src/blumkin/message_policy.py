"""Config-driven outbound message lint and policy checks."""

from __future__ import annotations

import html
import re
from typing import Any

from blumkin.config import BlumkinConfig


def should_suppress_signature(*, config: BlumkinConfig, detected_outlook_signature: bool) -> bool:
    policy = _policy(config)
    if policy is not None and not bool(getattr(policy, "honor_client_signature_suppression", True)):
        return False
    signature = getattr(config, "mail_signature", None)
    return bool(getattr(signature, "client_appends_signature", False) or detected_outlook_signature)


def validate_outbound_text(content: str, *, config: BlumkinConfig, field_name: str) -> None:
    if not _forbid_unicode_dashes(config):
        return
    if _DISALLOWED_DASH_RE.search(html.unescape(content)):
        raise ValueError(
            f"{field_name} contains a disallowed dash (— / – / &mdash; / &ndash;); "
            "use ASCII hyphen '-' instead"
        )


_DISALLOWED_DASH_RE = re.compile(r"[—–]")


def _forbid_unicode_dashes(config: BlumkinConfig) -> bool:
    policy = _policy(config)
    return True if policy is None else bool(getattr(policy, "forbid_unicode_dashes", True))


def _policy(config: BlumkinConfig) -> Any:
    return getattr(config, "message_policy", None)
