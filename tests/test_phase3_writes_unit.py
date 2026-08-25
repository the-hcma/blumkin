"""Hermetic tests for Phase 3 write skills."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from blumkin.skills.calendar import _event_to_dict
from blumkin.skills.calendar_writes import (
    _needs_accept,
    calendar_accept,
    calendar_cancel,
    calendar_create,
    parse_duration,
)
from blumkin.skills.mail import mail_draft, mail_send_draft


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
    sent = asyncio.run(mail_send_draft(draft_id="draft-1"))
    assert sent == {"sent": "draft-1"}
