"""Hermetic tests for Google calendar accept / cancel / update."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httplib2
import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind

_GOOGLE_CAL = "blumkin.providers.google.calendar"


def test_calendar_accept_patches_only_your_own_attendee_entry(tmp_path: Path) -> None:
    event = {
        "id": "evt-1",
        "summary": "Sync",
        "start": {"dateTime": "2026-09-01T10:00:00-04:00"},
        "end": {"dateTime": "2026-09-01T10:30:00-04:00"},
        "attendees": [
            {"email": "ada@example.com", "responseStatus": "accepted"},
            {"email": "me@example.com", "self": True, "responseStatus": "needsAction"},
        ],
    }
    service = _service(event=event)
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_accept(event_id="evt-1")
        )
    assert payload["accepted"] == ["evt-1"]
    assert payload["count"] == 1
    body = service.events.return_value.patch.call_args.kwargs["body"]
    by_email = {a["email"]: a for a in body["attendees"]}
    assert by_email["me@example.com"]["responseStatus"] == "accepted"
    # Everyone else's RSVP is sent back untouched.
    assert by_email["ada@example.com"]["responseStatus"] == "accepted"
    assert service.events.return_value.patch.call_args.kwargs["sendUpdates"] == "all"
    # A blind retry past an ambiguous failure would re-notify the organizer.
    service.events.return_value.patch.return_value.execute.assert_called_with(num_retries=0)


def test_calendar_accept_refuses_an_event_you_are_not_invited_to(tmp_path: Path) -> None:
    service = _service(event={"id": "evt-1", "attendees": [{"email": "ada@example.com"}]})
    with _patched(service), pytest.raises(ValueError, match="does not list you as an attendee"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_accept(event_id="evt-1"))


def test_calendar_accept_requires_exactly_one_selector(tmp_path: Path) -> None:
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
    with _patched(_service()):
        with pytest.raises(ValueError, match="exactly one of --event-id or --today-pending"):
            asyncio.run(provider.calendar_accept())
        with pytest.raises(ValueError, match="exactly one of --event-id or --today-pending"):
            asyncio.run(provider.calendar_accept(event_id="evt-1", today_pending=True))


def test_calendar_accept_today_pending_skips_answered_and_organized_events(tmp_path: Path) -> None:
    """response must be mapped from Google's status, or every event looks unanswered."""
    listed = {
        "items": [
            {  # already accepted
                "id": "evt-answered",
                "summary": "Answered",
                "start": {"dateTime": "2026-09-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T09:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "accepted"}
                ],
            },
            {  # yours, so nothing to accept
                "id": "evt-mine",
                "summary": "Mine",
                "start": {"dateTime": "2026-09-01T11:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T11:30:00-04:00"},
                "organizer": {"email": "me@example.com", "self": True},
            },
            {  # already declined - re-accepting this would email the organizer
                "id": "evt-declined",
                "summary": "Declined",
                "start": {"dateTime": "2026-09-01T10:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T10:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "declined"}
                ],
            },
            {  # tentatively accepted, also already answered
                "id": "evt-tentative",
                "summary": "Tentative",
                "start": {"dateTime": "2026-09-01T12:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T12:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "tentative"}
                ],
            },
            {  # the only one that needs an RSVP
                "id": "evt-pending",
                "summary": "Pending",
                "start": {"dateTime": "2026-09-01T13:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T13:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "needsAction"}
                ],
            },
        ]
    }
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = listed
    service.events.return_value.get.return_value.execute.return_value = listed["items"][-1]
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_accept(
                today_pending=True, tz_name="America/New_York"
            )
        )
    assert payload["accepted"] == ["evt-pending"]
    assert payload["count"] == 1


def test_calendar_cancel_deletes_and_notifies(tmp_path: Path) -> None:
    service = _service(
        event={"id": "evt-9", "organizer": {"email": "me@example.com", "self": True}}
    )
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_cancel(event_id=" evt-9 ")
        )
    assert payload == {"cancelled": "evt-9"}
    service.events.return_value.delete.assert_called_once_with(
        calendarId="primary", eventId="evt-9", sendUpdates="all"
    )
    # Cancellation mails attendees and a repeat delete 410s, so never retry.
    service.events.return_value.delete.return_value.execute.assert_called_with(num_retries=0)


def test_calendar_update_attaches_a_meet_conference(tmp_path: Path) -> None:
    patched_event = {
        "id": "evt-1",
        "summary": "Sync",
        "start": {"dateTime": "2026-09-01T10:00:00-04:00"},
        "end": {"dateTime": "2026-09-01T10:30:00-04:00"},
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
            ]
        },
    }
    service = _service(patched=patched_event)
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_update(
                event_id="evt-1", tz_name="America/New_York"
            )
        )
    # hangoutLink lags a fresh patch, so the link has to come off conferenceData.
    assert payload["event"]["online_join_url"] == "https://meet.google.com/abc-defg-hij"
    kwargs = service.events.return_value.patch.call_args.kwargs
    assert kwargs["conferenceDataVersion"] == 1
    # This PATCH emails every attendee about the new link; dropping it would leave
    # them never told, with every test still green.
    assert kwargs["sendUpdates"] == "all"
    request = kwargs["body"]["conferenceData"]["createRequest"]
    assert request["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert request["requestId"]
    # sendUpdates="all" on this PATCH means a retry would re-notify attendees.
    service.events.return_value.patch.return_value.execute.assert_called_with(num_retries=0)


def test_calendar_update_reports_a_conference_that_never_provisioned(tmp_path: Path) -> None:
    service = _service(patched={"id": "evt-1", "summary": "Sync"})
    with _patched(service), pytest.raises(RuntimeError, match="was not provisioned"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_update(event_id="evt-1"))


def test_calendar_update_rejects_no_teams(tmp_path: Path) -> None:
    with _patched(_service()), pytest.raises(ValueError, match="do not pass --no-teams"):
        asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_update(event_id="evt-1", teams=False)
        )


def _cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    if not oauth.is_file():
        oauth.write_text(
            '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}'
        )
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="America/New_York",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _patched(service: MagicMock):
    return patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def _service(*, event: dict | None = None, patched: dict | None = None) -> MagicMock:
    service = MagicMock()
    events = service.events.return_value
    events.get.return_value.execute.return_value = event or {"id": "evt-1", "attendees": []}
    events.patch.return_value.execute.return_value = patched or (event or {"id": "evt-1"})
    events.delete.return_value.execute.return_value = ""
    events.list.return_value.execute.return_value = {"items": []}
    return service


def test_calendar_cancel_refuses_an_event_you_do_not_organize(tmp_path: Path) -> None:
    """Google's delete would only drop your copy, silently leaving the meeting alive."""
    service = _service(event={"id": "evt-1", "organizer": {"email": "ada@example.com"}})
    with _patched(service), pytest.raises(ValueError, match="do not organize"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_cancel(event_id="evt-1"))
    service.events.return_value.delete.assert_not_called()


def test_calendar_update_rereads_when_the_conference_is_still_pending(tmp_path: Path) -> None:
    """Meet provisions asynchronously; a pending PATCH response is not a failure."""
    pending = {"id": "evt-1", "summary": "Sync", "conferenceData": {"status": "pending"}}
    settled = {
        "id": "evt-1",
        "summary": "Sync",
        "start": {"dateTime": "2026-09-01T10:00:00-04:00"},
        "end": {"dateTime": "2026-09-01T10:30:00-04:00"},
        "conferenceData": {
            "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/x"}]
        },
    }
    service = _service(patched=pending)
    service.events.return_value.get.return_value.execute.return_value = settled
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_update(event_id="evt-1")
        )
    assert payload["event"]["online_join_url"] == "https://meet.google.com/x"


def test_calendar_accept_today_pending_skips_rather_than_aborting_mid_batch(
    tmp_path: Path,
) -> None:
    """An un-acceptable event must not strand earlier RSVPs with no report."""
    listed = {
        "items": [
            {  # no self attendee: _needs_accept lets it through, _accept_one cannot act
                "id": "evt-orphan",
                "summary": "Placed on my calendar",
                "start": {"dateTime": "2026-09-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T09:30:00-04:00"},
            },
            {
                "id": "evt-pending",
                "summary": "Pending",
                "start": {"dateTime": "2026-09-01T13:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T13:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "needsAction"}
                ],
            },
        ]
    }
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = listed

    def _get(*, calendarId: str, eventId: str):  # noqa: N803
        request = MagicMock()
        request.execute.return_value = next(i for i in listed["items"] if i["id"] == eventId)
        return request

    service.events.return_value.get.side_effect = _get
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_accept(
                today_pending=True, tz_name="America/New_York"
            )
        )
    assert payload["accepted"] == ["evt-pending"]
    assert [item["id"] for item in payload["skipped"]] == ["evt-orphan"]


def test_calendar_accept_refuses_a_truncated_attendee_list(tmp_path: Path) -> None:
    """PATCHing a truncated list back would delete the omitted attendees and email all."""
    service = _service(
        event={
            "id": "evt-big",
            "attendeesOmitted": True,
            "attendees": [
                {"email": "me@example.com", "self": True, "responseStatus": "needsAction"}
            ],
        }
    )
    with _patched(service), pytest.raises(ValueError, match="attendeesOmitted"):
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_accept(event_id="evt-big"))
    service.events.return_value.patch.assert_not_called()


def test_accept_human_output_names_the_events_it_skipped() -> None:
    """The batch report has to survive the default (non-json) path too."""
    from blumkin.skills.calendar_writes import format_accept_human

    lines = format_accept_human(
        {
            "accepted": ["evt-pending"],
            "count": 1,
            "skipped": [{"id": "evt-orphan", "reason": "does not list you as an attendee"}],
        }
    )
    assert any("evt-pending" in line for line in lines)
    assert any("skipped evt-orphan" in line and "attendee" in line for line in lines)


def test_calendar_update_hint_does_not_send_you_to_create_teams(tmp_path: Path) -> None:
    """`calendar create --teams` attaches nothing on Google, so it is a dead-end hint."""
    service = _service(patched={"id": "evt-1", "summary": "Sync"})
    with _patched(service), pytest.raises(RuntimeError) as excinfo:
        asyncio.run(GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_update(event_id="evt-1"))
    assert "calendar create --teams" not in str(excinfo.value)
    assert "re-run `calendar update`" in str(excinfo.value)


def test_calendar_accept_today_pending_survives_an_http_failure_on_one_event(
    tmp_path: Path,
) -> None:
    """A 404 or transient 5xx on one event must not strand the RSVPs already sent."""
    listed = {
        "items": [
            {
                "id": "evt-gone",
                "summary": "Deleted since the listing",
                "start": {"dateTime": "2026-09-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T09:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "needsAction"}
                ],
            },
            {
                "id": "evt-pending",
                "summary": "Pending",
                "start": {"dateTime": "2026-09-01T13:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T13:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "needsAction"}
                ],
            },
        ]
    }
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = listed

    def _get(*, calendarId: str, eventId: str):  # noqa: N803
        request = MagicMock()
        if eventId == "evt-gone":
            request.execute.side_effect = HttpError(
                httplib2.Response({"status": 404}), b"{}", uri="x"
            )
        else:
            request.execute.return_value = next(i for i in listed["items"] if i["id"] == eventId)
        return request

    service.events.return_value.get.side_effect = _get
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_accept(
                today_pending=True, tz_name="America/New_York"
            )
        )
    assert payload["accepted"] == ["evt-pending"]
    assert [item["id"] for item in payload["skipped"]] == ["evt-gone"]


def test_calendar_accept_today_pending_survives_a_transport_error(tmp_path: Path) -> None:
    """A socket timeout is not an HttpError, and must not strand a half-done sweep."""
    listed = {
        "items": [
            {
                "id": "evt-flaky",
                "summary": "Flaky",
                "start": {"dateTime": "2026-09-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T09:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "needsAction"}
                ],
            },
            {
                "id": "evt-pending",
                "summary": "Pending",
                "start": {"dateTime": "2026-09-01T13:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T13:30:00-04:00"},
                "attendees": [
                    {"email": "me@example.com", "self": True, "responseStatus": "needsAction"}
                ],
            },
        ]
    }
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = listed

    def _get(*, calendarId: str, eventId: str):  # noqa: N803
        request = MagicMock()
        if eventId == "evt-flaky":
            request.execute.side_effect = TimeoutError("connection reset")
        else:
            request.execute.return_value = next(i for i in listed["items"] if i["id"] == eventId)
        return request

    service.events.return_value.get.side_effect = _get
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_accept(
                today_pending=True, tz_name="America/New_York"
            )
        )
    assert payload["accepted"] == ["evt-pending"]
    assert [item["id"] for item in payload["skipped"]] == ["evt-flaky"]


def test_meet_link_prefers_hangout_link_on_the_read_path(tmp_path: Path) -> None:
    """`hangoutLink` is how every pre-existing Meet event carries its URL.

    Reading only conferenceData would emit online_join_url=null for the entire
    back catalogue while the conferenceData-only tests stayed green.
    """
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "evt-hangout",
                "summary": "Legacy Meet",
                "hangoutLink": "https://meet.google.com/legacy-abc",
                "start": {"dateTime": "2026-09-01T09:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T09:30:00-04:00"},
            },
            {
                "id": "evt-both",
                "summary": "Both fields",
                "hangoutLink": "https://meet.google.com/wins-xyz",
                "conferenceData": {
                    "entryPoints": [
                        {"entryPointType": "video", "uri": "https://meet.google.com/other-def"}
                    ]
                },
                "start": {"dateTime": "2026-09-01T11:00:00-04:00"},
                "end": {"dateTime": "2026-09-01T11:30:00-04:00"},
            },
        ]
    }
    with _patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).calendar_view(
                start=datetime(2026, 9, 1, tzinfo=ZoneInfo("America/New_York")),
                end=datetime(2026, 9, 2, tzinfo=ZoneInfo("America/New_York")),
            )
        )
    links = {e["id"]: e["online_join_url"] for e in payload["items"]}
    assert links["evt-hangout"] == "https://meet.google.com/legacy-abc"
    # When both are present hangoutLink is authoritative; the entry point is the
    # fallback for a just-patched event whose hangoutLink has not caught up.
    assert links["evt-both"] == "https://meet.google.com/wins-xyz"
