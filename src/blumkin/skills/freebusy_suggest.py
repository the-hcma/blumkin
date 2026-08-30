"""Shared freebusy → mutual-suggest helpers (provider-agnostic payloads)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def collect_busy_intervals(
    items: list[dict[str, Any]],
    *,
    treat_tentative_busy: bool,
) -> list[tuple[datetime, datetime]]:
    """Union busy intervals from skill-shaped freebusy ``items``."""
    intervals: list[tuple[datetime, datetime]] = []
    for item in items:
        for slot in item.get("busy") or []:
            if not _status_is_busy(slot.get("status"), treat_tentative_busy=treat_tentative_busy):
                continue
            start_raw = slot.get("start")
            end_raw = slot.get("end")
            if not start_raw or not end_raw:
                continue
            start = datetime.fromisoformat(str(start_raw))
            end = datetime.fromisoformat(str(end_raw))
            if end > start:
                intervals.append((start, end))
    return intervals


def raise_if_schedule_errors(items: list[dict[str, Any]], *, requested: list[str]) -> None:
    """Fail closed when freebusy could not resolve a requested mailbox."""
    by_schedule = {
        str(item.get("schedule") or "").casefold(): item for item in items if item.get("schedule")
    }
    problems: list[str] = []
    for email in requested:
        key = email.casefold()
        item = by_schedule.get(key)
        if item is None:
            problems.append(f"{email}: no schedule returned")
            continue
        err = item.get("error")
        if err:
            problems.append(f"{email}: {err}")
    if problems:
        raise ValueError("freebusy lookup failed for: " + "; ".join(problems))


def _status_is_busy(status: Any, *, treat_tentative_busy: bool) -> bool:
    """Treat only explicit free (and optional tentative) as free; fail closed otherwise."""
    label = str(status or "").split(".")[-1].casefold()
    if label == "free":
        return False
    if label == "tentative":
        return treat_tentative_busy
    # busy / oof / workingElsewhere / unknown / missing / anything else → busy
    return True
