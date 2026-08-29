"""Hermetic tests for Phase 3 write skills."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from kiota_serialization_json.json_serialization_writer import JsonSerializationWriter
from msgraph.generated.models.body_type import BodyType
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
    format_update_human,
    parse_duration,
)
from blumkin.skills.mail import (
    MailBodyFileError,
    MailDraftNotFoundError,
    format_delete_draft_human,
    format_draft_human,
    format_send_draft_human,
    mail_delete_draft,
    mail_draft,
    mail_send_draft,
    mail_update_draft,
    resolve_mail_body,
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
        online_meeting=SimpleNamespace(join_url="https://teams.example/join"),
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
            tz_name="America/New_York",
        )
    )
    assert payload["event"]["id"] == "evt-new"
    assert payload["event"]["online_join_url"] == "https://teams.example/join"
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


def test_calendar_create_refetch_when_join_url_missing(monkeypatch) -> None:
    post_body = SimpleNamespace(
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
    fetched = SimpleNamespace(
        id="evt-new",
        subject="Sync",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=SimpleNamespace(join_url="https://teams.example/later"),
    )
    client = MagicMock()
    client.me.events.post = AsyncMock(return_value=post_body)
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=fetched)
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
            tz_name="America/New_York",
        )
    )
    assert payload["event"]["online_join_url"] == "https://teams.example/later"
    client.me.events.by_event_id.assert_called_once_with("evt-new")
    client.me.events.by_event_id.return_value.get.assert_awaited_once()


def test_calendar_create_raises_when_join_url_never_appears(monkeypatch) -> None:
    empty = SimpleNamespace(
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
    client.me.events.post = AsyncMock(return_value=empty)
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=empty)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    with pytest.raises(RuntimeError, match="not provisioned"):
        asyncio.run(
            calendar_create(
                subject="Sync",
                with_emails=["peer@example.com"],
                start_raw="2026-08-26T11:00",
                duration="30m",
                tz_name="America/New_York",
            )
        )


def test_calendar_create_raises_when_post_has_no_id(monkeypatch) -> None:
    client = MagicMock()
    client.me.events.post = AsyncMock(
        return_value=SimpleNamespace(id=None, online_meeting=None)
    )
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    with pytest.raises(RuntimeError, match="no event id from create"):
        asyncio.run(
            calendar_create(
                subject="Sync",
                with_emails=["peer@example.com"],
                start_raw="2026-08-26T11:00",
                duration="30m",
                tz_name="America/New_York",
            )
        )


def test_calendar_create_raises_when_post_returns_none(monkeypatch) -> None:
    client = MagicMock()
    client.me.events.post = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    with pytest.raises(RuntimeError, match="no event from create"):
        asyncio.run(
            calendar_create(
                subject="Sync",
                with_emails=["peer@example.com"],
                start_raw="2026-08-26T11:00",
                duration="30m",
                tz_name="America/New_York",
            )
        )


def test_calendar_create_raises_when_refetch_returns_none(monkeypatch) -> None:
    client = MagicMock()
    client.me.events.post = AsyncMock(
        return_value=SimpleNamespace(id="evt-new", online_meeting=None)
    )
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    with pytest.raises(RuntimeError, match="after create re-fetch"):
        asyncio.run(
            calendar_create(
                subject="Sync",
                with_emails=["peer@example.com"],
                start_raw="2026-08-26T11:00",
                duration="30m",
                tz_name="America/New_York",
            )
        )


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


def test_calendar_update_attaches_teams_mocked(monkeypatch) -> None:
    updated = SimpleNamespace(
        id="evt-1",
        subject="Sync",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=SimpleNamespace(join_url="https://teams.example/join"),
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.patch = AsyncMock(return_value=updated)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    from blumkin.skills.calendar_writes import calendar_update

    payload = asyncio.run(calendar_update(event_id="evt-1", tz_name="America/New_York"))
    assert payload["event"]["id"] == "evt-1"
    assert payload["event"]["online_join_url"] == "https://teams.example/join"
    patch_await = client.me.events.by_event_id.return_value.patch.await_args
    assert patch_await is not None
    patched = patch_await.args[0]
    assert patched.is_online_meeting is True
    assert patched.online_meeting_provider is OnlineMeetingProviderType.TeamsForBusiness


def test_calendar_update_no_teams_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    from blumkin.skills.calendar_writes import calendar_update

    with pytest.raises(ValueError, match="do not pass --no-teams"):
        asyncio.run(calendar_update(event_id="evt-1", teams=False, tz_name="America/New_York"))


def test_calendar_update_refetch_when_patch_returns_none(monkeypatch) -> None:
    fetched = SimpleNamespace(
        id="evt-1",
        subject="Sync",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=SimpleNamespace(join_url="https://teams.example/join"),
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.patch = AsyncMock(return_value=None)
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=fetched)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    from blumkin.skills.calendar_writes import calendar_update

    payload = asyncio.run(calendar_update(event_id="evt-1", tz_name="America/New_York"))
    assert payload["event"]["online_join_url"] == "https://teams.example/join"
    client.me.events.by_event_id.return_value.get.assert_awaited_once()


def test_calendar_update_refetch_when_join_url_missing(monkeypatch) -> None:
    patch_body = SimpleNamespace(
        id="evt-1",
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
    fetched = SimpleNamespace(
        id="evt-1",
        subject="Sync",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=SimpleNamespace(join_url="https://teams.example/later"),
    )
    client = MagicMock()
    client.me.events.by_event_id.return_value.patch = AsyncMock(return_value=patch_body)
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=fetched)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    from blumkin.skills.calendar_writes import calendar_update

    payload = asyncio.run(calendar_update(event_id="evt-1", tz_name="America/New_York"))
    assert payload["event"]["online_join_url"] == "https://teams.example/later"
    client.me.events.by_event_id.return_value.get.assert_awaited_once()


def test_calendar_update_raises_when_join_url_never_appears(monkeypatch) -> None:
    empty = SimpleNamespace(
        id="evt-1",
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
    client.me.events.by_event_id.return_value.patch = AsyncMock(return_value=empty)
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=empty)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    from blumkin.skills.calendar_writes import calendar_update

    with pytest.raises(RuntimeError, match="not provisioned"):
        asyncio.run(calendar_update(event_id="evt-1", tz_name="America/New_York"))


def test_calendar_update_raises_when_refetch_returns_none(monkeypatch) -> None:
    client = MagicMock()
    client.me.events.by_event_id.return_value.patch = AsyncMock(return_value=None)
    client.me.events.by_event_id.return_value.get = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    from blumkin.skills.calendar_writes import calendar_update

    with pytest.raises(RuntimeError, match="no event after update"):
        asyncio.run(calendar_update(event_id="evt-1", tz_name="America/New_York"))


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
    assert saved["draft"]["body_type"] == "text"
    post_await = client.me.messages.post.await_args
    assert post_await is not None
    posted = post_await.args[0]
    assert posted.subject == "Hi"
    assert posted.body.content == "Hello"
    assert posted.body.content_type == BodyType.Text
    assert posted.to_recipients[0].email_address.address == "a@b.com"
    sent = asyncio.run(mail_send_draft(draft_id="draft-1"))
    assert sent == {"sent": "draft-1"}
    client.me.messages.by_message_id.assert_called_once_with("draft-1")
    client.me.messages.by_message_id.return_value.send.post.assert_awaited_once()


def test_mail_draft_html_and_body_file(tmp_path, monkeypatch) -> None:
    draft = SimpleNamespace(id="draft-html", subject="Html")
    client = MagicMock()
    client.me.messages.post = AsyncMock(return_value=draft)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    saved = asyncio.run(
        mail_draft(to="a@b.com", subject="Html", body="<p>Hi</p>", body_type="html")
    )
    assert saved["draft"]["body_type"] == "html"
    post_await = client.me.messages.post.await_args
    assert post_await is not None
    posted = post_await.args[0]
    assert posted.body.content_type == BodyType.Html
    assert posted.body.content == "<p>Hi</p>"

    path = tmp_path / "message.html"
    path.write_text("<h1>File</h1>", encoding="utf-8")
    asyncio.run(
        mail_draft(
            to="a@b.com",
            subject="File",
            body_file=str(path),
            body_type="html",
        )
    )
    file_await = client.me.messages.post.await_args
    assert file_await is not None
    posted_file = file_await.args[0]
    assert posted_file.body.content == "<h1>File</h1>"
    assert posted_file.body.content_type == BodyType.Html

    text_path = tmp_path / "message.txt"
    text_path.write_text("plain file body", encoding="utf-8")
    asyncio.run(
        mail_draft(
            to="a@b.com",
            subject="TextFile",
            body_file=str(text_path),
        )
    )
    text_await = client.me.messages.post.await_args
    assert text_await is not None
    posted_text = text_await.args[0]
    assert posted_text.body.content == "plain file body"
    assert posted_text.body.content_type == BodyType.Text


def test_mail_delete_draft_mocked(monkeypatch) -> None:
    existing = SimpleNamespace(id="draft-1", is_draft=True)
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.delete = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_delete_draft(draft_id="draft-1"))
    assert payload == {"deleted": "draft-1"}
    client.me.messages.by_message_id.assert_called_with("draft-1")
    client.me.messages.by_message_id.return_value.delete.assert_awaited_once()


def test_mail_delete_draft_rejects_non_draft(monkeypatch) -> None:
    existing = SimpleNamespace(id="msg-1", is_draft=False)
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(MailDraftNotFoundError, match="not a draft"):
        asyncio.run(mail_delete_draft(draft_id="msg-1"))
    client.me.messages.by_message_id.return_value.delete.assert_not_called()


def test_resolve_mail_body_mutual_exclusion() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        resolve_mail_body(body=None, body_file=None)
    with pytest.raises(ValueError, match="exactly one"):
        resolve_mail_body(body="x", body_file="y")
    with pytest.raises(ValueError, match="body-type"):
        resolve_mail_body(body="x", body_type="markdown")


def test_resolve_mail_body_oserror_propagates(tmp_path, monkeypatch) -> None:
    path = tmp_path / "client_id.txt"
    path.write_text("x", encoding="utf-8")

    def _boom(self, *args, **kwargs):  # noqa: ANN001
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(type(path), "read_text", _boom)
    with pytest.raises(MailBodyFileError, match="cannot read --body-file"):
        resolve_mail_body(body_file=str(path))


def test_resolve_mail_body_unicode_decode_error(tmp_path) -> None:
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(MailBodyFileError, match="cannot read --body-file"):
        resolve_mail_body(body_file=str(path))


def test_mail_update_draft_mocked(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="a@b.com"))],
    )
    patched = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="New",
        body=SimpleNamespace(content_type=BodyType.Html, content="<p>new</p>"),
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="a@b.com"))],
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=patched)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(
        mail_update_draft(
            draft_id="draft-1",
            subject="New",
            body="<p>new</p>",
            body_type="html",
        )
    )
    assert payload["draft"]["subject"] == "New"
    assert payload["draft"]["body_type"] == "html"
    patch_await = client.me.messages.by_message_id.return_value.patch.await_args
    assert patch_await is not None
    posted = patch_await.args[0]
    assert posted.subject == "New"
    assert posted.body.content_type == BodyType.Html
    assert posted.body.content == "<p>new</p>"
    assert posted.to_recipients is None
    writer = JsonSerializationWriter()
    posted.serialize(writer)
    wire = json.loads(writer.get_serialized_content())
    assert "subject" in wire
    assert "body" in wire
    assert "toRecipients" not in wire
    assert "ccRecipients" not in wire


def test_mail_update_draft_requires_at_least_one_field() -> None:
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(mail_update_draft(draft_id="draft-1"))


def test_mail_update_draft_rejects_empty_body(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[],
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(mail_update_draft(draft_id="draft-1", body=""))
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(mail_update_draft(draft_id="draft-1", body="   "))


def test_mail_update_draft_subject_only(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Html, content="<p>old</p>"),
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="a@b.com"))],
    )
    patched = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="OnlySubject",
        body=existing.body,
        to_recipients=existing.to_recipients,
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=patched)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_update_draft(draft_id="draft-1", subject="OnlySubject"))
    assert payload["draft"]["to"] == "a@b.com"
    assert payload["draft"]["body_type"] == "html"
    assert payload["draft"]["subject"] == "OnlySubject"
    patch_await = client.me.messages.by_message_id.return_value.patch.await_args
    assert patch_await is not None
    posted = patch_await.args[0]
    assert posted.subject == "OnlySubject"
    assert posted.body is None
    assert posted.to_recipients is None
    writer = JsonSerializationWriter()
    posted.serialize(writer)
    wire = json.loads(writer.get_serialized_content())
    assert set(wire) <= {"@odata.type", "subject"}
    assert wire["subject"] == "OnlySubject"


def test_mail_update_draft_body_file(tmp_path, monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[],
    )
    path = tmp_path / "upd.html"
    path.write_text("<p>from file</p>", encoding="utf-8")
    patched = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Html, content="<p>from file</p>"),
        to_recipients=[],
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=patched)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(
        mail_update_draft(draft_id="draft-1", body_file=str(path), body_type="html")
    )
    assert payload["draft"]["body_type"] == "html"
    patch_await = client.me.messages.by_message_id.return_value.patch.await_args
    assert patch_await is not None
    posted = patch_await.args[0]
    assert posted.body.content == "<p>from file</p>"
    assert posted.body.content_type == BodyType.Html
    empty = tmp_path / "empty.txt"
    empty.write_text("   ", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(mail_update_draft(draft_id="draft-1", body_file=str(empty)))


def test_mail_update_draft_refetches_when_patch_returns_none(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="a@b.com"))],
    )
    after = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="New",
        body=existing.body,
        to_recipients=existing.to_recipients,
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(side_effect=[existing, after])
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_update_draft(draft_id="draft-1", subject="New"))
    assert payload["draft"]["subject"] == "New"
    assert client.me.messages.by_message_id.return_value.get.await_count == 2


def test_mail_update_draft_errors_when_patch_and_refetch_empty(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=None,
        to_recipients=[],
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(side_effect=[existing, None])
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(RuntimeError, match="no message after update-draft"):
        asyncio.run(mail_update_draft(draft_id="draft-1", subject="New"))


def test_mail_update_draft_to_only(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=SimpleNamespace(content_type=BodyType.Text, content="old"),
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="a@b.com"))],
    )
    patched = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=existing.body,
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="c@d.com"))],
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=patched)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_update_draft(draft_id="draft-1", to="c@d.com"))
    assert payload["draft"]["to"] == "c@d.com"
    patch_await = client.me.messages.by_message_id.return_value.patch.await_args
    assert patch_await is not None
    posted = patch_await.args[0]
    assert posted.subject is None
    assert posted.body is None
    assert len(posted.to_recipients) == 1
    assert posted.to_recipients[0].email_address.address == "c@d.com"


def test_mail_update_draft_replaces_to_when_multiple_recipients(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=None,
        to_recipients=[
            SimpleNamespace(email_address=SimpleNamespace(address="a@b.com")),
            SimpleNamespace(email_address=SimpleNamespace(address="c@d.com")),
        ],
        cc_recipients=[],
        bcc_recipients=[],
    )
    patched = SimpleNamespace(
        id="draft-1",
        is_draft=True,
        subject="Old",
        body=None,
        to_recipients=[SimpleNamespace(email_address=SimpleNamespace(address="e@f.com"))],
        cc_recipients=[],
        bcc_recipients=[],
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    client.me.messages.by_message_id.return_value.patch = AsyncMock(return_value=patched)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_update_draft(draft_id="draft-1", to="e@f.com"))
    assert payload["draft"]["to"] == "e@f.com"
    patch_await = client.me.messages.by_message_id.return_value.patch.await_args
    assert patch_await is not None
    posted = patch_await.args[0]
    assert len(posted.to_recipients) == 1
    assert posted.to_recipients[0].email_address.address == "e@f.com"


def test_mail_update_draft_rejects_non_draft(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="msg-1",
        is_draft=False,
        subject="Sent",
        body=None,
        to_recipients=[],
    )
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=existing)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(MailDraftNotFoundError, match="not a draft"):
        asyncio.run(mail_update_draft(draft_id="msg-1", subject="Nope"))


def test_mail_update_draft_message_not_found(monkeypatch) -> None:
    client = MagicMock()
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=None)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    with pytest.raises(MailDraftNotFoundError, match="message not found"):
        asyncio.run(mail_update_draft(draft_id="missing", subject="x"))


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
    update_lines = format_update_human(
        {
            "event": {
                "id": "evt-upd",
                "online_join_url": "https://teams.example/join",
                "subject": "Sync",
            }
        }
    )
    assert any("evt-upd" in line for line in update_lines)
    assert any("teams.example" in line for line in update_lines)
    draft_lines = format_draft_human({"draft": {"id": "draft-1", "subject": "Hi", "to": "a@b.com"}})
    assert any("draft-1" in line for line in draft_lines)
    assert any("a@b.com" in line for line in draft_lines)
    assert any("draft-1" in line for line in format_send_draft_human({"sent": "draft-1"}))
    assert any("draft-1" in line for line in format_delete_draft_human({"deleted": "draft-1"}))
