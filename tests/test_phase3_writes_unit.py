"""Hermetic tests for Phase 3 write skills."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from msgraph.generated.models.online_meeting_provider_type import OnlineMeetingProviderType

from blumkin.skills.calendar import _event_to_dict
from blumkin.skills.calendar_writes import (
    _needs_accept,
    calendar_accept,
    calendar_cancel,
    calendar_create,
    format_accept_human,
    format_cancel_human,
    format_create_human,
    parse_duration,
)
from blumkin.skills.mail import (
    format_draft_human,
    format_send_draft_human,
    mail_draft,
    mail_send_draft,
)


def test_parse_duration() -> None:
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("1h") == timedelta(hours=1)
    with pytest.raises(ValueError):
        parse_duration("bad")


def test_calendar_accept_by_event_id_mocked(monkeypatch) -> None:
    client = MagicMock()
    client.me.events.by_event_id.return_value.accept.post = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(calendar_accept(event_id="evt-1", today_pending=False))
    assert payload == {"accepted": ["evt-1"], "count": 1}
    client.me.events.by_event_id.assert_called_once_with("evt-1")
    accept_await = client.me.events.by_event_id.return_value.accept.post.await_args
    assert accept_await is not None
    assert accept_await.args[0].send_response is True


def test_calendar_accept_requires_exactly_one_mode() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(calendar_accept(event_id=None, today_pending=False))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(calendar_accept(event_id="evt-1", today_pending=True))


def test_calendar_accept_today_pending_mocked(monkeypatch) -> None:
    async def fake_today(**_kwargs):
        return {
            "items": [
                {"id": "a", "is_organizer": False, "response": "ResponseType.NotResponded"},
                {"id": "b", "is_organizer": True, "response": "ResponseType.NotResponded"},
                {"id": "c", "is_organizer": False, "response": "ResponseType.Accepted"},
                {"id": "d", "is_organizer": False, "response": "ResponseType.Declined"},
                {"id": "e", "is_organizer": False, "response": "ResponseType.TentativelyAccepted"},
            ]
        }

    client = MagicMock()
    client.me.events.by_event_id.return_value.accept.post = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.calendar_today", fake_today)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    payload = asyncio.run(calendar_accept(event_id=None, today_pending=True))
    assert payload["accepted"] == ["a"]
    assert payload["count"] == 1
    accept_await = client.me.events.by_event_id.return_value.accept.post.await_args
    assert accept_await is not None
    assert accept_await.args[0].send_response is True


def test_needs_accept_filters_responses() -> None:
    assert _needs_accept({"is_organizer": False, "response": "ResponseType.NotResponded"})
    assert _needs_accept({"is_organizer": False, "response": None})
    assert not _needs_accept({"is_organizer": True, "response": "ResponseType.NotResponded"})
    assert not _needs_accept({"is_organizer": False, "response": "ResponseType.Accepted"})
    assert not _needs_accept({"is_organizer": False, "response": "ResponseType.Declined"})
    assert not _needs_accept(
        {"is_organizer": False, "response": "ResponseType.TentativelyAccepted"}
    )


def test_needs_accept_from_event_to_dict() -> None:
    tz = ZoneInfo("UTC")

    def event(*, response: str | None, is_organizer: bool = False) -> SimpleNamespace:
        response_status = None if response is None else SimpleNamespace(response=response)
        return SimpleNamespace(
            end=None,
            id="evt-1",
            is_all_day=False,
            is_organizer=is_organizer,
            location=None,
            online_meeting=None,
            organizer=None,
            response_status=response_status,
            start=None,
            subject="Sync",
        )

    pending = _event_to_dict(event(response="NotResponded"), tz)
    missing = _event_to_dict(event(response=None), tz)
    accepted = _event_to_dict(event(response="Accepted"), tz)
    assert _needs_accept(pending)
    assert _needs_accept(missing)
    assert not _needs_accept(accepted)
    assert "response" in pending
    assert "is_organizer" in pending


def test_calendar_cancel_mocked(monkeypatch) -> None:
    client = MagicMock()
    client.me.events.by_event_id.return_value.cancel.post = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(calendar_cancel(event_id="evt-9"))
    assert payload == {"cancelled": "evt-9"}
    client.me.events.by_event_id.assert_called_once_with("evt-9")
    client.me.events.by_event_id.return_value.cancel.post.assert_awaited_once()


def test_calendar_create_mocked(monkeypatch) -> None:
    created = SimpleNamespace(
        id="evt-new",
        subject="Sync",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=None,
    )
    client = MagicMock()
    client.me.events.post = AsyncMock(return_value=created)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    payload = asyncio.run(
        calendar_create(
            subject="Sync",
            with_emails=["peer@example.com"],
            start_raw="2026-08-26T11:00",
            duration="30m",
            teams=True,
            tz_name="America/New_York",
        )
    )
    assert payload["event"]["id"] == "evt-new"
    await_args = client.me.events.post.await_args
    assert await_args is not None
    posted = await_args.args[0]
    assert posted.subject == "Sync"
    assert posted.is_online_meeting is True
    assert posted.online_meeting_provider is OnlineMeetingProviderType.TeamsForBusiness
    assert posted.attendees[0].email_address.address == "peer@example.com"
    start = datetime.fromisoformat(posted.start.date_time)
    end = datetime.fromisoformat(posted.end.date_time)
    assert end - start == timedelta(minutes=30)


def test_calendar_create_without_teams_mocked(monkeypatch) -> None:
    created = SimpleNamespace(
        id="evt-new",
        subject="Sync",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=None,
    )
    client = MagicMock()
    client.me.events.post = AsyncMock(return_value=created)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    asyncio.run(
        calendar_create(
            subject="Sync",
            with_emails=["peer@example.com"],
            start_raw="2026-08-26T11:00",
            duration="30m",
            teams=False,
            tz_name="America/New_York",
        )
    )
    post_await = client.me.events.post.await_args
    assert post_await is not None
    posted = post_await.args[0]
    assert posted.is_online_meeting is None
    assert posted.online_meeting_provider is None


def test_mail_draft_and_send_mocked(monkeypatch) -> None:
    draft = SimpleNamespace(id="draft-1", subject="Hi")
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=draft)
    client.me.messages.by_message_id.return_value.send.post = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    saved = asyncio.run(mail_draft(to="a@b.com", subject="Hi", body="Hello"))
    assert saved["draft"]["id"] == "draft-1"
    post_await = client.me.messages.post.await_args
    assert post_await is not None
    posted = post_await.args[0]
    assert posted.subject == "Hi"
    assert posted.body.content == "Hello"
    assert posted.to_recipients[0].email_address.address == "a@b.com"
    sent = asyncio.run(mail_send_draft(draft_id="draft-1"))
    assert sent == {"sent": "draft-1"}
    client.me.messages.by_message_id.assert_called_once_with("draft-1")
    client.me.messages.by_message_id.return_value.send.post.assert_awaited_once()


def test_write_formatters_human() -> None:
    assert any("evt-1" in line for line in format_accept_human({"accepted": ["evt-1"], "count": 1}))
    assert any("evt-9" in line for line in format_cancel_human({"cancelled": "evt-9"}))
    create_lines = format_create_human(
        {
            "event": {
                "end": "2026-08-26T11:30",
                "id": "evt-new",
                "online_join_url": "https://teams.example/join",
                "start": "2026-08-26T11:00",
                "subject": "Sync",
            }
        }
    )
    assert any("evt-new" in line for line in create_lines)
    assert any("teams.example" in line for line in create_lines)
    draft_lines = format_draft_human({"draft": {"id": "draft-1", "subject": "Hi", "to": "a@b.com"}})
    assert any("draft-1" in line for line in draft_lines)
    assert any("a@b.com" in line for line in draft_lines)
    assert any("draft-1" in line for line in format_send_draft_human({"sent": "draft-1"}))
