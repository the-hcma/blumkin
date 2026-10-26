"""Inbound mail trust gates for loop triggers (issue #503).

A message is acted on only when the sender is allowlisted and the *first*
Authentication-Results header (the receiving MTA typically prepends) reports
spf=pass, dkim=pass and dmarc=pass in that same header. Missing or partial
headers fail closed. Sender-injected later headers are ignored. Message content
is never used as instructions.
"""

from __future__ import annotations

import re
from typing import Any

_AUTH_HEADER = re.compile(r"^authentication-results\s*:", re.IGNORECASE)
_SPF = re.compile(r"\bspf=(pass)\b", re.IGNORECASE)
_DKIM = re.compile(r"\bdkim=(pass)\b", re.IGNORECASE)
_DMARC = re.compile(r"\bdmarc=(pass)\b", re.IGNORECASE)


def allowlisted_sender(sender: str | None, allowlist: tuple[str, ...] | list[str]) -> bool:
    """True when ``sender`` matches an allowlist entry (case-insensitive email)."""
    if not sender or not isinstance(sender, str):
        return False
    needle = sender.strip().lower()
    if not needle or "@" not in needle:
        return False
    allowed = {
        entry.strip().lower() for entry in allowlist if isinstance(entry, str) and entry.strip()
    }
    return needle in allowed


def authentication_results_pass(headers: list[dict[str, Any]] | list[str] | None) -> bool:
    """True when the first Authentication-Results header alone has spf/dkim/dmarc=pass."""
    text = _first_authentication_results(headers)
    if not text:
        return False
    return bool(_SPF.search(text) and _DKIM.search(text) and _DMARC.search(text))


def subject_matches(subject: str | None, tag: str | None) -> bool:
    """True when no tag is required, or ``tag`` appears in the subject."""
    if tag is None or not tag.strip():
        return True
    if not subject or not isinstance(subject, str):
        return False
    return tag.strip().lower() in subject.lower()


def _first_authentication_results(headers: list[dict[str, Any]] | list[str] | None) -> str:
    """Return the first Authentication-Results value (provider-prepended)."""
    if not headers:
        return ""
    for entry in headers:
        if isinstance(entry, str):
            if _AUTH_HEADER.match(entry):
                return entry
            continue
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("key") or "")
        value = str(entry.get("value") or entry.get("val") or "")
        if name.lower() == "authentication-results" and value.strip():
            return f"{name}: {value}"
    return ""
