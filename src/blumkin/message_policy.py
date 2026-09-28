"""Config-driven outbound message lint and policy checks."""

from __future__ import annotations

import html
import re

from blumkin.config import BlumkinConfig


def should_suppress_signature(*, config: BlumkinConfig, detected_outlook_signature: bool) -> bool:
    if not config.message_policy.honor_client_signature_suppression:
        return False
    return config.mail_signature.client_appends_signature or detected_outlook_signature


def validate_outbound_text(content: str, *, config: BlumkinConfig, field_name: str) -> None:
    if not config.message_policy.forbid_unicode_dashes:
        return
    if _DISALLOWED_DASH_RE.search(html.unescape(content)):
        raise ValueError(
            f"{field_name} contains a disallowed dash (— / – / &mdash; / &ndash;); "
            "use ASCII hyphen '-' instead (see docs/outbound-message-policy.md)"
        )


_DISALLOWED_DASH_RE = re.compile(r"[—–]")
