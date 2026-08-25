"""Hermetic unit tests for Phase 2 read skills."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from blumkin.skills.calendar import (
    calendar_freebusy,
    calendar_view,
    format_view_human,
    parse_local_datetime,
)
from blumkin.skills.chat import chat_find, chat_last, format_find_human
from blumkin.skills.mail import format_inbox_human, mail_inbox


def test_parse_local_datetime_date_and_wall_time() -> None:
    tz = ZoneInfo("America/New_York")
    day = parse_local_datetime("2026-08-25", tz)
    assert day == datetime(2026, 8, 25, tzinfo=tz)
    wall = parse_local_datetime("2026-08-27T17:00", tz)
    assert wall == datetime(2026, 8, 27, 17, 0, tzinfo=tz)


def test_calendar_view_mocked(monkeypatch) -> None:
    event = SimpleNamespace(
        id="evt-1",
        subject="Sync",
        is_all_day=False,
        is_organizer=False,
        start=SimpleNamespace(date_time="2026-08-25T15:00:00Z"),
        end=SimpleNamespace(date_time="2026-08-25T15:30:00Z"),
        location=SimpleNamespace(display_name=None),
        organizer=SimpleNamespace(email_address=SimpleNamespace(name="A", address="a@example.com")),
        response_status=SimpleNamespace(response="accepted"),
        online_meeting=None,
    )
    client = MagicMock()
    client.me.calendar.calendar_view.get = AsyncMock(return_value=SimpleNamespace(value=[event]))
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    tz = ZoneInfo("UTC")
    payload = asyncio.run(
        calendar_view(
            start=datetime(2026, 8, 25, tzinfo=tz),
            end=datetime(2026, 8, 26, tzinfo=tz),
        )
    )
    assert len(payload["items"]) == 1
    assert payload["items"][0]["subject"] == "Sync"
    assert any("Sync" in line for line in format_view_human(payload))


def test_calendar_freebusy_mocked(monkeypatch) -> None:
    entry = SimpleNamespace(
        schedule_id="peer@example.com",
        availability_view="000",
        schedule_items=[
            SimpleNamespace(
                status="busy",
                start=SimpleNamespace(date_time="2026-08-27T21:00:00Z"),
                end=SimpleNamespace(date_time="2026-08-27T21:30:00Z"),
            )
        ],
    )
    client = MagicMock()
    client.me.calendar.get_schedule.post = AsyncMock(return_value=SimpleNamespace(value=[entry]))
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    tz = ZoneInfo("America/New_York")
    payload = asyncio.run(
        calendar_freebusy(
            with_emails=["peer@example.com"],
            start=datetime(2026, 8, 27, 17, 0, tzinfo=tz),
            end=datetime(2026, 8, 27, 17, 30, tzinfo=tz),
        )
    )
    assert payload["items"][0]["schedule"] == "peer@example.com"
    assert payload["items"][0]["busy"][0]["status"] == "busy"


def test_mail_inbox_mocked(monkeypatch) -> None:
    msg = SimpleNamespace(
        id="m1",
        subject="Hello",
        body_preview="hi",
        body=SimpleNamespace(content="<p>hi</p>"),
        has_attachments=False,
        is_read=False,
        received_date_time="2026-08-25T12:00:00+00:00",
        from_=SimpleNamespace(email_address=SimpleNamespace(name="Sam", address="sam@example.com")),
    )
    client = MagicMock()
    client.me.messages.get = AsyncMock(return_value=SimpleNamespace(value=[msg]))
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_inbox(top=5))
    assert payload["top"] == 5
    assert payload["items"][0]["subject"] == "Hello"
    assert payload["items"][0]["body_text"] == "hi"
    assert any("Hello" in line for line in format_inbox_human(payload))


def test_chat_find_and_last_mocked(monkeypatch) -> None:
    chat = SimpleNamespace(id="chat-1", topic="Standup", chat_type="oneOnOne")
    member = SimpleNamespace(display_name="Daniel Erickson")
    message = SimpleNamespace(
        id="msg-1",
        message_type="message",
        created_date_time="2026-08-25T12:00:00+00:00",
        body=SimpleNamespace(content="<p>ping</p>"),
        from_=SimpleNamespace(user=SimpleNamespace(display_name="Daniel Erickson", id="u1")),
    )
    client = MagicMock()
    client.me.chats.get = AsyncMock(return_value=SimpleNamespace(value=[chat]))
    client.me.chats.by_chat_id.return_value.members.get = AsyncMock(
        return_value=SimpleNamespace(value=[member])
    )
    client.me.chats.by_chat_id.return_value.messages.get = AsyncMock(
        return_value=SimpleNamespace(value=[message])
    )
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    found = asyncio.run(chat_find(with_name="daniel"))
    assert found["items"][0]["id"] == "chat-1"
    assert any("Daniel" in line for line in format_find_human(found))
    last = asyncio.run(chat_last(with_name="daniel", n=1))
    assert last["chat"]["id"] == "chat-1"
    assert last["items"][0]["body_text"] == "ping"


def test_calendar_view_rejects_inverted_range(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    tz = ZoneInfo("UTC")
    try:
        asyncio.run(
            calendar_view(
                start=datetime(2026, 8, 26, tzinfo=tz),
                end=datetime(2026, 8, 25, tzinfo=tz),
            )
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "end must be after start" in str(exc)
