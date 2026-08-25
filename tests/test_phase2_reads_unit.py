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
    first_page = SimpleNamespace(value=[chat], odata_next_link=None)
    client.me.chats.get = AsyncMock(return_value=first_page)
    client.me.chats.by_chat_id.return_value.members.get = AsyncMock(
        return_value=SimpleNamespace(value=[member], odata_next_link=None)
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


def test_chat_find_follows_next_link(monkeypatch) -> None:
    page1_chat = SimpleNamespace(id="chat-a", topic="Other", chat_type="group")
    page2_chat = SimpleNamespace(id="chat-b", topic="Hit", chat_type="oneOnOne")
    page1 = SimpleNamespace(value=[page1_chat], odata_next_link="https://example/next")
    page2 = SimpleNamespace(value=[page2_chat], odata_next_link=None)
    client = MagicMock()
    client.me.chats.get = AsyncMock(return_value=page1)
    client.me.chats.with_url.return_value.get = AsyncMock(return_value=page2)

    def members_for(chat_id: str):
        name = "Other Person" if chat_id == "chat-a" else "Scott Young"
        return AsyncMock(
            return_value=SimpleNamespace(
                value=[SimpleNamespace(display_name=name)],
                odata_next_link=None,
            )
        )

    def by_chat_id(chat_id: str):
        stub = MagicMock()
        stub.members.get = members_for(chat_id)
        stub.members.with_url.return_value.get = AsyncMock()
        return stub

    client.me.chats.by_chat_id.side_effect = by_chat_id
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    found = asyncio.run(chat_find(with_name="Scott Young"))
    assert [c["id"] for c in found["items"]] == ["chat-b"]
    client.me.chats.with_url.assert_called_once_with("https://example/next")


def test_chat_find_skips_failing_member_fetch(monkeypatch) -> None:
    bad = SimpleNamespace(id="chat-bad", topic="Broken", chat_type="group")
    good = SimpleNamespace(id="chat-good", topic="Hit", chat_type="oneOnOne")
    client = MagicMock()
    client.me.chats.get = AsyncMock(
        return_value=SimpleNamespace(value=[bad, good], odata_next_link=None)
    )

    def by_chat_id(chat_id: str):
        stub = MagicMock()
        if chat_id == "chat-bad":
            stub.members.get = AsyncMock(side_effect=RuntimeError("429 throttled"))
        else:
            stub.members.get = AsyncMock(
                return_value=SimpleNamespace(
                    value=[SimpleNamespace(display_name="Daniel Erickson")],
                    odata_next_link=None,
                )
            )
        return stub

    client.me.chats.by_chat_id.side_effect = by_chat_id
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    found = asyncio.run(chat_find(with_name="daniel"))
    assert [c["id"] for c in found["items"]] == ["chat-good"]
    assert found["skipped"] == 1
    assert found["partial"] is True


def test_chat_find_all_member_failures_raise(monkeypatch) -> None:
    bad = SimpleNamespace(id="chat-bad", topic="Broken", chat_type="group")
    client = MagicMock()
    client.me.chats.get = AsyncMock(return_value=SimpleNamespace(value=[bad], odata_next_link=None))
    client.me.chats.by_chat_id.return_value.members.get = AsyncMock(
        side_effect=RuntimeError("429 throttled")
    )
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    try:
        asyncio.run(chat_find(with_name="daniel"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "all 1 chats" in str(exc)


def test_chat_find_auth_error_propagates(monkeypatch) -> None:
    chat = SimpleNamespace(id="chat-1", topic="Standup", chat_type="oneOnOne")
    client = MagicMock()
    client.me.chats.get = AsyncMock(
        return_value=SimpleNamespace(value=[chat], odata_next_link=None)
    )
    err = RuntimeError("forbidden")
    err.response_status_code = 403  # type: ignore[attr-defined]
    client.me.chats.by_chat_id.return_value.members.get = AsyncMock(side_effect=err)
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    try:
        asyncio.run(chat_find(with_name="daniel"))
        raise AssertionError("expected auth-class error")
    except RuntimeError as exc:
        assert getattr(exc, "response_status_code", None) == 403


def test_chat_last_follows_message_next_link(monkeypatch) -> None:
    chat = SimpleNamespace(id="chat-1", topic="Standup", chat_type="oneOnOne")
    member = SimpleNamespace(display_name="Daniel Erickson")
    page1_msg = SimpleNamespace(
        id="msg-event",
        message_type="systemEventMessage",
        created_date_time="2026-08-25T12:02:00+00:00",
        body=SimpleNamespace(content="joined"),
        from_=None,
    )
    page2_msg = SimpleNamespace(
        id="msg-1",
        message_type="message",
        created_date_time="2026-08-25T12:00:00+00:00",
        body=SimpleNamespace(content="<p>ping</p>"),
        from_=SimpleNamespace(user=SimpleNamespace(display_name="Daniel Erickson", id="u1")),
    )
    client = MagicMock()
    client.me.chats.get = AsyncMock(
        return_value=SimpleNamespace(value=[chat], odata_next_link=None)
    )
    stub = MagicMock()
    stub.members.get = AsyncMock(return_value=SimpleNamespace(value=[member], odata_next_link=None))
    stub.messages.get = AsyncMock(
        return_value=SimpleNamespace(value=[page1_msg], odata_next_link="https://example/msgs")
    )
    stub.messages.with_url.return_value.get = AsyncMock(
        return_value=SimpleNamespace(value=[page2_msg], odata_next_link=None)
    )
    client.me.chats.by_chat_id.return_value = stub
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    last = asyncio.run(chat_last(with_name="daniel", n=1))
    assert last["items"][0]["id"] == "msg-1"
    stub.messages.with_url.assert_called_once_with("https://example/msgs")


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
