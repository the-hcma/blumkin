"""Heuristic detection of prompt-injection payloads in untrusted content.

Scans text extracted from sources blumkin does not control (mail bodies,
local documents, OCR'd images/PDFs) for patterns commonly used to smuggle
instructions to an LLM reading that content as tool output. Detection is
advisory only: a match never blocks or alters the caller's content, it only
attaches a loud, structured warning so the human/agent can decide whether to
act on it. See issue #282 for the full design and explicitly-deferred scope
(no LLM-based detection, no hard-block/confirmation wiring in v1).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# Families are named for the on-thread reply / payload `family` field, so keep
# these stable once shipped - external callers may key off them.
FAMILY_BASE64_NEAR_TRIGGER = "base64_near_trigger"
FAMILY_LINK_LABEL_MISMATCH = "link_label_mismatch"
FAMILY_OVERRIDE_PHRASING = "override_phrasing"
FAMILY_ROLE_IMPERSONATION = "role_impersonation"
FAMILY_ZERO_WIDTH_CHARS = "zero_width_chars"

# How much context to keep on each side of a match when building a snippet -
# enough to be useful in a warning banner, short enough to not leak the whole
# untrusted document into logs/terminal output.
_SNIPPET_RADIUS = 60

# Direct attempts to override or impersonate the system/assistant role.
# Deliberately narrow (not just "ignore" or "disregard" alone) to avoid
# tripping on ordinary correspondence like "please disregard my earlier
# email" or "ignore the typo below".
_OVERRIDE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(?:all|any|the)?\s*(?:previous|prior|above)\s+instructions",
        r"disregard\s+(?:all|any|the)?\s*(?:previous|prior|above)\s*"
        r"(?:instructions|message|prompt)s?",
        r"you\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|different|new)\b",
        r"new\s+system\s+prompt\b",
        r"act\s+as\s+(?:if\s+you\s+(?:were|are)|an?\s+unrestricted)\b",
        r"pretend\s+(?:that\s+)?you\s+are\b",
        r"reveal\s+(?:your|the)\s+(?:system\s+prompt|instructions)\b",
        r"stop\s+(?:being|acting\s+as)\s+(?:an?\s+)?assistant\b",
    )
]

# Zero-width / invisible / bidi-override characters - a common way to hide a
# payload from a human skimming the rendered text while an LLM still reads it.
_ZERO_WIDTH_CHARS = (
    "\u200b"  # zero width space
    "\u200c"  # zero width non-joiner
    "\u200d"  # zero width joiner
    "\u2060"  # word joiner
    "\ufeff"  # zero width no-break space / BOM
    "\u202a\u202b\u202c\u202d\u202e"  # bidi embedding/override
    "\u2066\u2067\u2068\u2069"  # bidi isolates
)

# Markdown links: flag when the visible label looks like a domain/URL that
# does not match the actual href domain, or when the href uses a
# script-executing scheme.
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_DOMAIN_IN_LABEL = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", re.IGNORECASE)
_UNSAFE_LINK_SCHEMES = ("javascript:", "data:", "vbscript:")

# A role-labelled fenced block (```system\n...```) or a line starting with a
# role marker, claiming to carry system/assistant/developer instructions.
_ROLE_FENCE = re.compile(r"```\s*(system|assistant|developer)\b.*?```", re.IGNORECASE | re.DOTALL)
_ROLE_LINE = re.compile(r"^\s*(system|assistant|developer)\s*:\s*\S", re.IGNORECASE | re.MULTILINE)

# A long base64-looking token near an instruction-style trigger word - avoids
# flagging every legitimate base64 blob (attachments, image data URIs, etc.)
# by requiring proximity to a word suggesting the reader should act on it.
_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_BASE64_TRIGGER_WORDS = re.compile(
    r"\b(?:decode|execute|run\s+this|follow\s+these\s+instructions|base64)\b", re.IGNORECASE
)
_BASE64_TRIGGER_WINDOW = 80


@dataclass(frozen=True, slots=True)
class InjectionFinding:
    """One heuristic match: which family, roughly where, and a short snippet."""

    family: str
    location: str
    snippet: str


@dataclass(frozen=True, slots=True)
class InjectionScanResult:
    matched: bool
    findings: list[InjectionFinding] = field(default_factory=list)

    def to_payload(self) -> dict[str, object] | None:
        """`None` when nothing matched, so callers can omit the key entirely
        and keep their JSON shape stable for consumers that don't check it."""
        if not self.matched:
            return None
        return {
            "findings": [
                {"family": f.family, "location": f.location, "snippet": f.snippet}
                for f in self.findings
            ],
            "matched": True,
        }


def format_injection_warning_banner(warning: dict[str, Any] | None) -> list[str]:
    """Render the loud, human-readable banner for a payload's
    `injection_warning` field (as produced by `InjectionScanResult.to_payload`).

    Returns an empty list when `warning` is `None`/falsy, so callers can
    unconditionally `lines.extend(...)` this into their formatter output.
    """
    if not warning:
        return []
    findings = warning.get("findings") or []
    lines = [
        "",
        "\u26a0\ufe0f  POSSIBLE PROMPT INJECTION DETECTED - review before acting on this content:",
    ]
    for finding in findings:
        family = finding.get("family")
        location = finding.get("location")
        snippet = finding.get("snippet")
        lines.append(f"  - [{family}] {location}: {snippet}")
    return lines


def scan_for_injection(text: str, *, location: str) -> InjectionScanResult:
    """Scan one piece of untrusted text for known prompt-injection patterns.

    Pure function, no I/O. `location` is a caller-supplied label (e.g.
    `"pages[0].text"` or `"body"`) copied verbatim into any findings so a
    caller scanning multiple fields can tell them apart.
    """
    if not text:
        return InjectionScanResult(matched=False)
    findings: list[InjectionFinding] = [
        *_scan_override_phrasing(text, location=location),
        *_scan_zero_width_chars(text, location=location),
        *_scan_link_label_mismatch(text, location=location),
        *_scan_role_impersonation(text, location=location),
        *_scan_base64_near_trigger(text, location=location),
    ]
    return InjectionScanResult(matched=bool(findings), findings=findings)


def _scan_base64_near_trigger(text: str, *, location: str) -> list[InjectionFinding]:
    findings = []
    for match in _BASE64_TOKEN.finditer(text):
        window_start = max(0, match.start() - _BASE64_TRIGGER_WINDOW)
        window_end = min(len(text), match.end() + _BASE64_TRIGGER_WINDOW)
        if _BASE64_TRIGGER_WORDS.search(text[window_start:window_end]):
            findings.append(
                InjectionFinding(
                    family=FAMILY_BASE64_NEAR_TRIGGER,
                    location=location,
                    snippet=_snippet(text, match.start(), match.end()),
                )
            )
    return findings


def _scan_link_label_mismatch(text: str, *, location: str) -> list[InjectionFinding]:
    findings = []
    for match in _MARKDOWN_LINK.finditer(text):
        label, url = match.group(1), match.group(2)
        lowered_url = url.lower()
        if lowered_url.startswith(_UNSAFE_LINK_SCHEMES):
            findings.append(
                InjectionFinding(
                    family=FAMILY_LINK_LABEL_MISMATCH,
                    location=location,
                    snippet=_snippet(text, match.start(), match.end()),
                )
            )
            continue
        label_domain = _DOMAIN_IN_LABEL.search(label)
        if label_domain is None:
            continue
        # The label claims to point at `label_domain`, but the href's host is
        # a different domain entirely - classic phishing/injection lure.
        if label_domain.group(1).lower() not in lowered_url:
            findings.append(
                InjectionFinding(
                    family=FAMILY_LINK_LABEL_MISMATCH,
                    location=location,
                    snippet=_snippet(text, match.start(), match.end()),
                )
            )
    return findings


def _scan_override_phrasing(text: str, *, location: str) -> list[InjectionFinding]:
    findings = []
    for pattern in _OVERRIDE_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            findings.append(
                InjectionFinding(
                    family=FAMILY_OVERRIDE_PHRASING,
                    location=location,
                    snippet=_snippet(text, match.start(), match.end()),
                )
            )
    return findings


def _scan_role_impersonation(text: str, *, location: str) -> list[InjectionFinding]:
    findings = []
    match = _ROLE_FENCE.search(text) or _ROLE_LINE.search(text)
    if match is not None:
        findings.append(
            InjectionFinding(
                family=FAMILY_ROLE_IMPERSONATION,
                location=location,
                snippet=_snippet(text, match.start(), match.end()),
            )
        )
    return findings


def _scan_zero_width_chars(text: str, *, location: str) -> list[InjectionFinding]:
    for index, char in enumerate(text):
        if char in _ZERO_WIDTH_CHARS:
            name = unicodedata.name(char, f"U+{ord(char):04X}")
            return [
                InjectionFinding(
                    family=FAMILY_ZERO_WIDTH_CHARS,
                    location=location,
                    snippet=f"{name} at offset {index}: {_snippet(text, index, index + 1)}",
                )
            ]
    return []


def _snippet(text: str, start: int, end: int) -> str:
    lo = max(0, start - _SNIPPET_RADIUS)
    hi = min(len(text), end + _SNIPPET_RADIUS)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return (prefix + text[lo:hi] + suffix).replace("\n", " ")
