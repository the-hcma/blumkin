"""Google Calendar skills (reads plus ``create``), skill-shaped payloads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import CALENDAR_SCOPES, get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.calendar import find_mutual_free_slots, parse_local_datetime
from blumkin.skills.calendar_writes import (
    _DEFAULT_DURATION,
    _needs_accept,
    parse_duration,
    reminder_minutes_before_start,
)
from blumkin.skills.freebusy_suggest import collect_busy_intervals, raise_if_schedule_errors

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
    duration: str | None = None,
    remind_email: str | None = None,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    if not subject.strip():
        raise ValueError("--subject is required")
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    tz_key = tz.key if isinstance(tz, ZoneInfo) else str(tz)
    start = parse_local_datetime(start_raw, tz)
    # Add the duration in absolute time so an event spanning a DST transition keeps
    # its real length (wall-clock arithmetic would over- or under-count by an hour).
    end = (start.astimezone(UTC) + parse_duration(duration or _DEFAULT_DURATION)).astimezone(tz)
    body: dict[str, Any] = {
        "summary": subject.strip(),
        "start": {"dateTime": start.isoformat(timespec="seconds"), "timeZone": tz_key},
        "end": {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tz_key},
    }
    if with_emails:
        body["attendees"] = [{"email": email} for email in with_emails]
    if remind_email is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": reminder_minutes_before_start(remind_email)}
            ],
        }
    service = _calendar_service(cfg)
    created = execute(
        service.events().insert(
            calendarId="primary",
            body=body,
            sendUpdates="all" if with_emails else "none",
        ),
        # events.insert is a non-idempotent POST; a blind retry could double-book.
        num_retries=0,
    )
    return {"event": _event_to_dict(created, tz)}


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
    service = _calendar_service(cfg)
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
    teams: bool = True,
    tz_name: str | None = None,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Attach a Google Meet conference to an existing event.

    The Microsoft side calls this "Teams"; on Google the same ``--teams`` flag
    means a Meet link, requested through conferenceData rather than a separate
    online-meeting object.
    """
    eid = event_id.strip()
    if not eid:
        raise ValueError("--event-id is required")
    if not teams:
        raise ValueError(
            "calendar update currently only attaches a meeting; do not pass --no-teams"
        )
    cfg = config or load_config()
    tz = ZoneInfo(tz_name or cfg.default_tz)
    service = _calendar_service(cfg)
    body = {
        "conferenceData": {
            "createRequest": {
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
                "requestId": uuid4().hex,
            }
        }
    }
    updated = execute(
        service.events().patch(
            calendarId="primary",
            eventId=eid,
            body=body,
            conferenceDataVersion=1,
            sendUpdates="all",
        ),
        num_retries=0,
    )
    if not _meet_link(updated):
        # Meet provisions asynchronously: the PATCH response can carry
        # conferenceData.status "pending" with no entry points yet. Re-read once
        # before declaring failure, the same way the Graph path re-GETs.
        updated = execute(service.events().get(calendarId="primary", eventId=eid))
    if not _meet_link(updated):
        # Same contract as the Microsoft path: report a missing link rather than
        # returning an event that looks updated but has nothing to join.
        raise RuntimeError(
            f"Meet conference was not provisioned for event {eid!r} "
            "(no conferenceData entry point after PATCH); re-run `calendar update` "
            "on this event. Recreating will not help: `calendar create` does not "
            "attach a conference on Google."
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
    service = _calendar_service(cfg)
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


def _calendar_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False, required_scopes=CALENDAR_SCOPES)
    return build_api_service("calendar", "v3", creds=creds, config=cfg)


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
