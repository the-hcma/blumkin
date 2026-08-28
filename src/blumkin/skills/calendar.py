"""Calendar skills."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from msgraph.generated.models.date_time_time_zone import DateTimeTimeZone
from msgraph.generated.users.item.calendar.calendar_view.calendar_view_request_builder import (
    CalendarViewRequestBuilder,
)
from msgraph.generated.users.item.calendar.get_schedule.get_schedule_post_request_body import (
    GetSchedulePostRequestBody,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config


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
    body = GetSchedulePostRequestBody(
        schedules=list(with_emails),
        start_time=_to_graph_dtz(start),
        end_time=_to_graph_dtz(end),
        availability_view_interval=30,
    )
    client = create_graph_client(cfg)
    response = await client.me.calendar.get_schedule.post(body)
    schedules = [] if response is None else (response.value or [])
    items = [
        _schedule_to_dict(entry, ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC"))
        for entry in schedules
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
    """Return mutual free starts for ``duration`` over ``[start, end)`` for everyone in ``--with``.

    Builds on ``calendar freebusy`` (union of busy). Does not create an event.
    """
    if duration <= timedelta(0):
        raise ValueError("--duration must be positive")
    if limit < 1:
        raise ValueError("--limit must be >= 1")
    tentative = treat_tentative.strip().lower()
    if tentative not in {"busy", "free"}:
        raise ValueError("--treat-tentative must be 'busy' or 'free'")
    window_bounds = _parse_day_window(window) if window is not None else None
    step_delta = step if step is not None else min(timedelta(minutes=15), duration)
    if step_delta <= timedelta(0):
        raise ValueError("--step must be positive")
    freebusy = await calendar_freebusy(with_emails=with_emails, start=start, end=end, config=config)
    _raise_if_schedule_errors(freebusy["items"], requested=with_emails)
    busy = _collect_busy_intervals(freebusy["items"], treat_tentative_busy=(tentative == "busy"))
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
    client = create_graph_client(cfg)
    query = CalendarViewRequestBuilder.CalendarViewRequestBuilderGetQueryParameters(
        start_date_time=start.isoformat(),
        end_date_time=end.isoformat(),
        orderby=["start/dateTime"],
        select=[
            "id",
            "subject",
            "start",
            "end",
            "location",
            "isAllDay",
            "organizer",
            "isOrganizer",
            "responseStatus",
            "onlineMeeting",
        ],
    )
    view = await client.me.calendar.calendar_view.get(request_config(query))
    items = [] if view is None else (view.value or [])
    events = [_event_to_dict(ev, display_tz) for ev in items]
    return {
        "end": end.isoformat(),
        "items": events,
        "start": start.isoformat(),
        "timezone": str(display_tz),
    }


def find_mutual_free_slots(
    *,
    busy: list[tuple[datetime, datetime]],
    range_start: datetime,
    range_end: datetime,
    duration: timedelta,
    window: tuple[time, time] | None,
    step: timedelta,
    limit: int,
) -> list[dict[str, str]]:
    """Scan ``[range_start, range_end)`` for ``duration`` gaps outside merged ``busy``."""
    if range_end <= range_start or duration <= timedelta(0) or limit < 1:
        return []
    merged = _merge_intervals(busy)
    slots: list[dict[str, str]] = []
    cursor = range_start
    while cursor + duration <= range_end and len(slots) < limit:
        meeting_end = cursor + duration
        if window is not None and not _fits_day_window(cursor, meeting_end, window):
            cursor = _advance_past_window(cursor, window, step, range_end)
            continue
        if not _overlaps_any(cursor, meeting_end, merged):
            slots.append({"end": meeting_end.isoformat(), "start": cursor.isoformat()})
        cursor += step
    return slots


def format_freebusy_human(payload: dict[str, Any]) -> list[str]:
    lines = [f"Free/busy ({payload['start']} → {payload['end']}, {payload['timezone']})"]
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        avail = item.get("availability_view") or ""
        lines.append(f"  • {_freebusy_schedule_label(item)}: view={avail!r}")
        for slot in item.get("busy") or []:
            lines.append(f"      busy {slot['start']} → {slot['end']} ({slot.get('status')})")
    return lines


def format_suggest_human(payload: dict[str, Any]) -> list[str]:
    duration = payload.get("duration_minutes")
    window = payload.get("window") or "all day"
    lines = [
        f"Suggest {duration}m slots ({payload['start']} → {payload['end']}, "
        f"{payload['timezone']}, window={window}): {len(payload.get('slots') or [])}"
    ]
    people = payload.get("with") or []
    if people:
        lines.append(f"  with: {', '.join(str(p) for p in people)}")
    slots = payload.get("slots") or []
    if not slots:
        lines.append("  (none)")
        return lines
    for slot in slots:
        lines.append(f"  • {slot.get('start')} → {slot.get('end')}")
    return lines


def format_today_human(payload: dict[str, Any]) -> list[str]:
    date_label = payload.get("date") or payload.get("start", "")[:10]
    lines = [f"Calendar ({date_label}, {payload['timezone']}): {len(payload['items'])} event(s)"]
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        when = "all day" if item.get("is_all_day") else f"{item['start']} → {item['end']}"
        subject = item.get("subject") or "(no subject)"
        loc = item.get("location") or ""
        suffix = f" @ {loc}" if loc else ""
        lines.append(f"  • {when} — {subject}{suffix}")
    return lines


def format_view_human(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Calendar view ({payload['start']} → {payload['end']}, {payload['timezone']}): "
        f"{len(payload['items'])} event(s)"
    ]
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        when = "all day" if item.get("is_all_day") else f"{item['start']} → {item['end']}"
        subject = item.get("subject") or "(no subject)"
        loc = item.get("location") or ""
        suffix = f" @ {loc}" if loc else ""
        lines.append(f"  • {when} — {subject}{suffix}")
    return lines


def parse_local_datetime(raw: str, tz: ZoneInfo) -> datetime:
    """Parse ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM[:SS]`` in ``tz`` (no Z → local)."""
    text = raw.strip()
    if text.endswith("Z"):
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(tz)
    if "T" in text:
        dt = datetime.fromisoformat(text)
    else:
        day = date.fromisoformat(text)
        dt = datetime(day.year, day.month, day.day)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


# Windows zone names, which Graph returns for mailbox defaults alongside IANA names.
# Generated from the CLDR windowsZones.xml default (territory="001") mappings; keys are
# casefolded. Unlisted names fall back to UTC rather than raising.
_WINDOWS_TZ_ALIASES = {
    "afghanistan standard time": "Asia/Kabul",
    "alaskan standard time": "America/Anchorage",
    "aleutian standard time": "America/Adak",
    "altai standard time": "Asia/Barnaul",
    "arab standard time": "Asia/Riyadh",
    "arabian standard time": "Asia/Dubai",
    "arabic standard time": "Asia/Baghdad",
    "argentina standard time": "America/Buenos_Aires",
    "astrakhan standard time": "Europe/Astrakhan",
    "atlantic standard time": "America/Halifax",
    "aus central standard time": "Australia/Darwin",
    "aus central w. standard time": "Australia/Eucla",
    "aus eastern standard time": "Australia/Sydney",
    "azerbaijan standard time": "Asia/Baku",
    "azores standard time": "Atlantic/Azores",
    "bahia standard time": "America/Bahia",
    "bangladesh standard time": "Asia/Dhaka",
    "belarus standard time": "Europe/Minsk",
    "bougainville standard time": "Pacific/Bougainville",
    "canada central standard time": "America/Regina",
    "cape verde standard time": "Atlantic/Cape_Verde",
    "caucasus standard time": "Asia/Yerevan",
    "cen. australia standard time": "Australia/Adelaide",
    "central america standard time": "America/Guatemala",
    "central asia standard time": "Asia/Bishkek",
    "central brazilian standard time": "America/Cuiaba",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "central pacific standard time": "Pacific/Guadalcanal",
    "central standard time": "America/Chicago",
    "central standard time (mexico)": "America/Mexico_City",
    "chatham islands standard time": "Pacific/Chatham",
    "china standard time": "Asia/Shanghai",
    "cuba standard time": "America/Havana",
    "dateline standard time": "Etc/GMT+12",
    "e. africa standard time": "Africa/Nairobi",
    "e. australia standard time": "Australia/Brisbane",
    "e. europe standard time": "Europe/Chisinau",
    "e. south america standard time": "America/Sao_Paulo",
    "easter island standard time": "Pacific/Easter",
    "eastern standard time": "America/New_York",
    "eastern standard time (mexico)": "America/Cancun",
    "egypt standard time": "Africa/Cairo",
    "ekaterinburg standard time": "Asia/Yekaterinburg",
    "fiji standard time": "Pacific/Fiji",
    "fle standard time": "Europe/Kiev",
    "georgian standard time": "Asia/Tbilisi",
    "gmt standard time": "Europe/London",
    "greenland standard time": "America/Godthab",
    "greenwich standard time": "Atlantic/Reykjavik",
    "gtb standard time": "Europe/Bucharest",
    "haiti standard time": "America/Port-au-Prince",
    "hawaiian standard time": "Pacific/Honolulu",
    "india standard time": "Asia/Calcutta",
    "iran standard time": "Asia/Tehran",
    "israel standard time": "Asia/Jerusalem",
    "jordan standard time": "Asia/Amman",
    "kaliningrad standard time": "Europe/Kaliningrad",
    "korea standard time": "Asia/Seoul",
    "libya standard time": "Africa/Tripoli",
    "line islands standard time": "Pacific/Kiritimati",
    "lord howe standard time": "Australia/Lord_Howe",
    "magadan standard time": "Asia/Magadan",
    "magallanes standard time": "America/Punta_Arenas",
    "marquesas standard time": "Pacific/Marquesas",
    "mauritius standard time": "Indian/Mauritius",
    "middle east standard time": "Asia/Beirut",
    "montevideo standard time": "America/Montevideo",
    "morocco standard time": "Africa/Casablanca",
    "mountain standard time": "America/Denver",
    "mountain standard time (mexico)": "America/Mazatlan",
    "myanmar standard time": "Asia/Rangoon",
    "n. central asia standard time": "Asia/Novosibirsk",
    "namibia standard time": "Africa/Windhoek",
    "nepal standard time": "Asia/Katmandu",
    "new zealand standard time": "Pacific/Auckland",
    "newfoundland standard time": "America/St_Johns",
    "norfolk standard time": "Pacific/Norfolk",
    "north asia east standard time": "Asia/Irkutsk",
    "north asia standard time": "Asia/Krasnoyarsk",
    "north korea standard time": "Asia/Pyongyang",
    "omsk standard time": "Asia/Omsk",
    "pacific sa standard time": "America/Santiago",
    "pacific standard time": "America/Los_Angeles",
    "pacific standard time (mexico)": "America/Tijuana",
    "pakistan standard time": "Asia/Karachi",
    "paraguay standard time": "America/Asuncion",
    "qyzylorda standard time": "Asia/Qyzylorda",
    "romance standard time": "Europe/Paris",
    "russia time zone 10": "Asia/Srednekolymsk",
    "russia time zone 11": "Asia/Kamchatka",
    "russia time zone 3": "Europe/Samara",
    "russian standard time": "Europe/Moscow",
    "sa eastern standard time": "America/Cayenne",
    "sa pacific standard time": "America/Bogota",
    "sa western standard time": "America/La_Paz",
    "saint pierre standard time": "America/Miquelon",
    "sakhalin standard time": "Asia/Sakhalin",
    "samoa standard time": "Pacific/Apia",
    "sao tome standard time": "Africa/Sao_Tome",
    "saratov standard time": "Europe/Saratov",
    "se asia standard time": "Asia/Bangkok",
    "singapore standard time": "Asia/Singapore",
    "south africa standard time": "Africa/Johannesburg",
    "south sudan standard time": "Africa/Juba",
    "sri lanka standard time": "Asia/Colombo",
    "sudan standard time": "Africa/Khartoum",
    "syria standard time": "Asia/Damascus",
    "taipei standard time": "Asia/Taipei",
    "tasmania standard time": "Australia/Hobart",
    "tocantins standard time": "America/Araguaina",
    "tokyo standard time": "Asia/Tokyo",
    "tomsk standard time": "Asia/Tomsk",
    "tonga standard time": "Pacific/Tongatapu",
    "transbaikal standard time": "Asia/Chita",
    "turkey standard time": "Europe/Istanbul",
    "turks and caicos standard time": "America/Grand_Turk",
    "ulaanbaatar standard time": "Asia/Ulaanbaatar",
    "us eastern standard time": "America/Indianapolis",
    "us mountain standard time": "America/Phoenix",
    "utc": "Etc/UTC",
    "utc+12": "Etc/GMT-12",
    "utc+13": "Etc/GMT-13",
    "utc-02": "Etc/GMT+2",
    "utc-08": "Etc/GMT+8",
    "utc-09": "Etc/GMT+9",
    "utc-11": "Etc/GMT+11",
    "venezuela standard time": "America/Caracas",
    "vladivostok standard time": "Asia/Vladivostok",
    "volgograd standard time": "Europe/Volgograd",
    "w. australia standard time": "Australia/Perth",
    "w. central africa standard time": "Africa/Lagos",
    "w. europe standard time": "Europe/Berlin",
    "w. mongolia standard time": "Asia/Hovd",
    "west asia standard time": "Asia/Tashkent",
    "west bank standard time": "Asia/Hebron",
    "west pacific standard time": "Pacific/Port_Moresby",
    "yakutsk standard time": "Asia/Yakutsk",
    "yukon standard time": "America/Whitehorse",
}


def _advance_past_window(
    cursor: datetime,
    window: tuple[time, time],
    _step: timedelta,
    range_end: datetime,
) -> datetime:
    """Move ``cursor`` forward when the candidate cannot fit in today's window."""
    window_start, _window_end = window
    day = cursor.date()
    day_start = datetime.combine(day, window_start, tzinfo=cursor.tzinfo)
    if cursor < day_start:
        return day_start
    tomorrow_open = datetime.combine(day + timedelta(days=1), window_start, tzinfo=cursor.tzinfo)
    if tomorrow_open < range_end:
        return tomorrow_open
    return range_end


def _busy_slot_to_dict(item: Any, display_tz: ZoneInfo) -> dict[str, Any]:
    status = None
    if getattr(item, "status", None) is not None:
        status = str(item.status)
    return {
        "end": _graph_dt_to_iso(item.end, display_tz),
        "start": _graph_dt_to_iso(item.start, display_tz),
        "status": status,
    }


def _collect_busy_intervals(
    items: list[dict[str, Any]],
    *,
    treat_tentative_busy: bool,
) -> list[tuple[datetime, datetime]]:
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


def _event_to_dict(ev: Any, display_tz: ZoneInfo) -> dict[str, Any]:
    start = _graph_dt_to_iso(ev.start, display_tz)
    end = _graph_dt_to_iso(ev.end, display_tz)
    organizer_name = None
    organizer_email = None
    if ev.organizer and ev.organizer.email_address:
        organizer_name = ev.organizer.email_address.name
        organizer_email = ev.organizer.email_address.address
    location = None
    if ev.location and ev.location.display_name:
        location = ev.location.display_name
    response = None
    if ev.response_status and ev.response_status.response:
        response = str(ev.response_status.response)
    online = None
    if ev.online_meeting and getattr(ev.online_meeting, "join_url", None):
        online = ev.online_meeting.join_url
    return {
        "end": end,
        "id": ev.id,
        "is_all_day": bool(ev.is_all_day),
        "is_organizer": bool(ev.is_organizer),
        "location": location,
        "online_join_url": online,
        "organizer": {"email": organizer_email, "name": organizer_name},
        "response": response,
        "start": start,
        "subject": ev.subject,
        "timezone": str(display_tz),
    }


def _fits_day_window(start: datetime, end: datetime, window: tuple[time, time]) -> bool:
    if start.date() != end.date():
        return False
    window_start, window_end = window
    return (
        start.timetz().replace(tzinfo=None) >= window_start
        and end.timetz().replace(tzinfo=None) <= window_end
    )


def _format_time_of_day(value: Any) -> str | None:
    """Normalize Graph TimeOfDay (``datetime.time`` or ``HH:mm:ss…`` string) to ``HH:MM``."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    text = str(value).strip()
    if not text:
        return None
    # Edm.TimeOfDay often arrives as "09:00:00.0000000"
    return text[:5]


def _freebusy_schedule_label(item: dict[str, Any]) -> str:
    """Human label: ``email (IANA, working HH:MM-HH:MM)`` when hours are known."""
    schedule = item.get("schedule") or "(unknown)"
    hours = item.get("working_hours") or {}
    tz = item.get("timezone") or hours.get("timezone")
    start = hours.get("start")
    end = hours.get("end")
    if tz and start and end:
        return f"{schedule} ({tz}, working {start}-{end})"
    if tz:
        return f"{schedule} ({tz})"
    return str(schedule)


def _graph_dt_to_iso(value: Any, display_tz: ZoneInfo) -> str | None:
    if value is None or not getattr(value, "date_time", None):
        return None
    raw = str(value.date_time)
    if raw.endswith("Z"):
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            # calendarView sends a Prefer: outlook.timezone header and omits timeZone,
            # so UTC is right there. POST /me/events has no such header: it echoes a
            # naive dateTime alongside the zone the event was created in, and reading
            # that as UTC shifts the reported time by the local offset (issue #46).
            source_tz = _resolve_tz(getattr(value, "time_zone", None))
            dt = dt.replace(tzinfo=source_tz or ZoneInfo("UTC"))
    return dt.astimezone(display_tz).isoformat()


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _overlaps_any(
    start: datetime,
    end: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> bool:
    for busy_start, busy_end in intervals:
        if start < busy_end and end > busy_start:
            return True
    return False


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


def _raise_if_schedule_errors(items: list[dict[str, Any]], *, requested: list[str]) -> None:
    """Fail closed when getSchedule could not resolve a requested mailbox."""
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


def _resolve_tz(name: Any) -> ZoneInfo | None:
    """Best-effort ``ZoneInfo`` from a Graph ``timeZone`` label (IANA or Windows name).

    Returns ``None`` for absent or unrecognized zones so callers can fall back to UTC
    rather than failing a read on an unmapped label.
    """
    if name is None:
        return None
    label = str(name).strip()
    if not label:
        return None
    if label.casefold() in {"tzone://microsoft/utc", "utc"}:
        return ZoneInfo("UTC")
    for candidate in (label, _WINDOWS_TZ_ALIASES.get(label.casefold())):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except ValueError, ZoneInfoNotFoundError:
            continue
    return None


def _schedule_error_message(entry: Any) -> str | None:
    err = getattr(entry, "error", None)
    if err is None:
        err = getattr(entry, "free_busy_error", None)
    if err is None:
        return None
    message = getattr(err, "message", None)
    if message:
        return str(message).strip() or None
    text = str(err).strip()
    return text or None


def _schedule_to_dict(entry: Any, display_tz: ZoneInfo) -> dict[str, Any]:
    busy_items = entry.schedule_items or []
    hours = _working_hours_to_dict(getattr(entry, "working_hours", None))
    timezone = hours.get("timezone") if hours else None
    error = _schedule_error_message(entry)
    return {
        "availability_view": entry.availability_view,
        "busy": [_busy_slot_to_dict(item, display_tz) for item in busy_items],
        "error": error,
        "schedule": entry.schedule_id,
        "timezone": timezone,
        "working_hours": hours,
    }


def _status_is_busy(status: Any, *, treat_tentative_busy: bool) -> bool:
    label = str(status or "").split(".")[-1].casefold()
    if label in {"busy", "oof", "workingelsewhere"}:
        return True
    if label == "tentative":
        return treat_tentative_busy
    return False


def _to_graph_dtz(value: datetime) -> DateTimeTimeZone:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    tz = value.tzinfo
    tz_name = tz.key if isinstance(tz, ZoneInfo) else str(tz)
    return DateTimeTimeZone(
        date_time=value.replace(tzinfo=None).isoformat(timespec="seconds"),
        time_zone=tz_name,
    )


def _working_hours_to_dict(hours: Any) -> dict[str, Any] | None:
    """Map Graph ``workingHours`` (from getSchedule) into JSON-friendly fields.

    Timezone prefers a resolved IANA name via ``_resolve_tz``; otherwise the raw
    Windows / custom label. Missing hours stay ``None`` (not an empty object).
    """
    if hours is None:
        return None
    days_raw = getattr(hours, "days_of_week", None) or []
    days = sorted({str(day).split(".")[-1].lower() for day in days_raw if day is not None})
    start_s = _format_time_of_day(getattr(hours, "start_time", None))
    end_s = _format_time_of_day(getattr(hours, "end_time", None))
    tz_obj = getattr(hours, "time_zone", None)
    tz_label = getattr(tz_obj, "name", None) if tz_obj is not None else None
    if isinstance(tz_label, str):
        tz_label = tz_label.strip() or None
    else:
        tz_label = None
    resolved = _resolve_tz(tz_label)
    timezone = resolved.key if resolved is not None else tz_label
    if not days and start_s is None and end_s is None and timezone is None:
        return None
    return {
        "days_of_week": days,
        "end": end_s,
        "start": start_s,
        "timezone": timezone,
    }
