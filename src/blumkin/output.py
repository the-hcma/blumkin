"""Human and JSON stdout helpers."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, TextIO


def emit_error(
    *,
    error: str,
    message: str,
    as_json: bool,
    hint: str | None = None,
) -> None:
    if as_json:
        payload: dict[str, Any] = {"error": error, "message": message, "ok": False}
        if hint:
            payload["hint"] = hint
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)


def emit_json(payload: Any) -> None:
    """Print a JSON payload on stdout.

    Every object payload carries a top-level ``ok`` boolean so an agent can branch
    on success without inspecting the exit code: ``ok`` defaults to ``True`` here
    and is only ``False`` when the command sets it (a fail-closed result printed
    before a non-zero exit, e.g. ``people resolve`` ambiguous). Error payloads go
    through ``emit_error`` and already carry ``ok: false``.
    """
    if isinstance(payload, dict) and "ok" not in payload:
        payload = {"ok": True, **payload}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def emit_lines(lines: list[str]) -> None:
    for line in lines:
        print(line)


def emit_warning(message: str) -> None:
    """Print a non-fatal warning to stderr so stdout stays clean for --json output."""
    print(f"warning: {message}", file=sys.stderr)


def hyperlink(label: str, url: str, *, stream: TextIO | None = None) -> str:
    """Return ``url`` wrapped in an OSC 8 terminal hyperlink so a capable terminal
    renders ``label`` as a click target instead of dumping a wrapped raw URL.

    Falls back to plain text - the bare URL when ``label == url``, else
    ``"label (url)"`` so the address is still copyable - when the destination is
    not an interactive terminal (piped, redirected, CI), when ``BLUMKIN_HYPERLINKS``
    is ``never``, under ``NO_COLOR`` / ``TERM=dumb``, or when ``url`` carries a
    control character that would break out of the escape sequence.

    Never call this on a string headed for ``--json`` output - JSON payloads keep
    the raw URL string.
    """
    plain = url if label == url else f"{label} ({url})"
    if _CONTROL_RE.search(url) or not _hyperlinks_enabled(stream):
        return plain
    esc = "\x1b"
    return f"{esc}]8;;{url}{esc}\\{label}{esc}]8;;{esc}\\"


def sanitize_terminal(text: str) -> str:
    """Strip C0/C1 control chars that could hijack a terminal in human output."""
    return _CONTROL_RE.sub("", text)


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


def _hyperlinks_enabled(stream: TextIO | None) -> bool:
    mode = os.environ.get("BLUMKIN_HYPERLINKS", "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except AttributeError, ValueError:
        return False
