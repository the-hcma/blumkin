"""Google Calendar skills (reads plus ``create``), skill-shaped payloads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import (
    CALENDAR_FREEBUSY_SCOPES,
    CALENDAR_READ_SCOPES,
    CALENDAR_SCOPES,
    get_credentials,
)
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.calendar import (
    CalendarEventNotFoundError,
    find_mutual_free_slots,
    parse_local_datetime,
)
from blumkin.skills.calendar_writes import (
    _DEFAULT_DURATION,
    Recurrence,
    _all_day_bounds,
    _all_day_span_days,
    _needs_accept,
    parse_duration,
    recurrence_payload,
    recurrence_rrule,
    reject_date_only_start,
    reminder_minutes_before_start,
    resolve_event_body,
)
from blumkin.skills.freebusy_suggest import collect_busy_intervals, raise_if_schedule_errors

_RRULE_FREQ = {"DAILY": "daily", "MONTHLY": "monthly", "WEEKLY": "weekly"}
# Plain RRULE weekday codes; a prefixed form ("2WE") is an ordinal we cannot model.
_RRULE_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")  # index == date.weekday()
_RRULE_WEEKDAY_CODES = frozenset(_RRULE_WEEKDAYS)

# Google responseStatus -> the Graph vocabulary _needs_accept and the --json
# contract already speak, so both providers answer `response` the same way.
_RESPONSE_BY_GOOGLE_STATUS = {
    "accepted": "accepted",
    "declined": "declined",
    "needsAction": "notResponded",
    "tentative": "tentativelyAccepted",
}


async def calendar_accept(
    *,
    event_id: str | None = None,
    today_pending: bool = False,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """RSVP accepted on one event, or on today's unanswered invitations."""
    if today_pending == bool(event_id):
        raise ValueError("exactly one of --event-id or --today-pending is required")
    cfg = config or load_config()
    service = _calendar_service(cfg)
    if today_pending:
        payload = await calendar_today(tz_name=tz_name, config=cfg)
        event_ids = [
            str(item["id"]) for item in payload["items"] if item.get("id") and _needs_accept(item)
        ]
    else:
        event_ids = [str(event_id)]
    accepted: list[str] = []
    skipped: list[dict[str, str]] = []
    for eid in event_ids:
        try:
            _accept_one(service, eid)
        except Exception as exc:  # noqa: BLE001 - a batch must always report
            if not today_pending:
                # An explicit --event-id is a specific ask: surface the reason.
                raise
            # A batch must not abort half-done with earlier RSVPs already sent and
            # no report of what was left. That covers both an event we cannot act on
            # (_needs_accept passes anything with an unknown response, including
            # events carrying no self attendee) and an HTTP failure on one event -
            # a 404 for something deleted since the listing, a transient 5xx, or a
            # socket timeout - which is not an HttpError at all, so the catch has to
            # be broad. Re-running is safe: an already-accepted event is a no-op.
            skipped.append({"id": eid, "reason": str(exc)})
            continue
        accepted.append(eid)
    return {"accepted": accepted, "count": len(accepted), "skipped": skipped}


async def calendar_cancel(
    *,
    event_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Cancel an event and notify its attendees."""
    eid = event_id.strip()
    if not eid:
        raise ValueError("--event-id is required")
    cfg = config or load_config()
    service = _calendar_service(cfg)
    existing = execute(service.events().get(calendarId="primary", eventId=eid))
    if not (existing.get("organizer") or {}).get("self"):
        # events.delete on your primary calendar only removes *your copy* of someone
        # else's event: attendees are never told and the meeting goes ahead. Reporting
        # "cancelled" for that would be a lie, and Graph 403s here, so refuse to match.
        raise ValueError(
            f"you do not organize event {eid!r}, so cancelling it would only remove your "
            "own copy without telling anyone; decline it in your calendar client instead"
        )
    execute(
        service.events().delete(calendarId="primary", eventId=eid, sendUpdates="all"),
        # Cancellation mails attendees; a blind retry past a partial failure could
        # not un-send them, and a repeat delete 410s anyway.
        num_retries=0,
    )
    return {"cancelled": eid}


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
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not subject.strip():
        raise ValueError("--subject is required")
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    tz_key = tz.key if isinstance(tz, ZoneInfo) else str(tz)
    description, _ = resolve_event_body(body, body_file, body_type)
    if all_day:
        first_day, end_day = _all_day_bounds(start_raw, duration)
        start = datetime.combine(first_day, time(), tzinfo=tz)
        payload_start: dict[str, Any] = {"date": first_day.isoformat()}
        payload_end: dict[str, Any] = {"date": end_day.isoformat()}
    else:
        reject_date_only_start(start_raw)
        start = parse_local_datetime(start_raw, tz)
        # Absolute-time arithmetic so an event spanning a DST transition keeps its
        # real length.
        end = (start.astimezone(UTC) + parse_duration(duration or _DEFAULT_DURATION)).astimezone(tz)
        payload_start = {"dateTime": start.isoformat(timespec="seconds"), "timeZone": tz_key}
        payload_end = {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tz_key}
    # Validate the recurrence against --start before any network call.
    recurrence_echo = recurrence_payload(recurrence, start) if recurrence is not None else None
    event_body: dict[str, Any] = {
        "summary": subject.strip(),
        "start": payload_start,
        "end": payload_end,
    }
    if description is not None:
        event_body["description"] = description
    if location:
        event_body["location"] = location
    if recurrence is not None:
        event_body["recurrence"] = recurrence_rrule(recurrence, start, all_day=all_day)
    attendees = [{"email": email} for email in with_emails] + [
        {"email": email, "optional": True} for email in optional_emails or []
    ]
    if attendees:
        event_body["attendees"] = attendees
    if remind_email is not None:
        event_body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": reminder_minutes_before_start(remind_email)}
            ],
        }
    service = _calendar_service(cfg)
    created = execute(
        service.events().insert(
            calendarId="primary",
            body=event_body,
            sendUpdates="all" if attendees else "none",
        ),
        # events.insert is a non-idempotent POST; a blind retry could double-book.
        num_retries=0,
    )
    result: dict[str, Any] = {"event": _event_to_dict(created, tz)}
    if recurrence_echo is not None:
        result["recurrence"] = recurrence_echo
    return result


async def calendar_get(
    *,
    event_id: str,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Read one event in full - description, per-attendee responses, recurrence.

    ``--body-type`` is a Microsoft-only knob: Google stores a single description
    (which may contain HTML) and does not convert it server-side.
    """
    eid = event_id.strip()
    if not eid:
        raise ValueError("--event-id is required")
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    service = _calendar_service(cfg, required_scopes=CALENDAR_READ_SCOPES)
    try:
        event = execute(service.events().get(calendarId="primary", eventId=eid))
    except HttpError as exc:
        if getattr(getattr(exc, "resp", None), "status", None) in {404, 410}:
            raise CalendarEventNotFoundError(f"event not found: {eid}") from exc
        raise
    return {"event": _event_detail_to_dict(event, tz)}


async def calendar_freebusy(
    *,
    with_emails: list[str],
    start: datetime,
    end: datetime,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not with_emails:
        raise ValueError("at least one --with email is required")
    if end <= start:
        raise ValueError("--end must be after --start")
    cfg = config or load_config()
    tz_name = start.tzinfo.key if isinstance(start.tzinfo, ZoneInfo) else str(start.tzinfo)
    display_tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    service = _calendar_service(cfg, required_scopes=CALENDAR_FREEBUSY_SCOPES)
    body = {
        "items": [{"id": email} for email in with_emails],
        "timeMax": _rfc3339(end),
        "timeMin": _rfc3339(start),
    }
    response = execute(service.freebusy().query(body=body))
    calendars = response.get("calendars") or {}
    items = [
        _schedule_to_dict(email, calendars.get(email) or {}, display_tz) for email in with_emails
    ]
    return {
        "end": end.isoformat(),
        "items": items,
        "start": start.isoformat(),
        "timezone": tz_name,
    }


async def calendar_suggest(
    *,
    with_emails: list[str],
    start: datetime,
    end: datetime,
    duration: timedelta,
    window: str | None = None,
    treat_tentative: str = "busy",
    step: timedelta | None = None,
    limit: int = 10,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if duration <= timedelta(0):
        raise ValueError("--duration must be positive")
    if limit < 1:
        raise ValueError("--limit must be >= 1")
    tentative = treat_tentative.strip().lower()
    if tentative not in {"busy", "free"}:
        raise ValueError("--treat-tentative must be 'busy' or 'free'")
    if tentative == "free":
        raise ValueError(
            "--treat-tentative free is not supported for provider=google yet "
            "(Calendar freebusy only returns busy intervals; use the default busy)"
        )
    window_bounds = _parse_day_window(window) if window is not None else None
    step_delta = step if step is not None else min(timedelta(minutes=15), duration)
    if step_delta <= timedelta(0):
        raise ValueError("--step must be positive")
    freebusy = await calendar_freebusy(with_emails=with_emails, start=start, end=end, config=config)
    raise_if_schedule_errors(freebusy["items"], requested=with_emails)
    busy = collect_busy_intervals(freebusy["items"], treat_tentative_busy=True)
    slots = find_mutual_free_slots(
        busy=busy,
        range_start=start,
        range_end=end,
        duration=duration,
        window=window_bounds,
        step=step_delta,
        limit=limit,
    )
    return {
        "duration_minutes": int(duration.total_seconds() // 60),
        "end": end.isoformat(),
        "limit": limit,
        "slots": slots,
        "start": start.isoformat(),
        "step_minutes": int(step_delta.total_seconds() // 60),
        "timezone": freebusy["timezone"],
        "treat_tentative": tentative,
        "window": window,
        "with": list(with_emails),
    }


async def calendar_today(
    *,
    day: date | None = None,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    target = day or datetime.now(tz).date()
    start = datetime(target.year, target.month, target.day, tzinfo=tz)
    end = start + timedelta(days=1)
    payload = await calendar_view(start=start, end=end, config=cfg)
    return {
        "date": target.isoformat(),
        "items": payload["items"],
        "timezone": payload["timezone"],
    }


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
    """PATCH an existing event's fields. ``--teams`` maps to a Google Meet link.

    Every argument is optional; only what is passed is changed. ``teams`` is
    tri-state (None leave / True attach a Meet link / False remove it).
    """
    eid = event_id.strip()
    if not eid:
        raise ValueError("--event-id is required")
    if end_raw is not None and duration is not None:
        raise ValueError("pass only one of --end or --duration")
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    tz_key = tz.key if isinstance(tz, ZoneInfo) else str(tz)
    description = None
    if body is not None or body_file is not None:
        description, _ = resolve_event_body(body, body_file, body_type)
    service = _calendar_service(cfg)

    changes_time = start_raw is not None or end_raw is not None or duration is not None
    existing: Mapping[str, Any] | None = None
    if changes_time or all_day is not None:
        try:
            existing = execute(service.events().get(calendarId="primary", eventId=eid))
        except HttpError as exc:
            # Same 404/410 -> not_found mapping the PATCH below uses, so a time
            # edit against a missing/deleted event does not leak a raw HttpError.
            if getattr(getattr(exc, "resp", None), "status", None) in {404, 410}:
                raise CalendarEventNotFoundError(f"event not found: {eid}") from exc
            raise

    patch: dict[str, Any] = {}
    if subject is not None:
        patch["summary"] = subject.strip()
    if location is not None:
        patch["location"] = location
    if description is not None:
        patch["description"] = description
    if with_emails is not None:
        patch["attendees"] = [{"email": email} for email in with_emails]
    if changes_time or all_day is not None:
        start_dt, end_dt, is_all_day = _google_updated_bounds(
            existing or {}, start_raw, end_raw, duration, all_day, tz
        )
        if is_all_day:
            patch["start"] = {"date": start_dt.date().isoformat()}
            patch["end"] = {"date": end_dt.date().isoformat()}
        else:
            patch["start"] = {
                "dateTime": start_dt.isoformat(timespec="seconds"),
                "timeZone": tz_key,
            }
            patch["end"] = {"dateTime": end_dt.isoformat(timespec="seconds"), "timeZone": tz_key}

    conference_version = 0
    if teams is True:
        patch["conferenceData"] = {
            "createRequest": {
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
                "requestId": uuid4().hex,
            }
        }
        conference_version = 1
    elif teams is False:
        patch["conferenceData"] = None
        conference_version = 1

    if not patch:
        raise ValueError(
            "nothing to update; pass at least one of --subject / --start / --duration / "
            "--end / --location / --body / --with / --all-day / --teams"
        )

    try:
        updated = execute(
            service.events().patch(
                calendarId="primary",
                eventId=eid,
                body=patch,
                conferenceDataVersion=conference_version,
                sendUpdates="all",
            ),
            num_retries=0,
        )
    except HttpError as exc:
        if getattr(getattr(exc, "resp", None), "status", None) in {404, 410}:
            raise CalendarEventNotFoundError(f"event not found: {eid}") from exc
        raise
    if teams is True and not _meet_link(updated):
        # Meet provisions asynchronously; conferenceData.status can still be
        # "pending" on the immediate follow-up read. Re-GET once, then fail loudly
        # - the same guard calendar_create / the Graph update path keep - rather
        # than return a join-less success an agent would trust.
        updated = execute(service.events().get(calendarId="primary", eventId=eid))
        if not _meet_link(updated):
            raise RuntimeError(
                f"Google Meet was not provisioned for event {eid!r} "
                "(no conferenceData entry point after update); retry once Google "
                "finishes provisioning."
            )
    return {"event": _event_to_dict(updated, tz)}


async def calendar_view(
    *,
    start: datetime,
    end: datetime,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if end <= start:
        raise ValueError("end must be after start")
    cfg = config or load_config()
    display_tz = start.tzinfo if isinstance(start.tzinfo, ZoneInfo) else ZoneInfo("UTC")
    service = _calendar_service(cfg, required_scopes=CALENDAR_READ_SCOPES)
    response = execute(
        service.events().list(
            calendarId="primary",
            singleEvents=True,
            orderBy="startTime",
            timeMax=_rfc3339(end),
            timeMin=_rfc3339(start),
        )
    )
    events = [_event_to_dict(item, display_tz) for item in (response.get("items") or [])]
    return {
        "end": end.isoformat(),
        "items": events,
        "start": start.isoformat(),
        "timezone": str(display_tz),
    }


def _accept_one(service: Any, event_id: str) -> None:
    """Set the signed-in attendee's responseStatus to accepted on one event.

    Google has no accept action: you patch your own entry in the attendee list,
    so the current list has to be read first and sent back with just that one
    entry changed.
    """
    event = execute(service.events().get(calendarId="primary", eventId=event_id))
    if event.get("attendeesOmitted"):
        # attendees is a full replace on PATCH, and Google truncates the list it
        # returns for large meetings. Writing the short list back would delete every
        # omitted attendee and mail everyone about it, so refuse instead.
        raise ValueError(
            f"event {event_id!r} returned a truncated attendee list "
            "(attendeesOmitted); accepting it here would drop the omitted attendees, "
            "so RSVP in your calendar client instead"
        )
    attendees = [dict(a) for a in (event.get("attendees") or []) if isinstance(a, dict)]
    mine = next((a for a in attendees if a.get("self")), None)
    if mine is None:
        raise ValueError(
            f"event {event_id!r} does not list you as an attendee, so there is nothing to accept"
        )
    mine["responseStatus"] = "accepted"
    execute(
        service.events().patch(
            calendarId="primary",
            eventId=event_id,
            body={"attendees": attendees},
            sendUpdates="all",
        ),
        num_retries=0,
    )


def _busy_slot_to_dict(slot: dict[str, Any], display_tz: ZoneInfo) -> dict[str, Any]:
    return {
        "end": _google_dt_to_iso(slot.get("end"), display_tz),
        "start": _google_dt_to_iso(slot.get("start"), display_tz),
        "status": "busy",
    }


def _calendar_service(
    cfg: BlumkinConfig, *, required_scopes: frozenset[str] = CALENDAR_SCOPES
) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=required_scopes)
    return build_api_service("calendar", "v3", creds=creds, config=cfg)


def _attendee_to_dict(attendee: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": attendee.get("email"),
        "name": attendee.get("displayName"),
        "response": _RESPONSE_BY_GOOGLE_STATUS.get(str(attendee.get("responseStatus") or "")),
        "type": "optional" if attendee.get("optional") else "required",
    }


def _event_detail_to_dict(ev: dict[str, Any], display_tz: ZoneInfo) -> dict[str, Any]:
    detail = _event_to_dict(ev, display_tz)
    detail["attendees"] = [
        _attendee_to_dict(a) for a in (ev.get("attendees") or []) if isinstance(a, dict)
    ]
    detail["body"] = ev.get("description")
    # Google stores a single representation that may contain HTML; --body-type is
    # a Microsoft-only knob (Graph converts the body server-side).
    detail["body_type"] = "html"
    detail["is_cancelled"] = ev.get("status") == "cancelled"
    detail["recurrence"] = _rrule_to_payload(ev.get("recurrence"), display_tz, _start_date(ev))
    detail["series_master_id"] = ev.get("recurringEventId")
    detail["web_link"] = ev.get("htmlLink")
    return detail


def _event_to_dict(ev: dict[str, Any], display_tz: ZoneInfo) -> dict[str, Any]:
    start_raw = ev.get("start") or {}
    end_raw = ev.get("end") or {}
    is_all_day = "date" in start_raw and "dateTime" not in start_raw
    start = _google_dt_to_iso(start_raw.get("dateTime") or start_raw.get("date"), display_tz)
    end = _google_dt_to_iso(end_raw.get("dateTime") or end_raw.get("date"), display_tz)
    organizer = ev.get("organizer") or {}
    location = ev.get("location") or None
    return {
        "end": end,
        "id": ev.get("id"),
        "is_all_day": is_all_day,
        "is_organizer": bool(organizer.get("self")),
        "location": location,
        "online_join_url": _meet_link(ev),
        "organizer": {
            "email": organizer.get("email"),
            "name": organizer.get("displayName"),
        },
        "response": _self_response(ev),
        "start": start,
        "subject": ev.get("summary"),
        "timezone": str(display_tz),
    }


def _google_dt_to_iso(raw: Any, display_tz: ZoneInfo) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if "T" not in text:
        day = date.fromisoformat(text)
        dt = datetime(day.year, day.month, day.day, tzinfo=display_tz)
        return dt.isoformat()
    if text.endswith("Z"):
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=display_tz)
    return dt.astimezone(display_tz).isoformat()


def _google_updated_bounds(
    existing: Mapping[str, Any],
    start_raw: str | None,
    end_raw: str | None,
    duration: str | None,
    all_day: bool | None,
    tz: ZoneInfo,
) -> tuple[datetime, datetime, bool]:
    """New (start, end, is_all_day) for a Google time edit, filling from ``existing``."""
    ex_start = existing.get("start") or {}
    ex_end = existing.get("end") or {}
    was_all_day = "date" in ex_start and "dateTime" not in ex_start
    target_all_day = was_all_day if all_day is None else all_day
    old_start = _google_dt_to_iso(ex_start.get("dateTime") or ex_start.get("date"), tz)
    old_end = _google_dt_to_iso(ex_end.get("dateTime") or ex_end.get("date"), tz)
    prev_start = datetime.fromisoformat(old_start) if old_start else datetime.now(tz)
    prev_end = datetime.fromisoformat(old_end) if old_end else prev_start

    if target_all_day:
        if start_raw is not None and "T" in start_raw:
            raise ValueError("--all-day needs a date --start (YYYY-MM-DD), not a time")
        first = (
            date.fromisoformat(start_raw.strip()) if start_raw is not None else prev_start.date()
        )
        old_span_days = (prev_end.date() - prev_start.date()).days if was_all_day else None
        days = _all_day_span_days(first, end_raw, duration, old_span_days)
        start_dt = datetime.combine(first, time(), tzinfo=tz)
        return start_dt, start_dt + timedelta(days=days), True

    # A bare YYYY-MM-DD start/end on a timed event means a forgotten --all-day;
    # reject it rather than silently booking a 00:00 meeting (matches create).
    if not was_all_day:
        if start_raw is not None:
            reject_date_only_start(start_raw)
        if end_raw is not None and "T" not in end_raw.strip():
            raise ValueError(
                "a date-only --end needs --all-day; pass a time (e.g. 2026-12-24T17:00)"
            )
    start_dt = parse_local_datetime(start_raw, tz) if start_raw is not None else prev_start
    if end_raw is not None:
        end_dt = parse_local_datetime(end_raw, tz)
    elif duration is not None:
        end_dt = (start_dt.astimezone(UTC) + parse_duration(duration)).astimezone(tz)
    elif not was_all_day:
        end_dt = (start_dt.astimezone(UTC) + (prev_end - prev_start)).astimezone(tz)
    else:
        # all-day -> timed with no new length given: a default-length meeting,
        # not a multi-day block from the old all-day span.
        end_dt = (start_dt.astimezone(UTC) + parse_duration(_DEFAULT_DURATION)).astimezone(tz)
    if end_dt <= start_dt:
        raise ValueError("event end must be after start")
    return start_dt, end_dt, False


def _meet_link(ev: Mapping[str, Any]) -> str | None:
    """Meet URL for the event: hangoutLink, else a video entry point.

    A freshly patched event carries conferenceData before hangoutLink catches up,
    so reading only the latter would report "no link" right after attaching one.
    """
    direct = ev.get("hangoutLink")
    if direct:
        return str(direct)
    conference = ev.get("conferenceData") or {}
    for entry in conference.get("entryPoints") or []:
        if (
            isinstance(entry, Mapping)
            and entry.get("entryPointType") == "video"
            and entry.get("uri")
        ):
            return str(entry["uri"])
    return None


def _self_attendee(ev: Mapping[str, Any]) -> dict[str, Any] | None:
    for attendee in ev.get("attendees") or []:
        if isinstance(attendee, dict) and attendee.get("self"):
            return attendee
    return None


def _self_response(ev: Mapping[str, Any]) -> str | None:
    """The signed-in user's response, in the Microsoft vocabulary.

    ``calendar accept --today-pending`` filters on this via ``_needs_accept``; a
    hardcoded None there would read as "never responded" and accept every event
    on the day, so the mapping has to be real.
    """
    attendee = _self_attendee(ev)
    if attendee is None:
        return None
    return _RESPONSE_BY_GOOGLE_STATUS.get(str(attendee.get("responseStatus") or ""))


def _parse_clock(raw: str, *, flag: str) -> time:
    parts = raw.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"{flag} times must look like HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError(f"{flag} times must look like HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"{flag} times must look like HH:MM")
    return time(hour=hour, minute=minute, second=second)


def _parse_day_window(raw: str) -> tuple[time, time]:
    text = raw.strip()
    if "-" not in text:
        raise ValueError("--window must look like HH:MM-HH:MM")
    left, right = text.split("-", 1)
    start = _parse_clock(left.strip(), flag="--window")
    end = _parse_clock(right.strip(), flag="--window")
    if end <= start:
        raise ValueError("--window end must be after start")
    return start, end


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def _start_date(ev: Mapping[str, Any]) -> date | None:
    """Calendar date of the event's start (``dateTime`` or all-day ``date``)."""
    start = ev.get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _rrule_to_payload(
    recurrence: list[str] | None, display_tz: ZoneInfo, start: date | None = None
) -> dict[str, Any] | None:
    """First ``RRULE:`` line of a Google ``recurrence`` -> the ``calendar create`` shape.

    ``start`` is the event's own start date. A monthly/weekly RRULE this tool
    writes omits ``BYMONTHDAY`` / ``BYDAY`` when the value equals the RFC 5545
    DTSTART default, so the readback recovers ``day_of_month`` / ``days`` from the
    start to match both ``recurrence_payload`` and the Graph pattern mapping.
    """
    for line in recurrence or []:
        if not str(line).upper().startswith("RRULE:"):
            continue
        parts = dict(token.split("=", 1) for token in str(line)[6:].split(";") if "=" in token)
        parts = {key.upper(): value for key, value in parts.items()}
        freq = _RRULE_FREQ.get(parts.get("FREQ", "").upper())
        if freq is None:
            return {"freq": "other", "raw": str(line)}
        # The normalized schema represents only FREQ/INTERVAL/COUNT/UNTIL plus a
        # weekly BYDAY and a monthly BYMONTHDAY. Any other selector (BYSETPOS,
        # BYYEARDAY, a BYDAY on a non-weekly rule, a BYMONTHDAY on a non-monthly
        # rule, …) means an ordinal/positional pattern it cannot express -> "other".
        recognized = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST"}
        if freq == "weekly":
            recognized.add("BYDAY")
        if freq == "monthly":
            recognized.add("BYMONTHDAY")
        if any(key not in recognized for key in parts):
            return {"freq": "other", "raw": str(line)}
        by_day = [d.strip().upper() for d in parts.get("BYDAY", "").split(",") if d.strip()]
        # A prefixed weekday ("2WE", "-1FR") is an ordinal the schema cannot hold.
        if by_day and any(code not in _RRULE_WEEKDAY_CODES for code in by_day):
            return {"freq": "other", "raw": str(line)}
        by_month_day = parts.get("BYMONTHDAY", "")
        if by_month_day and not by_month_day.isdigit():
            return {"freq": "other", "raw": str(line)}
        payload: dict[str, Any] = {"freq": freq, "interval": int(parts.get("INTERVAL") or 1)}
        if freq == "weekly":
            codes = by_day or ([_RRULE_WEEKDAYS[start.weekday()]] if start else [])
            if codes:
                payload["days"] = [code.lower() for code in codes]
        if freq == "monthly":
            day = int(by_month_day) if by_month_day else (start.day if start else None)
            if day is not None:
                payload["day_of_month"] = day
        if "COUNT" in parts:
            payload["count"] = int(parts["COUNT"])
        elif "UNTIL" in parts:
            payload["until"] = _until_local_date(parts["UNTIL"], display_tz).isoformat()
        else:
            payload["ends"] = "never"
        return payload
    return None


def _until_local_date(raw: str, display_tz: ZoneInfo) -> date:
    """Local calendar date of an RRULE ``UNTIL`` value.

    ``recurrence_rrule`` stores UNTIL as the UTC end-of-day of the user's
    ``--until`` date, so a bare ``raw[:8]`` slice reports one day late in
    negative-offset zones; convert the timestamp back to ``display_tz`` first.
    """
    text = raw.strip()
    ymd = date.fromisoformat(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
    if "T" not in text:
        return ymd
    clock = text[9:].replace("Z", "")
    try:
        moment = datetime.combine(ymd, time.fromisoformat(clock[:8] or "00:00:00"), tzinfo=UTC)
    except ValueError:
        return ymd
    return moment.astimezone(display_tz).date()


def _schedule_error_message(entry: dict[str, Any]) -> str | None:
    errors = entry.get("errors") or []
    if not errors:
        return None
    parts: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        reason = str(err.get("reason") or "").strip()
        domain = str(err.get("domain") or "").strip()
        if reason and domain:
            parts.append(f"{domain}/{reason}")
        elif reason:
            parts.append(reason)
    return "; ".join(parts) if parts else "freebusy error"


def _schedule_to_dict(
    email: str,
    entry: dict[str, Any],
    display_tz: ZoneInfo,
) -> dict[str, Any]:
    busy_raw = entry.get("busy") or []
    return {
        "availability_view": None,
        "busy": [
            _busy_slot_to_dict(slot, display_tz) for slot in busy_raw if isinstance(slot, dict)
        ],
        "error": _schedule_error_message(entry),
        "schedule": email,
        "timezone": None,
        "working_hours": None,
    }
