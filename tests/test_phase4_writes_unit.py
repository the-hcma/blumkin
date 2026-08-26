"""Hermetic tests for Phase 4 chat write + meeting skills."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgraph.generated.models.body_type import BodyType

from blumkin.skills.chat import (
    chat_delete,
    chat_edit,
    chat_send,
    format_delete_human,
    format_edit_human,
    format_send_human,
)
from blumkin.skills.meeting import (
    format_get_human,
    format_transcription_human,
    meeting_get,
    meeting_transcription,
)


def test_chat_send_mocked(monkeypatch) -> None:
    async def fake_find(*, with_name: str, config=None):
        assert with_name == "daniel"
        return {
            "items": [
                {
                    "chat_type": "oneOnOne",
                    "id": "chat-1",
                    "members": ["Daniel Erickson"],
                    "topic": "Standup",
                }
            ],
            "partial": False,
            "query": with_name,
            "skipped": 0,
        }

    created = SimpleNamespace(
        id="msg-9",
        body=SimpleNamespace(content="use <b> tags", content_type=BodyType.Text),
        created_date_time="2026-08-26T12:00:00Z",
        from_=None,
    )
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.post = AsyncMock(return_value=created)
    monkeypatch.setattr("blumkin.skills.chat.chat_find", fake_find)
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(chat_send(with_name="daniel", text="  use <b> tags  "))
    assert payload["message"]["id"] == "msg-9"
    assert payload["message"]["body_text"] == "use <b> tags"
    assert payload["chat"]["id"] == "chat-1"
    post = client.me.chats.by_chat_id.return_value.messages.post
    post.assert_awaited_once()
    sent = post.await_args.args[0]  # type: ignore[union-attr]
    assert sent.body.content == "use <b> tags"
    assert sent.body.content_type == BodyType.Text
    assert "Sent message" in format_send_human(payload)[0]


def test_chat_send_refuses_partial_match(monkeypatch) -> None:
    async def fake_find(*, with_name: str, config=None):
        return {
            "items": [
                {
                    "chat_type": "group",
                    "id": "chat-g",
                    "members": ["Daniel"],
                    "topic": "Group",
                }
            ],
            "partial": True,
            "query": with_name,
            "skipped": 2,
        }

    monkeypatch.setattr("blumkin.skills.chat.chat_find", fake_find)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    with pytest.raises(ValueError, match="partial"):
        asyncio.run(chat_send(with_name="dan", text="hi"))

    async def fake_find(*, with_name: str, config=None):
        return {
            "items": [
                {"id": "chat-a", "members": ["Dan A"], "topic": "A", "chat_type": "oneOnOne"},
                {"id": "chat-b", "members": ["Dan B"], "topic": "B", "chat_type": "oneOnOne"},
            ],
            "partial": False,
            "query": with_name,
            "skipped": 0,
        }

    monkeypatch.setattr("blumkin.skills.chat.chat_find", fake_find)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    with pytest.raises(ValueError, match="ambiguous chat match"):
        asyncio.run(chat_send(with_name="dan", text="hi"))


def test_chat_send_by_chat_id(monkeypatch) -> None:
    created = SimpleNamespace(
        id="msg-2",
        body=SimpleNamespace(content="hi", content_type=BodyType.Text),
        created_date_time=None,
        from_=None,
    )
    client = MagicMock()
    client.me.chats.by_chat_id.return_value.messages.post = AsyncMock(return_value=created)
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(chat_send(chat_id="chat-9", text="hi"))
    assert payload["chat"]["id"] == "chat-9"
    client.me.chats.by_chat_id.assert_called_with("chat-9")


def test_chat_send_no_match_raises(monkeypatch) -> None:
    async def fake_find(*, with_name: str, config=None):
        return {"items": [], "partial": False, "query": with_name, "skipped": 0}

    monkeypatch.setattr("blumkin.skills.chat.chat_find", fake_find)
    with pytest.raises(LookupError, match="no chat matched"):
        asyncio.run(chat_send(with_name="nobody", text="hi"))


def test_chat_send_empty_text_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(chat_send(with_name="daniel", text="   "))


def test_chat_edit_mocked(monkeypatch) -> None:
    updated = SimpleNamespace(
        id="msg-1",
        body=SimpleNamespace(content="if x < 5", content_type=BodyType.Text),
        created_date_time="2026-08-26T12:00:00Z",
        from_=None,
    )
    client = MagicMock()
    stub = client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value
    stub.patch = AsyncMock(return_value=updated)
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(chat_edit(chat_id="chat-1", message_id="msg-1", text="if x < 5"))
    assert payload["message"]["body_text"] == "if x < 5"
    stub.patch.assert_awaited_once()
    assert "updated" in format_edit_human(payload)[0]


def test_chat_edit_reget_when_patch_empty(monkeypatch) -> None:
    updated = SimpleNamespace(
        id="msg-1",
        body=SimpleNamespace(content="edited"),
        created_date_time=None,
        from_=None,
    )
    client = MagicMock()
    stub = client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value
    stub.patch = AsyncMock(return_value=None)
    stub.get = AsyncMock(return_value=updated)
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(chat_edit(chat_id="c", message_id="m", text="edited"))
    assert payload["message"]["id"] == "msg-1"
    stub.get.assert_awaited_once()


def test_chat_delete_mocked(monkeypatch) -> None:
    client = MagicMock()
    soft = (
        client.me.chats.by_chat_id.return_value.messages.by_chat_message_id.return_value.soft_delete
    )
    soft.post = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.chat.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.chat.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(chat_delete(chat_id="chat-1", message_id="msg-1"))
    assert payload == {"chat_id": "chat-1", "deleted": "msg-1"}
    soft.post.assert_awaited_once()
    assert "soft-deleted" in format_delete_human(payload)[0]


def test_meeting_get_mocked(monkeypatch) -> None:
    event = SimpleNamespace(
        id="evt-1",
        is_online_meeting=True,
        online_meeting=SimpleNamespace(join_url="https://teams.microsoft.com/l/meetup-join/x"),
        subject="Sync",
    )
    meeting = SimpleNamespace(
        id="om-1",
        allow_transcription=False,
        allow_recording=True,
        record_automatically=False,
        join_web_url="https://teams.microsoft.com/l/meetup-join/x",
        subject="Sync",
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=event)
    client.me.online_meetings.get = AsyncMock(
        return_value=SimpleNamespace(value=[meeting]),
    )
    monkeypatch.setattr("blumkin.skills.meeting.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.meeting.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(meeting_get(event_id="evt-1"))
    assert payload["meeting"]["id"] == "om-1"
    assert payload["meeting"]["allow_transcription"] is False
    get_call = client.me.online_meetings.get.await_args
    assert get_call is not None
    query = get_call.args[0].query_parameters
    assert "JoinWebUrl eq" in query.filter
    assert "meetup-join/x" in query.filter
    assert "om-1" in format_get_human(payload)[1]


def test_meeting_get_not_found_for_attendee_only(monkeypatch) -> None:
    event = SimpleNamespace(
        id="evt-1",
        is_online_meeting=True,
        online_meeting=SimpleNamespace(join_url="https://join/abc"),
        subject="Sync",
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=event)
    client.me.online_meetings.get = AsyncMock(return_value=SimpleNamespace(value=[]))
    monkeypatch.setattr("blumkin.skills.meeting.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.meeting.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    with pytest.raises(LookupError, match="only returns meetings you organize"):
        asyncio.run(meeting_get(event_id="evt-1"))

    event = SimpleNamespace(
        id="evt-1",
        is_online_meeting=False,
        online_meeting=None,
        subject="Offline",
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=event)
    monkeypatch.setattr("blumkin.skills.meeting.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.meeting.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    with pytest.raises(LookupError, match="not a Teams online meeting"):
        asyncio.run(meeting_get(event_id="evt-1"))


def test_meeting_transcription_enable_mocked(monkeypatch) -> None:
    event = SimpleNamespace(
        id="evt-1",
        is_online_meeting=True,
        online_meeting=SimpleNamespace(join_url="https://join/abc"),
        subject="Sync",
    )
    before = SimpleNamespace(
        id="om-1",
        allow_transcription=False,
        allow_recording=None,
        record_automatically=None,
        join_web_url="https://join/abc",
        subject="Sync",
    )
    after = SimpleNamespace(
        id="om-1",
        allow_transcription=True,
        allow_recording=None,
        record_automatically=None,
        join_web_url="https://join/abc",
        subject="Sync",
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=event)
    client.me.online_meetings.get = AsyncMock(return_value=SimpleNamespace(value=[before]))
    client.me.online_meetings.by_online_meeting_id.return_value.patch = AsyncMock(
        return_value=after
    )
    monkeypatch.setattr("blumkin.skills.meeting.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.meeting.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(meeting_transcription(event_id="evt-1", enable=True))
    assert payload["enabled"] is True
    assert payload["mutated"] is True
    patch = client.me.online_meetings.by_online_meeting_id.return_value.patch
    patch.assert_awaited_once()
    assert patch.await_args.args[0].allow_transcription is True  # type: ignore[union-attr]
    assert "Enabled" in format_transcription_human(payload)[0]


def test_meeting_transcription_show_only(monkeypatch) -> None:
    event = SimpleNamespace(
        id="evt-1",
        is_online_meeting=True,
        online_meeting=SimpleNamespace(join_url="https://join/abc"),
        subject="Sync",
    )
    meeting = SimpleNamespace(
        id="om-1",
        allow_transcription=True,
        allow_recording=False,
        record_automatically=False,
        join_web_url="https://join/abc",
        subject="Sync",
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=event)
    client.me.online_meetings.get = AsyncMock(return_value=SimpleNamespace(value=[meeting]))
    monkeypatch.setattr("blumkin.skills.meeting.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.meeting.load_config",
        lambda: SimpleNamespace(client_id="x"),
    )
    payload = asyncio.run(meeting_transcription(event_id="evt-1", enable=False))
    assert payload["mutated"] is False
    assert payload["enabled"] is True
    client.me.online_meetings.by_online_meeting_id.return_value.patch.assert_not_called()
