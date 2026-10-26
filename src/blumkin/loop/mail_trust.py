"""Inbound mail trust gates for loop triggers (issue #503).

A message is acted on only when the sender is allowlisted and the *first*
Authentication-Results header (the receiving MTA typically prepends) reports
spf=pass, dkim=pass and dmarc=pass as method results (RFC 8601). Comments are
stripped before matching so comment text cannot spoof a pass. Missing or
partial headers fail closed. Message content is never used as instructions.
"""

from __future__ import annotations

import re
from typing import Any

_AUTH_HEADER = re.compile(r"^authentication-results\s*:", re.IGNORECASE)
_COMMENT = re.compile(r"\([^)]*\)")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SPF = re.compile(r"(?<![a-z0-9_])spf=pass(?![a-z0-9_=])", re.IGNORECASE)
_DKIM = re.compile(r"(?<![a-z0-9_])dkim=pass(?![a-z0-9_=])", re.IGNORECASE)
_DMARC = re.compile(r"(?<![a-z0-9_])dmarc=pass(?![a-z0-9_=])", re.IGNORECASE)


def allowlisted_sender(sender: str | None, allowlist: tuple[str, ...] | list[str]) -> bool:
    """True when ``sender`` matches an allowlist entry (case-insensitive email)."""
    if not sender or not isinstance(sender, str):
        return False
    needle = sender.strip().lower()
    if not _EMAIL.match(needle):
        return False
    allowed = {
        entry.strip().lower()
        for entry in allowlist
        if isinstance(entry, str) and _EMAIL.match(entry.strip())
    }
    return needle in allowed


def authentication_results_pass(headers: list[dict[str, Any]] | list[str] | None) -> bool:
    """True when the first Authentication-Results header has spf/dkim/dmarc=pass."""
    text = _strip_comments(_first_authentication_results(headers))
    if not text:
        return False
    return bool(_SPF.search(text) and _DKIM.search(text) and _DMARC.search(text))


def is_allowlist_email(value: str) -> bool:
    """True when ``value`` looks like a complete sender address."""
    return bool(_EMAIL.match(value.strip()))


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


def _strip_comments(text: str) -> str:
    """Remove RFC 8601 parenthetical comments so they cannot spoof method results."""
    previous = None
    while previous != text:
        previous = text
        text = _COMMENT.sub(" ", text)
    return text
