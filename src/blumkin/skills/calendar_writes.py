"""Calendar write skills (accept, create, cancel, update)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from msgraph.generated.models.attendee import Attendee
from msgraph.generated.models.attendee_type import AttendeeType
from msgraph.generated.models.day_of_week import DayOfWeek
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.event import Event
from msgraph.generated.models.online_meeting_provider_type import OnlineMeetingProviderType
from msgraph.generated.models.patterned_recurrence import PatternedRecurrence
from msgraph.generated.models.recurrence_pattern import RecurrencePattern
from msgraph.generated.models.recurrence_pattern_type import RecurrencePatternType
from msgraph.generated.models.recurrence_range import RecurrenceRange
from msgraph.generated.models.recurrence_range_type import RecurrenceRangeType
from msgraph.generated.users.item.events.item.accept.accept_post_request_body import (
    AcceptPostRequestBody,
)
from msgraph.generated.users.item.events.item.cancel.cancel_post_request_body import (
    CancelPostRequestBody,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client
from blumkin.output import sanitize_terminal
from blumkin.skills.calendar import (
    _event_to_dict,
    _to_graph_dtz,
    calendar_today,
    parse_local_datetime,
)


@dataclass(frozen=True)
class Recurrence:
    """A normalized, provider-agnostic recurrence request.

    ``freq`` is ``daily`` / ``weekly`` / ``monthly``. ``days`` holds RRULE
    two-letter weekday codes (``MO``..``SU``) and is only ever set for weekly
    patterns. At most one of ``count`` / ``until`` is set; neither means an
    open-ended series.
    """

    freq: str
    interval: int = 1
    days: tuple[str, ...] = ()
    count: int | None = None
    until: date | None = None


_DEFAULT_DURATION = "30m"
_DURATION_RE = re.compile(
    r"^(\d+)\s*(m|min|mins|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$", re.I
)
# RRULE two-letter weekday code -> Graph DayOfWeek enum member.
_GRAPH_DAY_OF_WEEK = {
    "FR": DayOfWeek.Friday,
    "MO": DayOfWeek.Monday,
    "SA": DayOfWeek.Saturday,
    "SU": DayOfWeek.Sunday,
    "TH": DayOfWeek.Thursday,
    "TU": DayOfWeek.Tuesday,
    "WE": DayOfWeek.Wednesday,
}
# Google caps a reminder lead time at four weeks; keep both providers to that bound.
_MAX_REMINDER_MINUTES = 40320
# RRULE weekday codes in Monday-first order, so ``_RRULE_WEEKDAYS[dt.weekday()]``
# is the code for a given date and ``.index`` sorts a --days set canonically.
_RRULE_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
# Accepted --days tokens (three-letter and full names) -> RRULE code.
_WEEKDAY_BY_TOKEN = {
    "fri": "FR",
    "friday": "FR",
    "mon": "MO",
    "monday": "MO",
    "sat": "SA",
    "saturday": "SA",
    "sun": "SU",
    "sunday": "SU",
    "thu": "TH",
    "thursday": "TH",
    "tue": "TU",
    "tuesday": "TU",
    "wed": "WE",
    "wednesday": "WE",
}


async def calendar_accept(
    *,
    event_id: str | None = None,
    today_pending: bool = False,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if today_pending == bool(event_id):
        raise ValueError("exactly one of --event-id or --today-pending is required")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    if today_pending:
        tz = ZoneInfo(tz_name or cfg.default_tz)
        payload = await calendar_today(tz_name=str(tz), config=cfg)
        event_ids = [
            str(item["id"]) for item in payload["items"] if item.get("id") and _needs_accept(item)
        ]
    else:
        event_ids = [str(event_id)]
    body = AcceptPostRequestBody(send_response=True)
    for eid in event_ids:
        await client.me.events.by_event_id(eid).accept.post(body)
    return {"accepted": event_ids, "count": len(event_ids)}


async def calendar_cancel(
    *,
    event_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not event_id.strip():
        raise ValueError("--event-id is required")
    cfg = config or load_config()
    client = create_graph_client(cfg)
    await client.me.events.by_event_id(event_id).cancel.post(CancelPostRequestBody())
    return {"cancelled": event_id}


async def calendar_create(
    *,
    subject: str,
    with_emails: list[str],
    start_raw: str,
    duration: str | None = None,
    recurrence: Recurrence | None = None,
    remind_email: str | None = None,
    teams: bool = True,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not subject.strip():
        raise ValueError("--subject is required")
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    start = parse_local_datetime(start_raw, tz)
    # Add the duration in absolute time so an event crossing a DST transition keeps
    # its real length (matches the Google provider path).
    end = (start.astimezone(UTC) + parse_duration(duration or _DEFAULT_DURATION)).astimezone(tz)
    # Validate the recurrence against --start before any network call.
    recurrence_echo = recurrence_payload(recurrence, start) if recurrence is not None else None
    attendees = [
        Attendee(
            email_address=EmailAddress(address=email),
            type=AttendeeType.Required,
        )
        for email in with_emails
    ]
    event = Event(
        attendees=attendees or None,
        end=_to_graph_dtz(end),
        recurrence=_graph_recurrence(recurrence, start) if recurrence is not None else None,
        start=_to_graph_dtz(start),
        subject=subject.strip(),
    )
    if remind_email is not None:
        # Outlook events carry only a client-side (popup) reminder, not a per-event
        # email reminder; --remind-email maps to that. Google gets a real email.
        event.is_reminder_on = True
        event.reminder_minutes_before_start = reminder_minutes_before_start(remind_email)
    if teams:
        event.is_online_meeting = True
        event.online_meeting_provider = OnlineMeetingProviderType.TeamsForBusiness
    client = create_graph_client(cfg)
    created = await client.me.events.post(event)
    if created is None:
        raise RuntimeError("Graph returned no event from create")
    if teams and not _event_join_url(created):
        # Same async provisioning race calendar_update handles after PATCH.
        if not created.id:
            raise RuntimeError("Graph returned no event id from create")
        created = await client.me.events.by_event_id(created.id).get()
        if created is None or not created.id:
            raise RuntimeError("Graph returned no event after create re-fetch")
        if not _event_join_url(created):
            raise RuntimeError(
                f"Teams online meeting was not provisioned for event {created.id!r} "
                "(no onlineMeeting.joinUrl after create); retry or use "
                "`calendar update` after Graph finishes provisioning."
            )
    result: dict[str, Any] = {"event": _event_to_dict(created, tz)}
    if recurrence_echo is not None:
        result["recurrence"] = recurrence_echo
    return result


async def calendar_update(
    *,
    event_id: str,
    teams: bool = True,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Attach a Teams online meeting to an existing event (Calendars.ReadWrite only)."""
    if not event_id.strip():
        raise ValueError("--event-id is required")
    if not teams:
        raise ValueError("calendar update currently only attaches Teams; do not pass --no-teams")
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    client = create_graph_client(cfg)
    patch = Event(
        is_online_meeting=True,
        online_meeting_provider=OnlineMeetingProviderType.TeamsForBusiness,
    )
    updated = await client.me.events.by_event_id(event_id).patch(patch)
    # Graph may return 204 (None), or 200 before onlineMeeting is populated.
    if updated is None or not _event_join_url(updated):
        updated = await client.me.events.by_event_id(event_id).get()
    if updated is None or not updated.id:
        raise RuntimeError(f"Graph returned no event after update: {event_id}")
    if not _event_join_url(updated):
        raise RuntimeError(
            f"Teams online meeting was not provisioned for event {event_id!r} "
            "(no onlineMeeting.joinUrl after PATCH); retry or recreate with "
            "`calendar create --teams`."
        )
    return {"event": _event_to_dict(updated, tz)}


def format_accept_human(payload: dict[str, Any]) -> list[str]:
    ids = payload.get("accepted") or []
    lines = [f"Accepted {payload.get('count', len(ids))} event(s):"] + [f"  • {eid}" for eid in ids]
    # A batch that quietly left events behind is worse than one that says so; the
    # --json payload already carries this, and the default path must not drop it.
    for item in payload.get("skipped") or []:
        lines.append(f"  • skipped {item.get('id')}: {item.get('reason')}")
    return lines


def format_cancel_human(payload: dict[str, Any]) -> list[str]:
    return [f"Cancelled event {payload.get('cancelled')!r}"]


def format_create_human(payload: dict[str, Any]) -> list[str]:
    event = payload.get("event") or {}
    subject = sanitize_terminal(str(event.get("subject") or "(no subject)"))
    when = f"{event.get('start')} → {event.get('end')}"
    lines = [f"Created: {subject!r} ({when})"]
    recurrence = payload.get("recurrence")
    if recurrence:
        lines.append(f"  repeats: {_format_recurrence(recurrence)}")
    if event.get("online_join_url"):
        lines.append(f"  join: {sanitize_terminal(str(event['online_join_url']))}")
    lines.append(f"  id={event.get('id')}")
    return lines


def format_update_human(payload: dict[str, Any]) -> list[str]:
    event = payload.get("event") or {}
    subject = sanitize_terminal(str(event.get("subject") or "(no subject)"))
    lines = [f"Updated: {subject!r}"]
    if event.get("online_join_url"):
        lines.append(f"  join: {sanitize_terminal(str(event['online_join_url']))}")
    lines.append(f"  id={event.get('id')}")
    return lines


def parse_duration(raw: str) -> timedelta:
    text = raw.strip().lower()
    match = _DURATION_RE.match(text)
    if not match:
        raise ValueError(f"invalid duration {raw!r}; use forms like 30m, 1h, 1d, 1w")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("w"):
        return timedelta(weeks=amount)
    if unit.startswith("d"):
        return timedelta(days=amount)
    if unit.startswith("h"):
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def parse_recurrence(
    *,
    repeat: str,
    count: int | None = None,
    days: str | None = None,
    interval: int = 1,
    until: str | None = None,
) -> Recurrence:
    """Turn the ``calendar create`` recurrence flags into a :class:`Recurrence`.

    Raises ``ValueError`` (surfaced by the CLI as ``usage_error`` / exit 2) for
    any inconsistent combination. ``--until`` vs ``--start`` is checked later,
    once the start datetime is known (see :func:`recurrence_payload`).
    """
    freq = repeat.strip().lower()
    if freq not in {"daily", "monthly", "weekly"}:
        raise ValueError("--repeat must be one of daily, weekly, monthly")
    if interval < 1:
        raise ValueError("--interval must be >= 1")
    if until is not None and count is not None:
        raise ValueError("pass only one of --until or --count")
    parsed_until: date | None = None
    if until is not None:
        try:
            parsed_until = date.fromisoformat(until.strip())
        except ValueError as exc:
            raise ValueError(f"invalid --until date {until!r}; use YYYY-MM-DD") from exc
    if count is not None and count < 1:
        raise ValueError("--count must be >= 1")
    day_codes: tuple[str, ...] = ()
    if days is not None:
        if freq != "weekly":
            raise ValueError("--days only applies with --repeat weekly")
        seen: list[str] = []
        for token in days.split(","):
            key = token.strip().lower()
            if not key:
                continue
            code = _WEEKDAY_BY_TOKEN.get(key)
            if code is None:
                raise ValueError(
                    f"invalid --days value {token.strip()!r}; use mon,tue,wed,thu,fri,sat,sun"
                )
            if code not in seen:
                seen.append(code)
        if not seen:
            raise ValueError("--days needs at least one weekday")
        day_codes = tuple(sorted(seen, key=_RRULE_WEEKDAYS.index))
    return Recurrence(freq=freq, interval=interval, days=day_codes, count=count, until=parsed_until)


def recurrence_payload(recurrence: Recurrence, start: datetime) -> dict[str, Any]:
    """Normalized recurrence echo for the ``calendar create`` ``--json`` payload.

    Also the one place the start-relative validation lives, so both providers
    reject an ``--until`` that precedes ``--start`` identically.
    """
    if recurrence.until is not None and recurrence.until < start.date():
        raise ValueError(
            f"--until {recurrence.until.isoformat()} is before --start {start.date().isoformat()}"
        )
    if recurrence.freq == "weekly" and recurrence.days:
        start_code = _RRULE_WEEKDAYS[start.weekday()]
        if start_code not in recurrence.days:
            # RFC 5545: a DTSTART not matching the BYDAY set produces an undefined
            # series, and Graph rejects it outright. Fail fast with a clear message.
            raise ValueError(
                f"--days {','.join(c.lower() for c in recurrence.days)} must include the "
                f"--start weekday ({start_code.lower()})"
            )
    payload: dict[str, Any] = {"freq": recurrence.freq, "interval": recurrence.interval}
    if recurrence.freq == "weekly":
        codes = recurrence.days or (_RRULE_WEEKDAYS[start.weekday()],)
        payload["days"] = [code.lower() for code in codes]
    if recurrence.freq == "monthly":
        payload["day_of_month"] = start.day
    if recurrence.count is not None:
        payload["count"] = recurrence.count
    elif recurrence.until is not None:
        payload["until"] = recurrence.until.isoformat()
    else:
        payload["ends"] = "never"
    return payload


def recurrence_rrule(recurrence: Recurrence, start: datetime) -> list[str]:
    """Build the RFC 5545 ``RRULE`` line list for the Google Calendar event body."""
    parts = [f"FREQ={recurrence.freq.upper()}", f"INTERVAL={recurrence.interval}"]
    if recurrence.freq == "weekly" and recurrence.days:
        parts.append("BYDAY=" + ",".join(recurrence.days))
    if recurrence.count is not None:
        parts.append(f"COUNT={recurrence.count}")
    elif recurrence.until is not None:
        # RFC 5545: with a timezone-aware DTSTART, UNTIL must be a UTC timestamp.
        # Take the end of the until day in the event's zone so that day's
        # occurrence is still included.
        until_utc = datetime.combine(
            recurrence.until, datetime.max.time(), tzinfo=start.tzinfo
        ).astimezone(UTC)
        parts.append("UNTIL=" + until_utc.strftime("%Y%m%dT%H%M%SZ"))
    return ["RRULE:" + ";".join(parts)]


def reminder_minutes_before_start(raw: str) -> int:
    """Whole minutes before start for a reminder lead time like ``30m``/``1h``/``1d``."""
    minutes = int(parse_duration(raw).total_seconds() // 60)
    if minutes <= 0:
        raise ValueError(f"reminder lead time {raw!r} must be positive")
    if minutes > _MAX_REMINDER_MINUTES:
        raise ValueError(
            f"reminder lead time {raw!r} exceeds the four-week maximum "
            f"({_MAX_REMINDER_MINUTES} minutes)"
        )
    return minutes


def _event_join_url(event: Any) -> str | None:
    meeting = getattr(event, "online_meeting", None)
    url = getattr(meeting, "join_url", None) if meeting is not None else None
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _format_recurrence(recurrence: dict[str, Any]) -> str:
    """One-line human summary of a :func:`recurrence_payload` dict."""
    freq = str(recurrence.get("freq") or "?")
    interval = int(recurrence.get("interval") or 1)
    unit = {"daily": "day", "monthly": "month", "weekly": "week"}.get(freq, freq)
    text = freq if interval == 1 else f"every {interval} {unit}s"
    days = recurrence.get("days")
    if days:
        text += " on " + ", ".join(days)
    if recurrence.get("count") is not None:
        text += f", {recurrence['count']} times"
    elif recurrence.get("until"):
        text += f" until {recurrence['until']}"
    else:
        text += ", no end"
    return text


def _graph_recurrence(recurrence: Recurrence, start: datetime) -> PatternedRecurrence:
    if recurrence.freq == "daily":
        pattern = RecurrencePattern(interval=recurrence.interval, type=RecurrencePatternType.Daily)
    elif recurrence.freq == "weekly":
        codes = recurrence.days or (_RRULE_WEEKDAYS[start.weekday()],)
        pattern = RecurrencePattern(
            days_of_week=[_GRAPH_DAY_OF_WEEK[code] for code in codes],
            # Required by Graph for weekly patterns; Monday matches the RRULE
            # default (WKST=MO) so both providers count multi-week intervals alike.
            first_day_of_week=DayOfWeek.Monday,
            interval=recurrence.interval,
            type=RecurrencePatternType.Weekly,
        )
    else:  # monthly: same day-of-month as --start, matching Google FREQ=MONTHLY
        pattern = RecurrencePattern(
            day_of_month=start.day,
            interval=recurrence.interval,
            type=RecurrencePatternType.AbsoluteMonthly,
        )
    if recurrence.count is not None:
        rng = RecurrenceRange(
            number_of_occurrences=recurrence.count,
            start_date=start.date(),
            type=RecurrenceRangeType.Numbered,
        )
    elif recurrence.until is not None:
        rng = RecurrenceRange(
            end_date=recurrence.until,
            start_date=start.date(),
            type=RecurrenceRangeType.EndDate,
        )
    else:
        rng = RecurrenceRange(start_date=start.date(), type=RecurrenceRangeType.NoEnd)
    return PatternedRecurrence(pattern=pattern, range=rng)


def _needs_accept(item: dict[str, Any]) -> bool:
    if item.get("is_organizer"):
        return False
    response = (item.get("response") or "").lower()
    return "notresponded" in response or response in {"", "none"}
