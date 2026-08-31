"""Calendar write skills (accept, create, cancel, update)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from msgraph.generated.models.attendee import Attendee
from msgraph.generated.models.attendee_type import AttendeeType
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.event import Event
from msgraph.generated.models.online_meeting_provider_type import OnlineMeetingProviderType
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

_DEFAULT_DURATION = "30m"
_DURATION_RE = re.compile(
    r"^(\d+)\s*(m|min|mins|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$", re.I
)
# Google caps a reminder lead time at four weeks; keep both providers to that bound.
_MAX_REMINDER_MINUTES = 40320


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
    end = start + parse_duration(duration or _DEFAULT_DURATION)
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
    return {"event": _event_to_dict(created, tz)}


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
    return [f"Accepted {payload.get('count', len(ids))} event(s):"] + [f"  • {eid}" for eid in ids]


def format_cancel_human(payload: dict[str, Any]) -> list[str]:
    return [f"Cancelled event {payload.get('cancelled')!r}"]


def format_create_human(payload: dict[str, Any]) -> list[str]:
    event = payload.get("event") or {}
    subject = sanitize_terminal(str(event.get("subject") or "(no subject)"))
    when = f"{event.get('start')} → {event.get('end')}"
    lines = [f"Created: {subject!r} ({when})"]
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


def _needs_accept(item: dict[str, Any]) -> bool:
    if item.get("is_organizer"):
        return False
    response = (item.get("response") or "").lower()
    return "notresponded" in response or response in {"", "none"}
