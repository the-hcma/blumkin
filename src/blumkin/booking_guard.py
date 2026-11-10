"""Refuse to book a meeting on a holiday, a weekend, off-hours or a busy slot (issue #716).

`calendar create` and a `calendar update` that moves an event or invites people run
:func:`check_booking` before the provider is called, for the CLI and the MCP server alike.
It looks at the slot in the organizer's time zone (weekend, the profile's holidays, the
weekly windows it never wants booked) and at each person's schedule from `calendar freebusy`
(working hours in their own zone, and whether they are free). A finding blocks the call until
the caller names that check with `--allow` (`--allow weekend`), so an override is deliberate
and says what was overridden. Anything that could not be read (no working hours, a schedule
freebusy could not see) is a warning, never a silent pass, and is reported with the result.

All-day events are not meetings and are not checked; a recurring series is checked at its
first occurrence only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from blumkin.config import BlumkinConfig, PreferencesConfig
from blumkin.skills.calendar import parse_local_datetime
from blumkin.skills.calendar_writes import parse_duration, reject_date_only_start
from blumkin.skills.errors import BookingGuardError

#: The checks, which `--allow` names one at a time.
CHECKS = ("busy", "exclusion", "holiday", "hours", "weekend")

#: The skills this guard runs before.
GUARDED_SKILLS = frozenset({"calendar.create", "calendar.update"})

_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DEFAULT_DURATION = "30m"
#: A busy slot of these kinds does not stop a meeting.
_NOT_BUSY = frozenset({"free", "workingelsewhere"})
#: An existing event's own slot is not a conflict when it is moved or given attendees: busy
#: times this close to its old start and end are taken as the event itself.
_SAME_SLOT = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason a slot should not be booked, named by the check that found it."""

    check: str
    message: str
    who: str = "you"

    def text(self) -> str:
        who = "" if self.who == "you" else f"{self.who}: "
        return f"{self.check}: {who}{self.message}"


def allowed_checks(raw: Any) -> frozenset[str]:
    """The checks a caller chose to override, each one of `CHECKS` (an unknown name is refused)."""
    names: list[str] = []
    for item in raw if isinstance(raw, list | tuple) else ([] if raw is None else [raw]):
        names.extend(part.strip().lower() for part in str(item).split(",") if part.strip())
    unknown = sorted(name for name in names if name not in CHECKS)
    if unknown:
        raise ValueError(
            f"--allow takes one of {', '.join(CHECKS)}, got {', '.join(repr(n) for n in unknown)}"
        )
    return frozenset(names)


def schedule_findings(
    start: datetime,
    end: datetime,
    item: dict[str, Any] | None,
    *,
    who: str,
    own_slot: tuple[datetime, datetime] | None = None,
) -> tuple[list[Finding], list[str], set[str]]:
    """What one person's schedule says about the slot: findings, warnings, checks made.

    `item` is one entry of `calendar freebusy`. A missing item, an error, no working hours
    or an unknown time zone are warnings that the check was not made, never a pass. `own_slot`
    is the event's current start and end when it is being moved: the organizer's own busy time
    is ignored for the organizer and for people already on the event (they accepted it, so it
    shows as their busy time); pass None for a newly invited person, whose busy time is real.
    """
    label = "you" if who == "you" else who
    if item is None:
        return (
            [],
            [f"freebusy returned nothing for {label}: hours and availability not checked"],
            set(),
        )
    if item.get("error"):
        return (
            [],
            [f"could not read {label}'s schedule ({item['error']}): not checked"],
            set(),
        )
    findings: list[Finding] = []
    warnings: list[str] = []
    checked: set[str] = set()
    hours_found, hours_warning = _hours_findings(start, end, item.get("working_hours"), label)
    if hours_warning:
        warnings.append(hours_warning)
    else:
        checked.add("hours")
        findings.extend(hours_found)
    busy = item.get("busy")
    # Microsoft answers an unreadable mailbox with an empty availability string; Google has no
    # such string (`None`) and answers a free person with an empty busy list, which is a result.
    if item.get("availability_view") == "" and not busy:
        warnings.append(f"freebusy returned nothing usable for {label}: availability not checked")
    else:
        checked.add("busy")
        findings.extend(_busy_findings(start, end, busy or [], label, own_slot))
    return findings, warnings, checked


def slot_findings(start: datetime, end: datetime, prefs: PreferencesConfig) -> list[Finding]:
    """The checks that need only the slot and the config: weekend, holiday, excluded windows."""
    findings: list[Finding] = []
    holidays = dict(prefs.holidays)
    for day in _days(start, end):
        if day.weekday() >= 5:
            findings.append(Finding("weekend", f"{day:%A %Y-%m-%d} is a weekend"))
        if day in holidays:
            name = f" ({holidays[day]})" if holidays[day] else ""
            findings.append(Finding("holiday", f"{day:%A %Y-%m-%d} is a holiday{name}"))
        for rule in prefs.booking_exclusions:
            if rule.day != day.weekday():
                continue
            window_start = datetime.combine(day, rule.start, tzinfo=start.tzinfo)
            window_end = datetime.combine(day, rule.end, tzinfo=start.tzinfo)
            if start < window_end and end > window_start:
                findings.append(
                    Finding(
                        "exclusion", f"{day:%A} {start:%H:%M}-{end:%H:%M} is in your {rule.text!r}"
                    )
                )
    return findings


async def check_booking(
    provider: Any,
    config: BlumkinConfig,
    skill_id: str,
    kwargs: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    """Check the slot a `calendar create` / `calendar update` is about to book.

    Returns the report to attach to the result (`checked`, `allowed`, `warnings`), or None when
    the call books no meeting time. Raises :class:`BookingGuardError` when a finding is not
    allowed by `--allow`.
    """
    allowed = allowed_checks(arguments.get("allow"))
    slot = await _slot(provider, config, skill_id, kwargs)
    if slot is None:
        return None
    start, end, invitees, own_slot, on_event = slot
    findings = slot_findings(start, end, config.preferences)
    checked: set[str] = {"weekend", "holiday"}
    if config.preferences.booking_exclusions:
        checked.add("exclusion")
    warnings: list[str] = []
    people: list[tuple[str, str]] = [("you", config.email)] if config.email else []
    people += [(email, email) for email in invitees]
    if not config.email:
        warnings.append(
            "your own address is not in the config: your hours and busy time not checked"
        )
    if people:
        items, failure = await _availability(provider, [addr for _who, addr in people], start, end)
        if failure:
            warnings.append(failure)
        else:
            by_address = {str(item.get("schedule") or "").lower(): item for item in items}
            for who, address in people:
                more, notes, done = schedule_findings(
                    start,
                    end,
                    by_address.get(address.lower()),
                    who=who,
                    own_slot=own_slot if who == "you" or address.lower() in on_event else None,
                )
                findings.extend(more)
                warnings.extend(notes)
                checked |= done
    blocking = [finding for finding in findings if finding.check not in allowed]
    if blocking:
        raise BookingGuardError(blocking, warnings=warnings)
    return {
        "allowed": [finding.text() for finding in findings],
        "checked": sorted(checked),
        "warnings": warnings,
    }


def _days(start: datetime, end: datetime) -> list[date]:
    """Every calendar day the slot touches (an end exactly at midnight does not touch its day)."""
    last = (end - timedelta(microseconds=1)).date() if end > start else start.date()
    return [start.date() + timedelta(days=n) for n in range((last - start.date()).days + 1)]


async def _availability(
    provider: Any, addresses: list[str], start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], str | None]:
    """`calendar freebusy` for everyone, or why it could not be read (a warning, not a pass)."""
    try:
        result = await provider.calendar_freebusy(with_emails=addresses, start=start, end=end)
    except Exception as exc:  # noqa: BLE001 - an unreadable schedule is reported, never a pass
        return [], (
            f"could not read availability ({type(exc).__name__}): hours and busy time not checked"
        )
    return list(result.get("items") or []), None


def _busy_findings(
    start: datetime,
    end: datetime,
    busy: list[dict[str, Any]],
    who: str,
    own_slot: tuple[datetime, datetime] | None,
) -> list[Finding]:
    findings: list[Finding] = []
    for slot in busy:
        status = str(slot.get("status") or "busy").lower()
        begins, ends = _parse_instant(slot.get("start")), _parse_instant(slot.get("end"))
        if status in _NOT_BUSY or begins is None or ends is None:
            continue
        if own_slot is not None and _is_event_itself(begins, ends, own_slot, (start, end)):
            continue  # the event being moved is not its own conflict
        if begins < end and ends > start:
            local = start.tzinfo
            shown = (begins.astimezone(local), ends.astimezone(local))
            findings.append(
                Finding("busy", f"busy {shown[0]:%H:%M}-{shown[1]:%H:%M} ({status})", who)
            )
    return findings


def _hours_findings(
    start: datetime, end: datetime, hours: dict[str, Any] | None, who: str
) -> tuple[list[Finding], str | None]:
    """The slot against one person's working hours in their own zone, or why it was not checked."""
    if not hours or not hours.get("start") or not hours.get("end") or not hours.get("days_of_week"):
        return [], f"no working hours known for {who}: hours not checked"
    try:
        zone = ZoneInfo(str(hours.get("timezone") or ""))
    except ZoneInfoNotFoundError, ValueError:
        return [], f"unknown time zone for {who}'s working hours: hours not checked"
    try:
        opens = time.fromisoformat(str(hours["start"]))
        closes = time.fromisoformat(str(hours["end"]))
    except ValueError:
        return [], f"unreadable working hours for {who}: hours not checked"
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    days = {str(day).lower() for day in hours["days_of_week"]}
    findings: list[Finding] = []
    where = f"{local_start:%a %H:%M}-{local_end:%H:%M} {zone.key}"
    if _DAY_NAMES[local_start.weekday()] not in days:
        whose = "you" if who == "you" else "them"
        findings.append(Finding("hours", f"{where} is not a working day for {whose}", who))
    elif (
        local_end.date() != local_start.date()
        or local_start.time() < opens
        or local_end.time() > closes
    ):
        findings.append(
            Finding(
                "hours",
                f"{where} is outside working hours {opens:%H:%M}-{closes:%H:%M}",
                who,
            )
        )
    return findings, None


def _parse_instant(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_event_itself(
    begins: datetime,
    ends: datetime,
    old: tuple[datetime, datetime],
    new: tuple[datetime, datetime],
) -> bool:
    """Whether a busy block is the event's own: its old slot, or that slot clipped to the new one.

    Microsoft reports the whole old slot; Google clips a busy block to the window asked about,
    so a move that partly overlaps the old slot shows only the overlap.
    """
    pairs = (old, (max(old[0], new[0]), min(old[1], new[1])))
    return any(_same(begins, a) and _same(ends, b) for a, b in pairs)


def _same(left: datetime, right: datetime) -> bool:
    return abs(left - right) <= _SAME_SLOT


async def _slot(
    provider: Any, config: BlumkinConfig, skill_id: str, kwargs: dict[str, Any]
) -> tuple[datetime, datetime, list[str], tuple[datetime, datetime] | None, frozenset[str]] | None:
    """The slot a call books and who is in it, or None when it books no meeting time."""
    zone_name = kwargs.get("tz_name") or config.default_tz
    if not zone_name:
        return None  # no zone to read the slot in: the provider refuses the call itself
    zone = ZoneInfo(zone_name)
    if skill_id == "calendar.create":
        if kwargs.get("all_day") or not kwargs.get("start_raw"):
            return None
        reject_date_only_start(kwargs["start_raw"])
        start = parse_local_datetime(kwargs["start_raw"], zone)
        length = parse_duration(kwargs.get("duration") or _DEFAULT_DURATION)
        return start, (start.astimezone(UTC) + length).astimezone(zone), [], None, frozenset()
    changes_time = any(kwargs.get(key) for key in ("start_raw", "end_raw", "duration"))
    invites = kwargs.get("with_emails")
    if kwargs.get("all_day") or not (changes_time or invites):
        return None
    event = (
        await provider.calendar_get(
            event_id=kwargs["event_id"], calendar=kwargs.get("calendar"), tz_name=zone.key
        )
    ).get("event") or {}
    # `--no-all-day --start ...` turns an all-day event into a meeting: the new slot is checked,
    # and the old all-day span is neither its own busy time nor the length of the meeting.
    converting = bool(event.get("is_all_day")) and kwargs.get("all_day") is False
    if event.get("is_all_day") and not (converting and kwargs.get("start_raw")):
        return None
    old_start, old_end = _parse_instant(event.get("start")), _parse_instant(event.get("end"))
    if old_start is None or old_end is None:
        raise ValueError("could not read the event's current time to check the booking")
    if kwargs.get("start_raw"):
        reject_date_only_start(kwargs["start_raw"])  # as create does: a date is not a time
    start = (
        parse_local_datetime(kwargs["start_raw"], zone) if kwargs.get("start_raw") else old_start
    )
    if kwargs.get("end_raw"):
        end = parse_local_datetime(kwargs["end_raw"], zone)
    elif kwargs.get("duration"):
        end = (start.astimezone(UTC) + parse_duration(kwargs["duration"])).astimezone(zone)
    elif converting:
        end = (start.astimezone(UTC) + parse_duration(_DEFAULT_DURATION)).astimezone(zone)
    else:
        end = start + (old_end - old_start)
    if invites is not None:
        invitees = [str(email) for email in invites]
    else:
        invitees = [
            str(person["email"]) for person in event.get("attendees") or [] if person.get("email")
        ]
    own = {config.email.lower()} if config.email else set()
    unique = list(dict.fromkeys(e for e in invitees if e.lower() not in own))
    on_event = frozenset(
        str(person["email"]).lower()
        for person in event.get("attendees") or []
        if person.get("email")
    )
    own_slot = None if converting else (old_start, old_end)
    return start.astimezone(zone), end.astimezone(zone), unique, own_slot, on_event
