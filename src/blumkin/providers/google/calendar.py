"""Google Calendar read skills (skill-shaped payloads)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from blumkin.config import BlumkinConfig, load_config
from blumkin.providers.google_auth import get_credentials
from blumkin.providers.google_http import build_api_service, execute
from blumkin.skills.calendar import find_mutual_free_slots
from blumkin.skills.freebusy_suggest import collect_busy_intervals, raise_if_schedule_errors


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


def _busy_slot_to_dict(slot: dict[str, Any], display_tz: ZoneInfo) -> dict[str, Any]:
    return {
        "end": _google_dt_to_iso(slot.get("end"), display_tz),
        "start": _google_dt_to_iso(slot.get("start"), display_tz),
        "status": "busy",
    }


def _calendar_service(cfg: BlumkinConfig) -> Any:
    creds = get_credentials(cfg, allow_interactive=False)
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
        "online_join_url": (ev.get("hangoutLink") or None),
        "organizer": {
            "email": organizer.get("email"),
            "name": organizer.get("displayName"),
        },
        "response": None,
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
