"""Online meeting skills (get + transcription flags)."""

from __future__ import annotations

from typing import Any

from msgraph.generated.models.online_meeting import OnlineMeeting
from msgraph.generated.users.item.online_meetings.online_meetings_request_builder import (
    OnlineMeetingsRequestBuilder,
)

from blumkin.config import BlumkinConfig, load_config
from blumkin.graph import create_graph_client, request_config
from blumkin.output import sanitize_terminal


async def meeting_get(
    *,
    event_id: str,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Resolve calendar event → online meeting and return transcription-related flags."""
    cfg = config or load_config()
    client = create_graph_client(cfg)
    event, meeting = await _event_and_online_meeting(client, event_id=event_id)
    return {
        "event": _event_summary(event),
        "meeting": _meeting_to_dict(meeting),
    }


async def meeting_transcription(
    *,
    event_id: str,
    enable: bool = False,
    config: BlumkinConfig | None = None,
) -> dict[str, Any]:
    """Show or enable ``allowTranscription`` on the online meeting for an event."""
    cfg = config or load_config()
    client = create_graph_client(cfg)
    event, meeting = await _event_and_online_meeting(client, event_id=event_id)
    meeting_id = meeting.id
    if not meeting_id:
        raise RuntimeError("online meeting is missing an id")
    if enable:
        patched = await client.me.online_meetings.by_online_meeting_id(meeting_id).patch(
            OnlineMeeting(allow_transcription=True)
        )
        if patched is None:
            patched = await client.me.online_meetings.by_online_meeting_id(meeting_id).get()
        if patched is None:
            raise RuntimeError("online meeting patch returned empty response")
        meeting = patched
    return {
        "enabled": bool(getattr(meeting, "allow_transcription", False)),
        "event": _event_summary(event),
        "meeting": _meeting_to_dict(meeting),
        "mutated": bool(enable),
    }


def format_get_human(payload: dict[str, Any]) -> list[str]:
    event = payload.get("event") or {}
    meeting = payload.get("meeting") or {}
    subject = sanitize_terminal(str(event.get("subject") or ""))
    return [
        f"Meeting for event {event.get('id')!r}: {subject}",
        f"  online_meeting_id={meeting.get('id')}",
        f"  join_url={sanitize_terminal(str(meeting.get('join_url') or ''))}",
        f"  allow_transcription={meeting.get('allow_transcription')}",
        f"  allow_recording={meeting.get('allow_recording')}",
        f"  record_automatically={meeting.get('record_automatically')}",
    ]


def format_transcription_human(payload: dict[str, Any]) -> list[str]:
    meeting = payload.get("meeting") or {}
    verb = "Enabled" if payload.get("mutated") else "Current"
    return [
        f"{verb} transcription flags for online meeting {meeting.get('id')!r}:",
        f"  allow_transcription={meeting.get('allow_transcription')}",
        f"  allow_recording={meeting.get('allow_recording')}",
        f"  record_automatically={meeting.get('record_automatically')}",
    ]


async def _event_and_online_meeting(client: Any, *, event_id: str) -> tuple[Any, Any]:
    eid = event_id.strip()
    if not eid:
        raise ValueError("--event-id is required")
    event = await client.me.events.by_event_id(eid).get()
    if event is None:
        raise LookupError(f"event not found: {eid}")
    join_url = None
    if event.online_meeting is not None:
        join_url = getattr(event.online_meeting, "join_url", None)
    if not join_url:
        raise LookupError(f"event is not a Teams online meeting: {eid}")
    meeting = await _online_meeting_by_join_url(client, join_url=str(join_url))
    return event, meeting


def _event_summary(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "is_online_meeting": bool(getattr(event, "is_online_meeting", False)),
        "subject": event.subject,
    }


def _meeting_to_dict(meeting: Any) -> dict[str, Any]:
    return {
        "allow_recording": getattr(meeting, "allow_recording", None),
        "allow_transcription": getattr(meeting, "allow_transcription", None),
        "id": meeting.id,
        "join_url": getattr(meeting, "join_web_url", None),
        "record_automatically": getattr(meeting, "record_automatically", None),
        "subject": getattr(meeting, "subject", None),
    }


async def _online_meeting_by_join_url(client: Any, *, join_url: str) -> Any:
    escaped = join_url.replace("'", "''")
    query = OnlineMeetingsRequestBuilder.OnlineMeetingsRequestBuilderGetQueryParameters(
        filter=f"JoinWebUrl eq '{escaped}'",
    )
    page = await client.me.online_meetings.get(request_config(query))
    meetings = list(page.value or []) if page is not None else []
    if not meetings:
        raise LookupError(
            "online meeting not found for event join URL "
            "(GET /me/onlineMeetings only returns meetings you organize; "
            "attendee-only calendar events cannot be resolved this way)"
        )
    return meetings[0]
