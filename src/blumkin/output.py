"""Human and JSON stdout helpers."""

from __future__ import annotations

import json
import re
import sys
from typing import Any


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


def sanitize_terminal(text: str) -> str:
    """Strip C0/C1 control chars that could hijack a terminal in human output."""
    return _CONTROL_RE.sub("", text)


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
