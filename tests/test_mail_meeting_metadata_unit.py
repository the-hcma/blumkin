"""Meeting-invite metadata on mail reads (issue #277).

Covers both providers: Microsoft/Graph exposes `meetingMessageType` + a
`$expand`-ed `event` on eventMessage instances; Google/Gmail has no first-class
signal, so the fields are derived from the inline `text/calendar` MIME part
that Google Calendar attaches to invites/updates/cancellations/replies.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.date_time_time_zone import DateTimeTimeZone
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.event import Event
from msgraph.generated.models.event_message import EventMessage
from msgraph.generated.models.meeting_message_type import MeetingMessageType
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.generated.models.recipient import Recipient

from blumkin.providers.google import mail as google_mail
from blumkin.skills.mail import (
    format_get_human,
    format_inbox_human,
    format_list_human,
    format_search_human,
    format_thread_human,
    mail_get,
    mail_inbox,
    mail_thread,
)

# ---------------------------------------------------------------------------
# Microsoft / Graph
# ---------------------------------------------------------------------------


def test_mail_get_reports_meeting_metadata_for_an_event_message(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_event_message())

    message = asyncio.run(mail_get(message_id="msg-1"))["message"]

    assert message["is_meeting_message"] is True
    assert message["meeting_message_type"] == "meetingRequest"
    assert message["linked_event_id"] == "evt-1"
    assert message["organizer_email"] == "janelle@example.com"
    assert message["start"] == "2026-01-15T14:00:00.0000000Z"
    assert message["end"] == "2026-01-15T15:00:00.0000000Z"
    # Pin the actual request shape: deleting `meetingMessageType` from `$select`
    # or dropping/mistyping the `$expand` cast would keep the mocked assertions
    # above green while a live tenant silently stopped reporting these fields.
    sent_query = item.get.await_args_list[0].args[0].query_parameters
    assert "meetingMessageType" in sent_query.select
    assert sent_query.expand == [
        "Microsoft.Graph.EventMessage/Event($select=id,organizer,start,end)"
    ]


def test_mail_get_reports_a_non_utc_event_time_as_graph_sent_it(monkeypatch) -> None:
    """No `Prefer: outlook.timezone` header is sent, so Graph defaults to UTC —
    but if that ever changes upstream, a non-UTC zone is passed through as-is
    rather than silently mislabeled with a trailing `Z`.
    """
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    event_message = _event_message()
    event_message.event.start = DateTimeTimeZone(
        date_time="2026-01-15T09:00:00.0000000", time_zone="Eastern Standard Time"
    )
    item.get = AsyncMock(return_value=event_message)

    message = asyncio.run(mail_get(message_id="msg-1"))["message"]

    assert message["start"] == "2026-01-15T09:00:00.0000000"


def test_mail_get_reports_no_meeting_metadata_for_a_plain_message(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_plain_message())

    message = asyncio.run(mail_get(message_id="msg-1"))["message"]

    assert message["is_meeting_message"] is False
    assert message["meeting_message_type"] is None
    assert message["linked_event_id"] is None


def test_mail_get_falls_back_when_a_tenant_rejects_the_cast_expand(monkeypatch) -> None:
    """A 400 on the expand-carrying request must not fail the whole read.

    The retry drops `$expand`, so a message that would otherwise be a meeting
    request still comes back (with `linked_event_id` unresolved) instead of a
    ``mail get`` failure over metadata that is a nice-to-have.
    """
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    error = ODataError()
    error.response_status_code = 400
    error.error = MainError(code="invalidRequest", message="unsupported $expand")
    retried = _event_message()
    retried.event = None
    outcomes = iter([error, retried])
    # `query.expand` is cleared **in place** on the shared query object before
    # the retry, so asserting `await_args_list[i].args[0].query_parameters.expand`
    # after the fact would read the same, already-mutated object for both calls
    # regardless of what the retry actually sent — snapshot it at call time instead.
    seen_expands: list[Any] = []

    async def fake_get(config: Any) -> Any:
        expand = config.query_parameters.expand
        seen_expands.append(list(expand) if expand else None)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    item.get = AsyncMock(side_effect=fake_get)

    message = asyncio.run(mail_get(message_id="msg-1"))["message"]

    assert item.get.await_count == 2
    assert seen_expands == [
        ["Microsoft.Graph.EventMessage/Event($select=id,organizer,start,end)"],
        None,
    ]
    assert message["is_meeting_message"] is True
    assert message["meeting_message_type"] == "meetingRequest"
    assert message["linked_event_id"] is None


def test_mail_inbox_reports_meeting_metadata_for_a_list_item(monkeypatch) -> None:
    """The issue's own scenario: spotting an invite in an inbox listing, not `mail get`."""
    client = _client(monkeypatch)
    page = SimpleNamespace(value=[_event_message()], odata_next_link=None)
    client.me.messages.get = AsyncMock(return_value=page)

    payload = asyncio.run(mail_inbox(top=5))

    (item,) = payload["items"]
    assert item["is_meeting_message"] is True
    assert item["meeting_message_type"] == "meetingRequest"
    sent_query = client.me.messages.get.await_args_list[0].args[0].query_parameters
    assert "meetingMessageType" in sent_query.select


def test_mail_thread_reports_meeting_metadata_for_a_list_item(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.by_message_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(conversation_id="conv-1")
    )
    page = SimpleNamespace(value=[_event_message()], odata_next_link=None)
    client.me.messages.get = AsyncMock(return_value=page)

    payload = asyncio.run(mail_thread(message_id="msg-1"))

    (item,) = payload["items"]
    assert item["is_meeting_message"] is True
    assert item["meeting_message_type"] == "meetingRequest"
    sent_query = client.me.messages.get.await_args_list[0].args[0].query_parameters
    assert "meetingMessageType" in sent_query.select


def test_mail_thread_full_reports_organizer_and_times_from_the_expanded_event(
    monkeypatch,
) -> None:
    """`--full` re-fetches each message via `mail_get` (which expands the event);
    that resolved organizer/start/end must make it into the thread item, not just
    `body`/`body_type` — the list query alone has no `$expand` to source them from.
    """
    client = _client(monkeypatch)
    client.me.messages.by_message_id.return_value.get = AsyncMock(
        side_effect=[SimpleNamespace(conversation_id="conv-1"), _event_message()]
    )
    page = SimpleNamespace(value=[_event_message()], odata_next_link=None)
    client.me.messages.get = AsyncMock(return_value=page)

    payload = asyncio.run(mail_thread(message_id="msg-1", full=True))

    (item,) = payload["items"]
    assert item["is_meeting_message"] is True
    assert item["meeting_message_type"] == "meetingRequest"
    assert item["linked_event_id"] == "evt-1"
    assert item["organizer_email"] == "janelle@example.com"
    assert item["start"] == "2026-01-15T14:00:00.0000000Z"
    assert item["end"] == "2026-01-15T15:00:00.0000000Z"


def test_mail_inbox_reports_no_meeting_metadata_for_a_plain_list_item(monkeypatch) -> None:
    client = _client(monkeypatch)
    page = SimpleNamespace(value=[_plain_message()], odata_next_link=None)
    client.me.messages.get = AsyncMock(return_value=page)

    payload = asyncio.run(mail_inbox(top=5))

    (item,) = payload["items"]
    assert item["is_meeting_message"] is False
    assert item["meeting_message_type"] is None


# ---------------------------------------------------------------------------
# Human-formatted output (`--json` is not the only consumer)
# ---------------------------------------------------------------------------


def test_format_get_human_tags_a_meeting_request() -> None:
    payload = {
        "message": {
            "subject": "Sync",
            "is_meeting_message": True,
            "meeting_message_type": "meetingRequest",
        }
    }

    lines = format_get_human(payload)

    assert lines[0] == "Sync [meeting: request]"


def test_format_get_human_falls_back_to_a_generic_tag_for_an_unmapped_type() -> None:
    """Covers a null/unrecognized `meeting_message_type` (e.g. a Graph event message
    with a null enum, or a Gmail REPLY without a PARTSTAT) — still flagged as a
    meeting rather than silently dropped."""
    payload = {
        "message": {"subject": "Sync", "is_meeting_message": True, "meeting_message_type": None}
    }

    lines = format_get_human(payload)

    assert lines[0] == "Sync [meeting: invite]"


def test_format_get_human_has_no_tag_for_a_plain_message() -> None:
    payload = {"message": {"subject": "Sync", "is_meeting_message": False}}

    lines = format_get_human(payload)

    assert lines[0] == "Sync"


def _meeting_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "subject": "Sync",
        "from_name": "Rebecca",
        "received": "2026-01-01T00:00:00Z",
        "is_meeting_message": True,
        "meeting_message_type": "meetingAccepted",
    }
    item.update(overrides)
    return item


def test_format_inbox_human_tags_a_meeting_item() -> None:
    payload = {"top": 5, "items": [_meeting_item()], "orderby": None, "filters": {}}

    lines = format_inbox_human(payload)

    assert lines[-1].endswith("[meeting: accepted]")


def test_format_list_human_tags_a_meeting_item() -> None:
    payload = {"top": 5, "items": [_meeting_item()], "orderby": None, "filters": {}, "folder": None}

    lines = format_list_human(payload)

    assert lines[-1].endswith("[meeting: accepted]")


def test_format_search_human_tags_a_meeting_item() -> None:
    payload = {"query": "sync", "items": [_meeting_item(folder="inbox")]}

    lines = format_search_human(payload)

    assert lines[-1].endswith("[meeting: accepted]")


def test_format_thread_human_tags_a_meeting_item() -> None:
    payload = {"conversation_id": "c-1", "items": [_meeting_item()]}

    lines = format_thread_human(payload)

    assert lines[-1].endswith("[meeting: accepted]")


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
    return client


def _event_message() -> Any:
    msg: Any = EventMessage()
    msg.id = "msg-1"
    msg.subject = "Quarterly sync"
    msg.body = SimpleNamespace(content="hello", content_type=BodyType.Text)
    msg.body_preview = "hello"
    msg.conversation_id = "conv-1"
    msg.created_date_time = "2026-08-27T08:59Z"
    msg.has_attachments = False
    msg.is_read = True
    msg.is_draft = False
    msg.parent_folder_id = None
    msg.received_date_time = "2026-08-27T09:00Z"
    msg.sent_date_time = "2026-08-27T08:58Z"
    msg.to_recipients = []
    msg.meeting_message_type = MeetingMessageType.MeetingRequest
    msg.event = Event()
    msg.event.id = "evt-1"
    msg.event.organizer = Recipient(email_address=EmailAddress(address="janelle@example.com"))
    msg.event.start = DateTimeTimeZone(date_time="2026-01-15T14:00:00.0000000", time_zone="UTC")
    msg.event.end = DateTimeTimeZone(date_time="2026-01-15T15:00:00.0000000", time_zone="UTC")
    msg.from_ = SimpleNamespace(
        email_address=SimpleNamespace(address="organizer@example.com", name="Organizer")
    )
    return msg


def _plain_message() -> Any:
    return SimpleNamespace(
        id="msg-1",
        subject="Quarterly sync",
        body=SimpleNamespace(content="hello", content_type=BodyType.Text),
        body_preview="hello",
        conversation_id="conv-1",
        created_date_time="2026-08-27T08:59Z",
        has_attachments=False,
        importance=None,
        is_read=True,
        is_draft=False,
        parent_folder_id=None,
        received_date_time="2026-08-27T09:00Z",
        sent_date_time="2026-08-27T08:58Z",
        to_recipients=[],
        from_=SimpleNamespace(
            email_address=SimpleNamespace(address="rebecca@example.com", name="Rebecca Doe")
        ),
    )


# ---------------------------------------------------------------------------
# Google / Gmail
# ---------------------------------------------------------------------------

_GOOGLE_MAIL = "blumkin.providers.google.mail"


def test_google_mail_get_detects_a_meeting_request_ics_part() -> None:
    service = _service(_full_message_with_ics(method="REQUEST", uid="uid-123@google.com"))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["is_meeting_message"] is True
    assert message["meeting_message_type"] == "meetingRequest"
    assert message["ical_uid"] == "uid-123@google.com"


def test_google_mail_get_detects_a_cancellation_ics_part() -> None:
    service = _service(_full_message_with_ics(method="CANCEL", uid="uid-456@google.com"))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["meeting_message_type"] == "meetingCancelled"


def test_google_mail_get_maps_a_folded_reply_partstat_to_a_meeting_message_type() -> None:
    """RFC 5545 folds any line over 75 octets onto a continuation line.

    A real REPLY's ATTENDEE line (CUTYPE/ROLE/PARTSTAT/RSVP/CN/X-NUM-GUESTS
    params plus a mailto:) routinely lands past that limit, so this pins the
    fold-independent match rather than one that only happens to work on a
    short, single-line fixture.
    """
    folded_attendee = (
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=TRUE;\r\n"
        " CN=Rebecca Doe;X-NUM-GUESTS=0:mailto:rebecca@example.com\r\n"
    )
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REPLY\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:uid-999@google.com\r\n" + folded_attendee + "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["meeting_message_type"] == "meetingAccepted"


def test_google_mail_get_maps_a_reply_partstat_to_a_meeting_message_type() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REPLY\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:uid-789@google.com\r\n"
        'ATTENDEE;CN="Rebecca";PARTSTAT=DECLINED:mailto:rebecca@example.com\r\n'
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["meeting_message_type"] == "meetingDeclined"
    assert message["ical_uid"] == "uid-789@google.com"


def test_google_mail_get_maps_a_tentative_reply_partstat_to_a_meeting_message_type() -> None:
    """`TENTATIVE` is the one PARTSTAT->type mapping spelled unlike its Graph
    counterpart (`meetingTenativelyAccepted`, matching `_MEETING_TYPE_LABELS` in
    `skills/mail.py`) — pin it through `_meeting_fields_from_payload`, not just
    against a hand-built payload dict.
    """
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REPLY\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:uid-tentative@google.com\r\n"
        'ATTENDEE;CN="Rebecca";PARTSTAT=TENTATIVE:mailto:rebecca@example.com\r\n'
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["meeting_message_type"] == "meetingTenativelyAccepted"


def test_google_mail_get_reports_meeting_details_from_the_ics_body() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:uid-details@google.com\r\n"
        "ORGANIZER;CN=Janelle Doe:mailto:janelle@example.com\r\n"
        "DTSTART:20260115T140000Z\r\n"
        "DTEND:20260115T150000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["organizer_email"] == "janelle@example.com"
    assert message["start"] == "2026-01-15T14:00:00Z"
    assert message["end"] == "2026-01-15T15:00:00Z"


def test_google_mail_get_reports_a_tzid_qualified_start_in_utc() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:uid-tz@google.com\r\n"
        "DTSTART;TZID=America/New_York:20260115T090000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    # 09:00 America/New_York in January (EST, UTC-5) is 14:00 UTC.
    assert message["start"] == "2026-01-15T14:00:00Z"


def test_google_mail_get_scopes_dtstart_to_the_vevent_not_a_preceding_vtimezone() -> None:
    """A TZID-qualified DTSTART implies a VTIMEZONE block, which carries its own
    DTSTART for each DAYLIGHT/STANDARD rule and (per Google/Outlook convention)
    precedes the VEVENT — an unscoped search must not pick that line up instead
    of the real event's.
    """
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VTIMEZONE\r\n"
        "TZID:America/New_York\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "DTSTART:19700308T020000\r\n"
        "TZOFFSETFROM:-0500\r\n"
        "TZOFFSETTO:-0400\r\n"
        "END:DAYLIGHT\r\n"
        "BEGIN:STANDARD\r\n"
        "DTSTART:19701101T020000\r\n"
        "TZOFFSETFROM:-0400\r\n"
        "TZOFFSETTO:-0500\r\n"
        "END:STANDARD\r\n"
        "END:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:uid-vtimezone@google.com\r\n"
        "DTSTART;TZID=America/New_York:20260115T090000\r\n"
        "DTEND;TZID=America/New_York:20260115T100000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    # 09:00/10:00 America/New_York in January (EST, UTC-5) is 14:00/15:00 UTC —
    # not the VTIMEZONE's 1970 DAYLIGHT rule DTSTART.
    assert message["start"] == "2026-01-15T14:00:00Z"
    assert message["end"] == "2026-01-15T15:00:00Z"


def test_google_mail_get_does_not_report_a_meeting_for_an_unrecognized_method() -> None:
    """A `PUBLISH` iTIP part is a plain calendar broadcast, not an invite/RSVP —
    finding a `text/calendar` part alone must not be enough to claim
    `is_meeting_message`.
    """
    ics = (
        "BEGIN:VCALENDAR\r\nMETHOD:PUBLISH\r\nBEGIN:VEVENT\r\n"
        "UID:uid-publish@google.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["is_meeting_message"] is False
    assert message["meeting_message_type"] is None
    assert message["ical_uid"] is None


def test_google_mail_get_reports_no_meeting_metadata_for_a_plain_message() -> None:
    service = _service(_full_message_plain())

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["is_meeting_message"] is False
    assert message["meeting_message_type"] is None
    assert message["ical_uid"] is None


def test_google_mail_list_leaves_meeting_metadata_unknown(monkeypatch) -> None:
    """Gmail's `metadata` message format has no MIME parts to inspect."""
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": "m-1"}]}
    messages.get.return_value.execute.return_value = {
        "id": "m-1",
        "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": "Subject", "value": "Team sync"}]},
    }

    with _patched(service):
        payload = asyncio.run(google_mail.mail_inbox(top=5))

    (item,) = payload["items"]
    assert item["is_meeting_message"] is None
    assert item["meeting_message_type"] is None
    assert item["ical_uid"] is None


def test_google_mail_get_fetches_a_calendar_part_served_as_an_attachment() -> None:
    """An Outlook/Exchange-originated invite can land as `Content-Disposition:
    attachment`, in which case Gmail leaves `body.data` empty and only sets
    `body.attachmentId` — the same shape every other attachment already uses.
    """
    ics = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
        "UID:uid-att@google.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    encoded = base64.urlsafe_b64encode(ics.encode()).decode()
    message = {
        "id": "m-1",
        "threadId": "t-1",
        "labelIds": ["INBOX"],
        "internalDate": "1735689600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Team sync"}],
            "parts": [
                {
                    "mimeType": "text/calendar",
                    "filename": "invite.ics",
                    "body": {"attachmentId": "att-1"},
                },
            ],
        },
    }
    service = _service(message)
    attachments = service.users.return_value.messages.return_value.attachments
    attachments.return_value.get.return_value.execute.return_value = {"data": encoded}

    with _patched(service):
        result = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert result["is_meeting_message"] is True
    assert result["meeting_message_type"] == "meetingRequest"
    assert result["ical_uid"] == "uid-att@google.com"


def test_google_mail_get_reports_unresolvable_metadata_for_a_gone_attachment() -> None:
    """A vanished attachment must not fail the whole read: still a meeting, just
    with RSVP/UID unresolved rather than incorrectly reported as "not a meeting".
    """
    message = {
        "id": "m-1",
        "threadId": "t-1",
        "labelIds": ["INBOX"],
        "internalDate": "1735689600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Team sync"}],
            "parts": [
                {
                    "mimeType": "text/calendar",
                    "filename": "invite.ics",
                    "body": {"attachmentId": "att-missing"},
                },
            ],
        },
    }
    service = _service(message)
    attachments = service.users.return_value.messages.return_value.attachments
    attachments.return_value.get.return_value.execute.return_value = {}

    with _patched(service):
        result = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert result["is_meeting_message"] is True
    assert result["meeting_message_type"] is None
    assert result["ical_uid"] is None


def test_google_mail_get_reports_unresolvable_metadata_when_the_attachment_fetch_errors() -> None:
    """A transient Google API error (404/403/5xx) fetching the attachment must not
    fail the whole message read either — same courtesy-read contract as a
    successful-but-empty response.
    """
    message = {
        "id": "m-1",
        "threadId": "t-1",
        "labelIds": ["INBOX"],
        "internalDate": "1735689600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Team sync"}],
            "parts": [
                {
                    "mimeType": "text/calendar",
                    "filename": "invite.ics",
                    "body": {"attachmentId": "att-broken"},
                },
            ],
        },
    }
    service = _service(message)
    attachments = service.users.return_value.messages.return_value.attachments
    attachments.return_value.get.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 404}), b"gone"
    )

    with _patched(service):
        result = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert result["is_meeting_message"] is True
    assert result["meeting_message_type"] is None
    assert result["ical_uid"] is None


def test_google_mail_get_reports_unresolvable_metadata_for_a_malformed_ics_body() -> None:
    """A `text/calendar` part that isn't valid ICS (truncated, corrupted, or
    otherwise not parseable) must not fail the whole read either — same
    courtesy-read contract as a gone/unfetchable attachment. This part is
    sender-controlled, so a merely odd body shouldn't hard-fail `mail get`.
    """
    service = _service(_full_message_with_raw_ics("this is not a calendar body"))

    with _patched(service):
        result = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert result["is_meeting_message"] is True
    assert result["meeting_message_type"] is None
    assert result["ical_uid"] is None


def test_google_mail_get_uses_the_first_attendee_for_a_multi_attendee_reply() -> None:
    """A REPLY normally carries exactly one `ATTENDEE` (the person replying),
    but pin the behavior for the rare/non-standard case of more than one:
    the first `ATTENDEE`'s `PARTSTAT` is the one that determines
    `meeting_message_type`, not a later one.
    """
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "METHOD:REPLY\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:uid-multi@google.com\r\n"
        'ATTENDEE;CN="Rebecca";PARTSTAT=ACCEPTED:mailto:rebecca@example.com\r\n'
        'ATTENDEE;CN="Sam";PARTSTAT=DECLINED:mailto:sam@example.com\r\n'
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    service = _service(_full_message_with_raw_ics(ics))

    with _patched(service):
        message = asyncio.run(google_mail.mail_get(message_id="m-1"))["message"]

    assert message["meeting_message_type"] == "meetingAccepted"


def test_google_mail_thread_full_surfaces_meeting_metadata_per_item() -> None:
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.get.return_value.execute.return_value = {"threadId": "t-1"}
    ics_msg = _full_message_with_ics(method="REQUEST", uid="uid-thread@google.com")
    plain_msg = _full_message_plain()
    plain_msg["id"] = "m-2"
    users.threads.return_value.get.return_value.execute.return_value = {
        "messages": [ics_msg, plain_msg]
    }

    with _patched(service):
        payload = asyncio.run(google_mail.mail_thread(message_id="m-1", full=True))

    invite_item, plain_item = payload["items"]
    assert invite_item["is_meeting_message"] is True
    assert invite_item["meeting_message_type"] == "meetingRequest"
    assert invite_item["ical_uid"] == "uid-thread@google.com"
    assert plain_item["is_meeting_message"] is False
    assert plain_item["meeting_message_type"] is None
    assert plain_item["ical_uid"] is None


def test_mail_get_does_not_retry_the_expand_query_on_a_non_400_error(monkeypatch) -> None:
    """A 429/5xx/401/403 is a real failure, not a rejected query shape: retrying
    would duplicate the request (doubling load right when Graph may be
    signalling back-off) and hide the original, more diagnostic error.
    """
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    error = ODataError()
    error.response_status_code = 429
    error.error = MainError(code="TooManyRequests", message="throttled")
    item.get = AsyncMock(side_effect=error)

    with pytest.raises(ODataError):
        asyncio.run(mail_get(message_id="msg-1"))

    assert item.get.await_count == 1


def _full_message_with_ics(*, method: str, uid: str) -> dict:
    ics = (
        f"BEGIN:VCALENDAR\r\nMETHOD:{method}\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return _full_message_with_raw_ics(ics)


def _full_message_with_raw_ics(ics: str) -> dict:
    encoded = base64.urlsafe_b64encode(ics.encode()).decode()
    return {
        "id": "m-1",
        "threadId": "t-1",
        "labelIds": ["INBOX"],
        "internalDate": "1735689600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Team sync"},
                {"name": "From", "value": "Organizer <organizer@example.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"hi").decode()},
                },
                {"mimeType": "text/calendar", "body": {"data": encoded}},
            ],
        },
    }


def _full_message_plain() -> dict:
    return {
        "id": "m-1",
        "threadId": "t-1",
        "labelIds": ["INBOX"],
        "internalDate": "1735689600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Team sync"},
                {"name": "From", "value": "Rebecca <rebecca@example.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"hi").decode()},
                },
            ],
        },
    }


def _patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_MAIL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _service(message: dict) -> MagicMock:
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.get.return_value.execute.return_value = message
    return service
