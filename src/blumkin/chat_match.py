"""Why a chat name match is partial, and what the caller may do about it (#787).

`chat find --with` fetches each chat's members and skips a chat whose fetch fails. A skip that
is a 403 never goes away on a retry (the account has no access to that chat), while a throttle
does; the caller needs to tell them apart, and the read-only `chat last` may accept a unique
match anyway when asked to.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: HTTP statuses that mean "slow down" rather than "no access".
_THROTTLE_STATUSES = frozenset({429, 503, 504})


def partial_message(name: str, found: dict[str, Any], *, allow_hint: bool = False) -> str:
    """The refusal for a partial match; `allow_hint` names `--allow-partial` (reads only)."""
    reasons = _reasons_text(found.get("skip_reasons"))
    detail = f": {reasons}" if reasons else ""
    out = (
        f"chat match for {name!r} is partial "
        f"(skipped {int(found['skipped'])} chat(s){detail}); "
        "retry later or pass --chat-id from `chat find`"
    )
    if allow_hint:
        out += ", or pass --allow-partial to accept a unique match despite the skipped chats"
    return out


def skip_reasons(statuses: Iterable[int | None]) -> dict[str, int]:
    """Count skipped chats by cause: `forbidden` (403), `throttled` (429, 503, 504), `other`."""
    counts = {"forbidden": 0, "other": 0, "throttled": 0}
    for status in statuses:
        if status == 403:
            counts["forbidden"] += 1
        elif status in _THROTTLE_STATUSES:
            counts["throttled"] += 1
        else:
            counts["other"] += 1
    return counts


def skipped_summary(payload: dict[str, Any]) -> str:
    """` - 6 forbidden (403, retrying will not help), ...` for a human line; empty if unknown."""
    reasons = _reasons_text(payload.get("skip_reasons"))
    return f" - {reasons}" if reasons else ""


def _reasons_text(reasons: Any) -> str:
    if not isinstance(reasons, dict):
        return ""
    labels = (("forbidden", "forbidden (403, retrying will not help)"), ("throttled", "throttled"))
    parts = [f"{int(reasons[key])} {label}" for key, label in labels if reasons.get(key)]
    if reasons.get("other"):
        parts.append(f"{int(reasons['other'])} other error(s)")
    return ", ".join(parts)
