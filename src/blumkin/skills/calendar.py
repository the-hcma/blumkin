"""Calendar skills."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from msgraph.generated.users.item.calendar.calendar_view.calendar_view_request_builder import (
    CalendarViewRequestBuilder,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config


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
    events = [_event_to_dict(ev, tz) for ev in items]
    return {
        "date": target.isoformat(),
        "items": events,
        "timezone": str(tz),
    }


def format_today_human(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Calendar ({payload['date']}, {payload['timezone']}): {len(payload['items'])} event(s)"
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
    # Graph calendarView often returns UTC-naive or with Z; normalize for display.
    if raw.endswith("Z"):
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(display_tz).isoformat()
