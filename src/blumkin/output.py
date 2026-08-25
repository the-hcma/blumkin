"""Human and JSON stdout helpers."""

from __future__ import annotations

import json
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
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def emit_lines(lines: list[str]) -> None:
    for line in lines:
        print(line)
