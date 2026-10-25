"""Cron due-time evaluation and last-due catch-up (issue #502).

A schedule means "make sure this has happened by then", not "run at this instant".
The runner stores the last due time per trigger; a miss while the machine was off
runs once on the next start (catch-up), and never more than once per due time.
Paused triggers do not catch up on resume unless asked (resume advances last-due).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir
from blumkin.loop.types import TriggerKind, TriggerSpec

_LAST_DUE_FILENAME = "cron-last-due.json"
_STORE_DIRNAME = "loop"


class CronError(ValueError):
    """A cron expression or timezone is not acceptable."""


@dataclass(frozen=True, slots=True)
class DueFire:
    """One due instant that has not yet been consumed for a trigger."""

    due_at: datetime
    trigger: TriggerSpec


@dataclass(frozen=True, slots=True)
class _Schedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    dom: frozenset[int]
    months: frozenset[int]
    dow: frozenset[int]


def due_fires(
    triggers: dict[str, TriggerSpec],
    *,
    config_dir: Path,
    now: datetime | None = None,
    paused: frozenset[str] = frozenset(),
) -> list[DueFire]:
    """Due times that still need a run (at most one catch-up per trigger)."""
    instant = now or datetime.now(UTC)
    last = load_last_due(config_dir)
    fires: list[DueFire] = []
    for name in sorted(triggers):
        spec = triggers[name]
        if spec.kind is not TriggerKind.CRON:
            continue
        if name in paused:
            continue
        due = next_due_at_or_before(spec, instant, after=last.get(name))
        if due is not None:
            fires.append(DueFire(due_at=due, trigger=spec))
    return fires


def load_last_due(config_dir: Path) -> dict[str, datetime]:
    path = config_dir / _STORE_DIRNAME / _LAST_DUE_FILENAME
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CronError(f"Expected readable cron last-due state at {path}, got {exc}") from exc
    if not isinstance(raw, dict):
        raise CronError(f"Expected an object in {path}")
    out: dict[str, datetime] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise CronError(f"Expected string trigger→iso pairs in {path}")
        try:
            out[name] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CronError(f"Expected ISO due times in {path}, got {value!r}") from exc
    return out


def mark_due_consumed(config_dir: Path, trigger: str, due_at: datetime) -> None:
    """Record that `due_at` was handled for `trigger` (catch-up will not repeat it)."""
    current = load_last_due(config_dir)
    current[trigger] = due_at.astimezone(UTC)
    _write_last_due(config_dir, current)


def next_due_at_or_before(
    spec: TriggerSpec,
    now: datetime,
    *,
    after: datetime | None,
) -> datetime | None:
    """The latest schedule tick at or before `now` that is still after `after`."""
    if not spec.schedule or not spec.timezone:
        raise CronError(f"cron.{spec.name} is missing schedule or timezone")
    try:
        zone = ZoneInfo(spec.timezone)
    except ZoneInfoNotFoundError as exc:
        raise CronError(
            f"cron.{spec.name}.timezone is not a valid IANA zone: {spec.timezone}"
        ) from exc
    local_now = now.astimezone(zone)
    # Walk back at most 8 days of minutes to find the latest matching tick.
    cursor = local_now.replace(second=0, microsecond=0)
    earliest = cursor - timedelta(days=8)
    schedule = _parse_schedule(spec.schedule, label=f"cron.{spec.name}.schedule")
    while cursor >= earliest:
        if _matches(schedule, cursor) and _working_day_ok(spec, cursor):
            utc_tick = cursor.astimezone(UTC)
            if after is None or utc_tick > after.astimezone(UTC):
                return utc_tick
        cursor -= timedelta(minutes=1)
    return None


def validate_cron_trigger(spec: TriggerSpec) -> None:
    """Fail fast at startup when a cron trigger's schedule or zone is invalid."""
    if spec.kind is not TriggerKind.CRON:
        return
    if not spec.schedule or not spec.timezone:
        raise CronError(f"cron.{spec.name} requires schedule and timezone")
    _parse_schedule(spec.schedule, label=f"cron.{spec.name}.schedule")
    try:
        ZoneInfo(spec.timezone)
    except ZoneInfoNotFoundError as exc:
        raise CronError(
            f"cron.{spec.name}.timezone is not a valid IANA zone: {spec.timezone}"
        ) from exc


def _matches(schedule: _Schedule, when: datetime) -> bool:
    # cron dow: 0=Sunday … 6=Saturday; datetime.weekday(): Monday=0 … Sunday=6.
    cron_dow = (when.weekday() + 1) % 7
    return (
        when.minute in schedule.minutes
        and when.hour in schedule.hours
        and when.day in schedule.dom
        and when.month in schedule.months
        and cron_dow in schedule.dow
    )


def _parse_field(raw: str, minimum: int, maximum: int, *, label: str) -> frozenset[int]:
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"{label}: invalid step in {raw!r}")
            step = int(step_s)
            part = base
        if part == "*":
            span = range(minimum, maximum + 1)
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not a_s.isdigit() or not b_s.isdigit():
                raise CronError(f"{label}: invalid range in {raw!r}")
            a, b = int(a_s), int(b_s)
            if not minimum <= a <= b <= maximum:
                raise CronError(f"{label}: range out of bounds in {raw!r}")
            span = range(a, b + 1)
        elif part.isdigit():
            n = int(part)
            if not minimum <= n <= maximum:
                raise CronError(f"{label}: value out of bounds in {raw!r}")
            span = range(n, n + 1)
        else:
            raise CronError(f"{label}: invalid field {raw!r}")
        values.update(list(span)[::step] if step > 1 else span)
    if not values:
        raise CronError(f"{label}: empty field {raw!r}")
    return frozenset(values)


def _parse_schedule(expression: str, *, label: str) -> _Schedule:
    parts = expression.split()
    if len(parts) != 5:
        raise CronError(f"{label} must be a 5-field cron expression, got {expression!r}")
    return _Schedule(
        minutes=_parse_field(parts[0], 0, 59, label=label),
        hours=_parse_field(parts[1], 0, 23, label=label),
        dom=_parse_field(parts[2], 1, 31, label=label),
        months=_parse_field(parts[3], 1, 12, label=label),
        dow=_parse_field(parts[4], 0, 6, label=label),
    )


def _working_day_ok(spec: TriggerSpec, local: datetime) -> bool:
    if not spec.working_days_only:
        return True
    return local.weekday() < 5


def _write_last_due(config_dir: Path, values: dict[str, datetime]) -> None:
    try:
        directory = ensure_private_dir(config_dir / _STORE_DIRNAME)
    except BrowserProfileError as exc:
        raise CronError(str(exc)) from exc
    path = directory / _LAST_DUE_FILENAME
    payload = {
        name: due.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for name, due in sorted(values.items())
    }
    text = json.dumps(payload, sort_keys=True) + "\n"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        data = text.encode("utf-8")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
