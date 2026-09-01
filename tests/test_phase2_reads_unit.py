"""Hermetic unit tests for Phase 2 read skills."""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from blumkin.skills.calendar import (
    calendar_freebusy,
    calendar_view,
    format_freebusy_human,
    format_view_human,
    parse_local_datetime,
)
from blumkin.skills.chat import chat_find, chat_last, format_find_human, format_last_human
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
    # Graph Edm.TimeOfDay arrives as strings via msgraph-sdk (not datetime.time).
    hours = SimpleNamespace(
        days_of_week=["monday", "tuesday", "wednesday", "thursday", "friday"],
        start_time="09:00:00.0000000",
        end_time="17:00:00.0000000",
        time_zone=SimpleNamespace(name="Central Standard Time"),
    )
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
        working_hours=hours,
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
    item = payload["items"][0]
    assert item["schedule"] == "peer@example.com"
    assert item["busy"][0]["status"] == "busy"
    assert item["timezone"] == "America/Chicago"
    assert item["working_hours"]["start"] == "09:00"
    assert item["working_hours"]["end"] == "17:00"
    assert item["working_hours"]["timezone"] == "America/Chicago"
    assert "friday" in item["working_hours"]["days_of_week"]
    human = format_freebusy_human(payload)
    assert any("America/Chicago" in line and "working 09:00-17:00" in line for line in human)


def test_calendar_freebusy_omits_empty_working_hours_object(monkeypatch) -> None:
    entry = SimpleNamespace(
        schedule_id="peer@example.com",
        availability_view="0",
        schedule_items=[],
        working_hours=SimpleNamespace(
            days_of_week=[],
            start_time=None,
            end_time=None,
            time_zone=None,
        ),
    )
    client = MagicMock()
    client.me.calendar.get_schedule.post = AsyncMock(return_value=SimpleNamespace(value=[entry]))
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    tz = ZoneInfo("UTC")
    payload = asyncio.run(
        calendar_freebusy(
            with_emails=["peer@example.com"],
            start=datetime(2026, 8, 27, 17, 0, tzinfo=tz),
            end=datetime(2026, 8, 27, 18, 0, tzinfo=tz),
        )
    )
    assert payload["items"][0]["working_hours"] is None
    assert payload["items"][0]["timezone"] is None


def test_calendar_freebusy_omits_missing_working_hours(monkeypatch) -> None:
    entry = SimpleNamespace(
        schedule_id="peer@example.com",
        availability_view="0",
        schedule_items=[],
        working_hours=None,
    )
    client = MagicMock()
    client.me.calendar.get_schedule.post = AsyncMock(return_value=SimpleNamespace(value=[entry]))
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    tz = ZoneInfo("UTC")
    payload = asyncio.run(
        calendar_freebusy(
            with_emails=["peer@example.com"],
            start=datetime(2026, 8, 27, 17, 0, tzinfo=tz),
            end=datetime(2026, 8, 27, 18, 0, tzinfo=tz),
        )
    )
    assert payload["items"][0]["working_hours"] is None
    assert payload["items"][0]["timezone"] is None


def test_calendar_freebusy_partial_hours_timezone_only_label(monkeypatch) -> None:
    hours = SimpleNamespace(
        days_of_week=["monday"],
        start_time=None,
        end_time=None,
        time_zone=SimpleNamespace(name="Pacific Standard Time"),
    )
    entry = SimpleNamespace(
        schedule_id="west@example.com",
        availability_view="0",
        schedule_items=[],
        working_hours=hours,
    )
    client = MagicMock()
    client.me.calendar.get_schedule.post = AsyncMock(return_value=SimpleNamespace(value=[entry]))
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    tz = ZoneInfo("UTC")
    payload = asyncio.run(
        calendar_freebusy(
            with_emails=["west@example.com"],
            start=datetime(2026, 8, 27, 17, 0, tzinfo=tz),
            end=datetime(2026, 8, 27, 18, 0, tzinfo=tz),
        )
    )
    item = payload["items"][0]
    assert item["timezone"] == "America/Los_Angeles"
    assert item["working_hours"]["start"] is None
    assert item["working_hours"]["end"] is None
    human = format_freebusy_human(payload)
    assert any("America/Los_Angeles" in line and "working" not in line for line in human)


def test_calendar_freebusy_working_hours_without_timezone(monkeypatch) -> None:
    hours = SimpleNamespace(
        days_of_week=["monday"],
        start_time=time(9, 0),
        end_time=time(17, 0),
        time_zone=None,
    )
    entry = SimpleNamespace(
        schedule_id="peer@example.com",
        availability_view="0",
        schedule_items=[],
        working_hours=hours,
    )
    client = MagicMock()
    client.me.calendar.get_schedule.post = AsyncMock(return_value=SimpleNamespace(value=[entry]))
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    tz = ZoneInfo("UTC")
    payload = asyncio.run(
        calendar_freebusy(
            with_emails=["peer@example.com"],
            start=datetime(2026, 8, 27, 17, 0, tzinfo=tz),
            end=datetime(2026, 8, 27, 18, 0, tzinfo=tz),
        )
    )
    item = payload["items"][0]
    assert item["timezone"] is None
    assert item["working_hours"]["timezone"] is None
    assert item["working_hours"]["start"] == "09:00"
    assert item["working_hours"]["end"] == "17:00"


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
    stub_get = client.me.chats.by_chat_id.return_value.messages.get
    assert stub_get.await_count == 1
    await_args = stub_get.await_args
    assert await_args is not None
    cfg = await_args.args[0]
    assert cfg.query_parameters.orderby == ["createdDateTime desc"]


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


def test_chat_find_all_forbidden_reraises_403(monkeypatch) -> None:
    bad = SimpleNamespace(id="chat-bad", topic="Broken", chat_type="group")
    client = MagicMock()
    client.me.chats.get = AsyncMock(return_value=SimpleNamespace(value=[bad], odata_next_link=None))
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
        raise AssertionError("expected 403")
    except RuntimeError as exc:
        assert getattr(exc, "response_status_code", None) == 403


def test_chat_find_auth_error_aborts_with_partial_match(monkeypatch) -> None:
    bad = SimpleNamespace(id="chat-bad", topic="Broken", chat_type="group")
    good = SimpleNamespace(id="chat-good", topic="Hit", chat_type="oneOnOne")
    client = MagicMock()
    client.me.chats.get = AsyncMock(
        return_value=SimpleNamespace(value=[bad, good], odata_next_link=None)
    )

    def by_chat_id(chat_id: str):
        stub = MagicMock()
        if chat_id == "chat-bad":
            err = RuntimeError("unauthorized")
            err.response_status_code = 401  # type: ignore[attr-defined]
            stub.members.get = AsyncMock(side_effect=err)
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
    try:
        asyncio.run(chat_find(with_name="daniel"))
        raise AssertionError("expected auth-class error")
    except RuntimeError as exc:
        assert getattr(exc, "response_status_code", None) == 401


def test_chat_find_auth_error_propagates(monkeypatch) -> None:
    chat = SimpleNamespace(id="chat-1", topic="Standup", chat_type="oneOnOne")
    client = MagicMock()
    client.me.chats.get = AsyncMock(
        return_value=SimpleNamespace(value=[chat], odata_next_link=None)
    )
    err = RuntimeError("unauthorized")
    err.response_status_code = 401  # type: ignore[attr-defined]
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
        assert getattr(exc, "response_status_code", None) == 401


def test_chat_find_mixed_all_skip_errors_raise_generic(monkeypatch) -> None:
    forbidden = SimpleNamespace(id="chat-forbidden", topic="Meeting", chat_type="meeting")
    throttled = SimpleNamespace(id="chat-throttled", topic="Broken", chat_type="group")
    client = MagicMock()
    client.me.chats.get = AsyncMock(
        return_value=SimpleNamespace(value=[forbidden, throttled], odata_next_link=None)
    )

    def by_chat_id(chat_id: str):
        stub = MagicMock()
        if chat_id == "chat-forbidden":
            err = RuntimeError("forbidden")
            err.response_status_code = 403  # type: ignore[attr-defined]
            stub.members.get = AsyncMock(side_effect=err)
        else:
            stub.members.get = AsyncMock(side_effect=RuntimeError("429 throttled"))
        return stub

    client.me.chats.by_chat_id.side_effect = by_chat_id
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    try:
        asyncio.run(chat_find(with_name="daniel"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "all 2 chats" in str(exc)
        assert getattr(exc, "response_status_code", None) is None


def test_chat_find_skips_forbidden_member_fetch(monkeypatch) -> None:
    bad = SimpleNamespace(id="chat-bad", topic="Meeting", chat_type="meeting")
    good = SimpleNamespace(id="chat-good", topic="Hit", chat_type="oneOnOne")
    client = MagicMock()
    client.me.chats.get = AsyncMock(
        return_value=SimpleNamespace(value=[bad, good], odata_next_link=None)
    )

    def by_chat_id(chat_id: str):
        stub = MagicMock()
        if chat_id == "chat-bad":
            err = RuntimeError("forbidden")
            err.response_status_code = 403  # type: ignore[attr-defined]
            stub.members.get = AsyncMock(side_effect=err)
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


def test_format_last_human_strips_control_chars() -> None:
    payload = {
        "chat": {"id": "c1", "topic": "T"},
        "items": [
            {
                "body_text": "hi\x1b[2Jthere",
                "created": "2026-08-25",
                "from_name": "Dan\x07iel",
            }
        ],
        "partial": False,
        "query": "dan",
        "skipped": 0,
    }
    lines = format_last_human(payload)
    joined = "\n".join(lines)
    assert "\x1b" not in joined
    assert "\x07" not in joined
    assert "hi[2Jthere" in joined
    assert "Daniel" in joined


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


def test_chat_last_by_chat_id_skips_member_scan(monkeypatch) -> None:
    """--chat-id goes straight to messages: no /me/chats listing, no member fetch."""
    message = SimpleNamespace(
        id="msg-1",
        message_type="message",
        created_date_time="2026-09-01T12:00:00+00:00",
        body=SimpleNamespace(content="<p>ping</p>"),
        from_=SimpleNamespace(user=SimpleNamespace(display_name="Daniel Erickson", id="u1")),
    )
    client = MagicMock()
    client.me.chats.get = AsyncMock(side_effect=AssertionError("should not list chats"))
    client.me.chats.by_chat_id.return_value.messages.get = AsyncMock(
        return_value=SimpleNamespace(value=[message], odata_next_link=None)
    )
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    last = asyncio.run(chat_last(chat_id="19:abc@unq.gbl.spaces", n=1))
    assert last["chat"]["id"] == "19:abc@unq.gbl.spaces"
    assert last["items"][0]["body_text"] == "ping"
    assert last["query"] is None
    assert last["partial"] is False
    client.me.chats.by_chat_id.assert_called_with("19:abc@unq.gbl.spaces")


def test_chat_last_requires_exactly_one_selector() -> None:
    # Neither selector.
    with pytest.raises(ValueError, match="exactly one of --with or --chat-id"):
        asyncio.run(chat_last(n=1))
    # Both selectors.
    with pytest.raises(ValueError, match="exactly one of --with or --chat-id"):
        asyncio.run(chat_last(with_name="daniel", chat_id="19:abc", n=1))


def test_chat_last_ambiguous_with_lists_candidate_ids(monkeypatch) -> None:
    """Two name matches must fail closed with the ids, not silently pick the first."""
    monkeypatch.setattr(
        "blumkin.skills.chat.chat_find",
        AsyncMock(
            return_value={
                "items": [{"id": "chat-1"}, {"id": "chat-2"}],
                "partial": False,
                "skipped": 0,
            }
        ),
    )
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(ValueError, match=r"ambiguous chat match .*chat-1, chat-2"):
        asyncio.run(chat_last(with_name="daniel", n=1))


def test_chat_last_refuses_a_partial_member_scan(monkeypatch) -> None:
    """One match from a partial scan is not trustworthy: a skipped chat may match too."""
    monkeypatch.setattr(
        "blumkin.skills.chat.chat_find",
        AsyncMock(return_value={"items": [{"id": "chat-1"}], "partial": True, "skipped": 2}),
    )
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(ValueError, match=r"is partial \(skipped 2 chat\(s\)\)"):
        asyncio.run(chat_last(with_name="daniel", n=1))


def _chat_message(msg_id: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=msg_id,
        message_type="message",
        created_date_time="2026-09-01T12:00:00+00:00",
        body=SimpleNamespace(content=f"<p>{text}</p>"),
        from_=SimpleNamespace(user=SimpleNamespace(display_name="Vivek", id="u1")),
    )


def _chat_id_client(monkeypatch, pages: list[SimpleNamespace]) -> MagicMock:
    client = MagicMock()
    first, *rest = pages
    client.me.chats.by_chat_id.return_value.messages.get = AsyncMock(return_value=first)
    client.me.chats.by_chat_id.return_value.messages.with_url.return_value.get = AsyncMock(
        side_effect=rest or [None]
    )
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    return client


def test_chat_last_contains_filters_bodies_case_insensitively(monkeypatch) -> None:
    page = SimpleNamespace(
        value=[
            _chat_message("m1", "lunch?"),
            _chat_message("m2", "Admin access to your laptop is sorted"),
            _chat_message("m3", "thanks"),
        ],
        odata_next_link=None,
    )
    _chat_id_client(monkeypatch, [page])
    last = asyncio.run(chat_last(chat_id="19:abc", contains="ADMIN ACCESS", n=5))
    assert [item["id"] for item in last["items"]] == ["m2"]
    assert last["filters"]["contains"] == "ADMIN ACCESS"
    # Walked the whole chat, so an empty result would genuinely mean "not there".
    assert last["filters"]["complete"] is True
    assert last["filters"]["scanned"] == 3


def test_chat_last_contains_reports_incomplete_when_it_stops_early(monkeypatch) -> None:
    page = SimpleNamespace(
        value=[_chat_message("m1", "admin"), _chat_message("m2", "admin")],
        odata_next_link="next-page",
    )
    _chat_id_client(monkeypatch, [page])
    last = asyncio.run(chat_last(chat_id="19:abc", contains="admin", n=1))
    assert [item["id"] for item in last["items"]] == ["m1"]
    # Stopped once n matches were in hand, so absence past this point is unknown.
    assert last["filters"]["complete"] is False
    assert last["filters"]["scanned"] == 1


def test_chat_last_without_contains_leaves_scan_fields_null(monkeypatch) -> None:
    page = SimpleNamespace(value=[_chat_message("m1", "ping")], odata_next_link=None)
    _chat_id_client(monkeypatch, [page])
    last = asyncio.run(chat_last(chat_id="19:abc", n=1))
    assert last["filters"] == {"complete": None, "contains": None, "scanned": None}


def test_chat_last_human_output_says_a_contains_filter_ran(monkeypatch) -> None:
    """A bare "(none)" must not read as "this chat is empty" when a filter was applied."""
    page = SimpleNamespace(
        value=[_chat_message("m1", "lunch?")],
        odata_next_link="next-page",
    )
    _chat_id_client(monkeypatch, [page])
    last = asyncio.run(chat_last(chat_id="19:abc", contains="admin", n=1))
    human = format_last_human(last)
    assert any("contains='admin'" in line for line in human)
    assert any("stopped after scanning" in line for line in human)
    assert any("(none)" in line for line in human)


def test_chat_last_human_output_is_unchanged_without_contains(monkeypatch) -> None:
    page = SimpleNamespace(value=[_chat_message("m1", "ping")], odata_next_link=None)
    _chat_id_client(monkeypatch, [page])
    human = format_last_human(asyncio.run(chat_last(chat_id="19:abc", n=1)))
    assert not any("filters:" in line for line in human)
