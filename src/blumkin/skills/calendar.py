"""Calendar skills."""

from __future__ import annotations

from datetime import date, datetime, timedelta
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


def format_freebusy_human(payload: dict[str, Any]) -> list[str]:
    lines = [f"Free/busy ({payload['start']} → {payload['end']}, {payload['timezone']})"]
    if not payload["items"]:
        lines.append("  (none)")
        return lines
    for item in payload["items"]:
        schedule = item.get("schedule") or "(unknown)"
        avail = item.get("availability_view") or ""
        lines.append(f"  • {schedule}: view={avail!r}")
        for slot in item.get("busy") or []:
            lines.append(f"      busy {slot['start']} → {slot['end']} ({slot.get('status')})")
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


# Graph returns Windows zone names (the mailbox default) as well as IANA names in
# DateTimeTimeZone.timeZone. Unlisted names fall back to UTC rather than raising.
_WINDOWS_TZ_ALIASES = {
    "alaskan standard time": "America/Anchorage",
    "arab standard time": "Asia/Riyadh",
    "arabian standard time": "Asia/Dubai",
    "argentina standard time": "America/Argentina/Buenos_Aires",
    "atlantic standard time": "America/Halifax",
    "aus eastern standard time": "Australia/Sydney",
    "azores standard time": "Atlantic/Azores",
    "bangladesh standard time": "Asia/Dhaka",
    "cape verde standard time": "Atlantic/Cape_Verde",
    "central america standard time": "America/Guatemala",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "central standard time": "America/Chicago",
    "central standard time (mexico)": "America/Mexico_City",
    "china standard time": "Asia/Shanghai",
    "e. australia standard time": "Australia/Brisbane",
    "e. south america standard time": "America/Sao_Paulo",
    "eastern standard time": "America/New_York",
    "egypt standard time": "Africa/Cairo",
    "fle standard time": "Europe/Kyiv",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "gtb standard time": "Europe/Bucharest",
    "hawaiian standard time": "Pacific/Honolulu",
    "india standard time": "Asia/Kolkata",
    "iran standard time": "Asia/Tehran",
    "israel standard time": "Asia/Jerusalem",
    "korea standard time": "Asia/Seoul",
    "morocco standard time": "Africa/Casablanca",
    "mountain standard time": "America/Denver",
    "myanmar standard time": "Asia/Yangon",
    "new zealand standard time": "Pacific/Auckland",
    "newfoundland standard time": "America/St_Johns",
    "pacific sa standard time": "America/Santiago",
    "pacific standard time": "America/Los_Angeles",
    "pakistan standard time": "Asia/Karachi",
    "romance standard time": "Europe/Paris",
    "russian standard time": "Europe/Moscow",
    "sa eastern standard time": "America/Cayenne",
    "se asia standard time": "Asia/Bangkok",
    "singapore standard time": "Asia/Singapore",
    "south africa standard time": "Africa/Johannesburg",
    "taipei standard time": "Asia/Taipei",
    "tokyo standard time": "Asia/Tokyo",
    "turkey standard time": "Europe/Istanbul",
    "us eastern standard time": "America/Indiana/Indianapolis",
    "us mountain standard time": "America/Phoenix",
    "w. australia standard time": "Australia/Perth",
    "w. europe standard time": "Europe/Berlin",
}


def _busy_slot_to_dict(item: Any, display_tz: ZoneInfo) -> dict[str, Any]:
    status = None
    if getattr(item, "status", None) is not None:
        status = str(item.status)
    return {
        "end": _graph_dt_to_iso(item.end, display_tz),
        "start": _graph_dt_to_iso(item.start, display_tz),
        "status": status,
    }


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


def _schedule_to_dict(entry: Any, display_tz: ZoneInfo) -> dict[str, Any]:
    busy_items = entry.schedule_items or []
    return {
        "availability_view": entry.availability_view,
        "busy": [_busy_slot_to_dict(item, display_tz) for item in busy_items],
        "schedule": entry.schedule_id,
        "working_hours": None,
    }


def _to_graph_dtz(value: datetime) -> DateTimeTimeZone:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    tz = value.tzinfo
    tz_name = tz.key if isinstance(tz, ZoneInfo) else str(tz)
    return DateTimeTimeZone(
        date_time=value.replace(tzinfo=None).isoformat(timespec="seconds"),
        time_zone=tz_name,
    )
