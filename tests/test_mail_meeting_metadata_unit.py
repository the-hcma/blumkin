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

from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.event import Event
from msgraph.generated.models.event_message import EventMessage
from msgraph.generated.models.meeting_message_type import MeetingMessageType
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.providers.google import mail as google_mail
from blumkin.skills.mail import mail_get, mail_inbox

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
    item.get = AsyncMock(side_effect=[error, retried])

    message = asyncio.run(mail_get(message_id="msg-1"))["message"]

    assert item.get.await_count == 2
    second_query = item.get.await_args_list[1].args[0].query_parameters
    # `query.expand` is cleared in place on the shared query object before the
    # retry, so only the second call's state can be asserted directly here.
    assert second_query.expand is None
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


def test_mail_inbox_reports_no_meeting_metadata_for_a_plain_list_item(monkeypatch) -> None:
    client = _client(monkeypatch)
    page = SimpleNamespace(value=[_plain_message()], odata_next_link=None)
    client.me.messages.get = AsyncMock(return_value=page)

    payload = asyncio.run(mail_inbox(top=5))

    (item,) = payload["items"]
    assert item["is_meeting_message"] is False
    assert item["meeting_message_type"] is None


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
