"""Calendar write skills (accept, create, cancel, update)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from msgraph.generated.models.attendee import Attendee
from msgraph.generated.models.attendee_type import AttendeeType
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.day_of_week import DayOfWeek
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.event import Event
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.location import Location
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
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
from blumkin.graph import create_graph_client, is_id_lookup_failure, request_config
from blumkin.output import sanitize_terminal
from blumkin.skills.calendar import (
    CalendarEventNotFoundError,
    _event_to_dict,
    _to_graph_dtz,
    calendar_today,
    format_recurrence,
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
    all_day: bool = False,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    duration: str | None = None,
    location: str | None = None,
    optional_emails: list[str] | None = None,
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
    body_content, graph_body_type = resolve_event_body(body, body_file, body_type)
    # An all-day event cannot host a Teams online meeting; never try to attach one
    # (Graph would fail the create and leave a half-built event behind).
    teams = teams and not all_day
    if all_day:
        first_day, last_day = _all_day_bounds(start_raw, duration)
        start = datetime.combine(first_day, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(last_day, datetime.min.time(), tzinfo=tz)
    else:
        reject_date_only_start(start_raw)
        start = parse_local_datetime(start_raw, tz)
        # Add the duration in absolute time so an event crossing a DST transition
        # keeps its real length (matches the Google provider path).
        end = (start.astimezone(UTC) + parse_duration(duration or _DEFAULT_DURATION)).astimezone(tz)
    # Validate the recurrence against --start before any network call.
    recurrence_echo = recurrence_payload(recurrence, start) if recurrence is not None else None
    attendees = [
        Attendee(email_address=EmailAddress(address=email), type=AttendeeType.Required)
        for email in with_emails
    ] + [
        Attendee(email_address=EmailAddress(address=email), type=AttendeeType.Optional)
        for email in optional_emails or []
    ]
    event = Event(
        attendees=attendees or None,
        body=ItemBody(content=body_content, content_type=graph_body_type)
        if body_content is not None
        else None,
        end=_to_graph_dtz(end),
        is_all_day=all_day or None,
        location=Location(display_name=location) if location else None,
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
    all_day: bool | None = None,
    body: str | None = None,
    body_file: str | None = None,
    body_type: str = "text",
    duration: str | None = None,
    end_raw: str | None = None,
    location: str | None = None,
    start_raw: str | None = None,
    subject: str | None = None,
    teams: bool | None = None,
    with_emails: list[str] | None = None,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """PATCH an existing event's fields (Calendars.ReadWrite).

    Every argument is optional; only what is passed is changed. ``teams`` is
    tri-state: ``None`` leaves the online meeting alone, ``True`` attaches one,
    ``False`` removes it. Editing a recurring series edits the whole series.
    """
    eid = event_id.strip()
    if not eid:
        raise ValueError("--event-id is required")
    if end_raw is not None and duration is not None:
        raise ValueError("pass only one of --end or --duration")
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    graph_body_type = BodyType.Text
    body_content = None
    if body is not None or body_file is not None:
        body_content, graph_body_type = resolve_event_body(body, body_file, body_type)
    client = create_graph_client(cfg)

    changes_time = start_raw is not None or end_raw is not None or duration is not None
    need_existing = changes_time or all_day is not None
    existing = None
    if need_existing:
        try:
            existing = await client.me.events.by_event_id(eid).get(
                request_config(headers={"Prefer": 'outlook.timezone="UTC"'})
            )
        except ODataError as exc:
            # A 404, or a 400 with an id-shaped code, on the pre-edit GET means the
            # event is gone - map it like calendar_get rather than leaking ODataError.
            if not is_id_lookup_failure(exc):
                raise
            raise CalendarEventNotFoundError(f"event not found: {eid}") from exc
        if existing is None or not existing.id:
            raise CalendarEventNotFoundError(f"event not found: {eid}")

    patch = Event()
    touched = False
    if subject is not None:
        patch.subject = subject.strip()
        touched = True
    if location is not None:
        patch.location = Location(display_name=location)
        touched = True
    if body_content is not None:
        patch.body = ItemBody(content=body_content, content_type=graph_body_type)
        touched = True
    if with_emails is not None:
        patch.attendees = [
            Attendee(email_address=EmailAddress(address=email), type=AttendeeType.Required)
            for email in with_emails
        ] or None
        touched = True
    if changes_time or all_day is not None:
        new_start, new_end, is_all_day = _updated_bounds(
            existing, start_raw, end_raw, duration, all_day, tz
        )
        patch.start = _to_graph_dtz(new_start)
        patch.end = _to_graph_dtz(new_end)
        if is_all_day is not None:
            patch.is_all_day = is_all_day
        touched = True
    if teams is True:
        patch.is_online_meeting = True
        patch.online_meeting_provider = OnlineMeetingProviderType.TeamsForBusiness
        touched = True
    elif teams is False:
        patch.is_online_meeting = False
        touched = True

    if not touched:
        raise ValueError(
            "nothing to update; pass at least one of --subject / --start / --duration / "
            "--end / --location / --body / --with / --all-day / --teams"
        )

    updated = await client.me.events.by_event_id(eid).patch(patch)
    if updated is None:
        updated = await client.me.events.by_event_id(eid).get()
    if updated is None or not updated.id:
        raise CalendarEventNotFoundError(f"event not found: {eid}")
    if teams is True and not _event_join_url(updated):
        # Graph provisions the Teams meeting asynchronously, so the PATCH (or
        # 204) response can precede onlineMeeting.joinUrl. Re-GET once, then fail
        # loudly - the same guard calendar_create keeps - rather than report a
        # join-less success an agent would trust.
        updated = await client.me.events.by_event_id(eid).get()
        if updated is None or not updated.id:
            raise CalendarEventNotFoundError(f"event not found: {eid}")
        if not _event_join_url(updated):
            raise RuntimeError(
                f"Teams online meeting was not provisioned for event {eid!r} "
                "(no onlineMeeting.joinUrl after update); retry once Graph "
                "finishes provisioning."
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


def _format_when(event: dict[str, Any]) -> str:
    """Human ``when`` for a created/updated event: ``all day`` or ``start -> end``."""
    if event.get("is_all_day"):
        return "all day"
    return f"{event.get('start')} → {event.get('end')}"


def format_create_human(payload: dict[str, Any]) -> list[str]:
    event = payload.get("event") or {}
    subject = sanitize_terminal(str(event.get("subject") or "(no subject)"))
    when = _format_when(event)
    lines = [f"Created: {subject!r} ({when})"]
    recurrence = payload.get("recurrence")
    if recurrence:
        lines.append(f"  repeats: {format_recurrence(recurrence)}")
    if event.get("online_join_url"):
        lines.append(f"  join: {sanitize_terminal(str(event['online_join_url']))}")
    lines.append(f"  id={event.get('id')}")
    return lines


def format_update_human(payload: dict[str, Any]) -> list[str]:
    event = payload.get("event") or {}
    subject = sanitize_terminal(str(event.get("subject") or "(no subject)"))
    when = "all day" if event.get("is_all_day") else f"{event.get('start')} → {event.get('end')}"
    lines = [f"Updated: {subject!r} ({when})"]
    if event.get("location"):
        lines.append(f"  location: {sanitize_terminal(str(event['location']))}")
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


def resolve_event_body(
    body: str | None, body_file: str | None, body_type: str
) -> tuple[str | None, BodyType]:
    """``(content, Graph BodyType)`` from the event-body flags.

    ``content`` is ``None`` when neither ``--body`` nor ``--body-file`` is given
    (unlike mail, an event body is optional).
    """
    label = body_type.strip().lower()
    if label not in {"html", "text"}:
        raise ValueError("--body-type must be 'text' or 'html'")
    graph_type = BodyType.Html if label == "html" else BodyType.Text
    if body is not None and body_file is not None:
        raise ValueError("pass only one of --body or --body-file")
    if body_file is not None:
        try:
            return Path(body_file).read_text(encoding="utf-8"), graph_type
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read --body-file {body_file}: {exc}") from exc
    return body, graph_type


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


def recurrence_rrule(
    recurrence: Recurrence, start: datetime, *, all_day: bool = False
) -> list[str]:
    """Build the RFC 5545 ``RRULE`` line list for the Google Calendar event body."""
    parts = [f"FREQ={recurrence.freq.upper()}", f"INTERVAL={recurrence.interval}"]
    if recurrence.freq == "weekly" and recurrence.days:
        parts.append("BYDAY=" + ",".join(recurrence.days))
    if recurrence.count is not None:
        parts.append(f"COUNT={recurrence.count}")
    elif recurrence.until is not None:
        if all_day:
            # RFC 5545: an all-day event has a DATE-valued DTSTART, so UNTIL must
            # also be a date (a DATE-TIME here is a type mismatch Google rejects).
            parts.append("UNTIL=" + recurrence.until.strftime("%Y%m%d"))
        else:
            # With a timezone-aware DATE-TIME DTSTART, UNTIL must be a UTC
            # timestamp. Take the end of the until day in the event's zone so
            # that day's occurrence is still included.
            until_utc = datetime.combine(
                recurrence.until, datetime.max.time(), tzinfo=start.tzinfo
            ).astimezone(UTC)
            parts.append("UNTIL=" + until_utc.strftime("%Y%m%dT%H%M%SZ"))
    return ["RRULE:" + ";".join(parts)]


def reject_date_only_start(start_raw: str) -> None:
    """Reject a bare ``YYYY-MM-DD`` ``--start`` unless ``--all-day`` was passed.

    Without this a forgotten ``--all-day`` silently books a zero-dark timed
    meeting at 00:00 instead of the all-day hold the date implies.
    """
    # Any legitimate timed start carries a "T" (2026-12-24T09:00[Z]); a bare
    # date, with or without a trailing UTC marker, is a forgotten --all-day.
    if "T" not in start_raw.strip():
        raise ValueError(
            "a date-only --start needs --all-day; "
            "pass a time (e.g. 2026-12-24T09:00) for a timed event"
        )


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


def _all_day_bounds(start_raw: str, duration: str | None) -> tuple[date, date]:
    """``(first_day, end_day_exclusive)`` for an all-day event; ``--duration`` is whole days."""
    text = start_raw.strip()
    if "T" in text:
        raise ValueError("--all-day needs a date --start (YYYY-MM-DD), not a time")
    try:
        first = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid --start date {start_raw!r}; use YYYY-MM-DD") from exc
    days = 1
    if duration is not None:
        delta = parse_duration(duration)
        if delta < timedelta(days=1) or delta % timedelta(days=1):
            raise ValueError("--all-day --duration must be whole days, e.g. 1d, 3d")
        days = delta.days
    return first, first + timedelta(days=days)


def _all_day_span_days(
    first: date,
    end_raw: str | None,
    duration: str | None,
    old_span_days: int | None,
    all_day: bool | None,
) -> int:
    """Whole-day length of an all-day event edit.

    Precedence: an explicit ``--end`` date (exclusive, matching Google's all-day
    end-date semantics), then a whole-day ``--duration``, then the event's
    current span for a move that keeps ``--all-day`` (``all_day is None``), else
    a single day.
    """
    if end_raw is not None:
        if "T" in end_raw:
            raise ValueError("--all-day needs a date --end (YYYY-MM-DD), not a time")
        span = (date.fromisoformat(end_raw.strip()) - first).days
        if span < 1:
            raise ValueError("--all-day --end must be at least one day after --start")
        return span
    if duration is not None:
        delta = parse_duration(duration)
        if delta < timedelta(days=1) or delta % timedelta(days=1):
            raise ValueError("--all-day --duration must be whole days, e.g. 1d, 3d")
        return delta.days
    if all_day is None and old_span_days:
        return max(1, old_span_days)
    return 1


def _dtz_to_utc_datetime(dtz: Any) -> datetime | None:
    """Graph DateTimeTimeZone fetched with ``Prefer: outlook.timezone="UTC"`` -> aware datetime."""
    raw = getattr(dtz, "date_time", None)
    if not raw:
        return None
    text = str(raw).rstrip("Z")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


def _updated_bounds(
    existing: Any,
    start_raw: str | None,
    end_raw: str | None,
    duration: str | None,
    all_day: bool | None,
    tz: ZoneInfo,
) -> tuple[datetime, datetime, bool | None]:
    """Resolve the new (start, end, is_all_day) for a time edit, filling from ``existing``."""
    old_start = _dtz_to_utc_datetime(getattr(existing, "start", None))
    old_end = _dtz_to_utc_datetime(getattr(existing, "end", None))
    was_all_day = bool(getattr(existing, "is_all_day", False))
    target_all_day = was_all_day if all_day is None else all_day
    # An already-all-day event serializes at 00:00 UTC under Prefer:
    # outlook.timezone="UTC", so its UTC date IS the calendar date - read it
    # directly. A timed event is a real instant, so its calendar date is the
    # local one (the UTC date can be a day off in either direction).
    old_start_date = (
        (old_start.date() if was_all_day else old_start.astimezone(tz).date())
        if old_start
        else None
    )
    old_end_date = old_end.date() if old_end and was_all_day else None

    if target_all_day:
        if start_raw is not None and "T" in start_raw:
            raise ValueError("--all-day needs a date --start (YYYY-MM-DD), not a time")
        first = (
            date.fromisoformat(start_raw.strip())
            if start_raw is not None
            else (old_start_date or datetime.now(tz).date())
        )
        old_span_days = (
            (old_end_date - old_start_date).days
            if was_all_day and old_start_date and old_end_date
            else None
        )
        days = _all_day_span_days(first, end_raw, duration, old_span_days, all_day)
        start_dt = datetime.combine(first, datetime.min.time(), tzinfo=tz)
        return start_dt, start_dt + timedelta(days=days), True if all_day is not None else None

    # A bare YYYY-MM-DD start/end on a timed event means a forgotten --all-day;
    # reject it rather than silently booking a 00:00 meeting (matches create).
    if not was_all_day:
        if start_raw is not None:
            reject_date_only_start(start_raw)
        if end_raw is not None and "T" not in end_raw.strip():
            raise ValueError(
                "a date-only --end needs --all-day; pass a time (e.g. 2026-12-24T17:00)"
            )
    if start_raw is not None:
        start_dt = parse_local_datetime(start_raw, tz)
    elif was_all_day and old_start_date:
        # all-day -> timed: keep the date, open at local midnight.
        start_dt = datetime.combine(old_start_date, datetime.min.time(), tzinfo=tz)
    elif old_start:
        start_dt = old_start.astimezone(tz)
    else:
        start_dt = datetime.now(tz)
    if end_raw is not None:
        end_dt = parse_local_datetime(end_raw, tz)
    elif duration is not None:
        end_dt = (start_dt.astimezone(UTC) + parse_duration(duration)).astimezone(tz)
    elif old_start and old_end and not was_all_day:
        end_dt = (start_dt.astimezone(UTC) + (old_end - old_start)).astimezone(tz)
    else:
        end_dt = (start_dt.astimezone(UTC) + parse_duration(_DEFAULT_DURATION)).astimezone(tz)
    if end_dt <= start_dt:
        raise ValueError("event end must be after start")
    return start_dt, end_dt, False if all_day is not None else None


def _event_join_url(event: Any) -> str | None:
    meeting = getattr(event, "online_meeting", None)
    url = getattr(meeting, "join_url", None) if meeting is not None else None
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


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
