"""Hermetic unit tests for Google Workspace provider MVP."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.providers.google_auth import GOOGLE_SCOPES, status_dict
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind


def test_auth_status_dict_keys(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    payload = status_dict(cfg)
    assert payload["provider"] == "google"
    assert payload["client_id_configured"] is True
    assert payload["token_cache"] is False
    assert payload["auth_record"] is False
    assert payload["tenant_id"] == ""
    assert payload["requested_scopes"] == sorted(GOOGLE_SCOPES)
    assert payload["config_dir"] == str(tmp_path)


def test_auth_status_with_token_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    expiry = datetime(2099, 1, 1, tzinfo=UTC)
    (tmp_path / "google_token.json").write_text(
        json.dumps(
            {
                "client_id": "fake-google-desktop-client.apps.googleusercontent.com",
                "client_secret": "",
                "expiry": expiry.isoformat().replace("+00:00", "Z"),
                "refresh_token": "fake-refresh",
                "scopes": sorted(GOOGLE_SCOPES),
                "token": "fake-access",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    payload = status_dict(cfg)
    assert payload["token_cache"] is True
    assert payload["refresh_token_present"] is True
    assert payload["access_token_expired"] is False


def test_logout_deletes_token(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    path = tmp_path / "google_token.json"
    path.write_text("{}")
    provider = GoogleWorkspaceProvider(cfg)
    provider.auth_logout()
    assert not path.exists()


def test_people_resolve_unsupported() -> None:
    provider = GoogleWorkspaceProvider(_cfg(Path("unused")))
    with pytest.raises(ValueError, match="not supported for provider=google"):
        asyncio.run(provider.people_resolve(name="Ada"))


def test_calendar_today_maps_events(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "evt-1",
                "summary": "Standup",
                "start": {"dateTime": "2026-08-30T15:00:00Z"},
                "end": {"dateTime": "2026-08-30T15:30:00Z"},
                "location": "Room A",
                "organizer": {
                    "email": "ada@example.com",
                    "displayName": "Ada",
                    "self": True,
                },
            }
        ]
    }
    with (
        patch("blumkin.providers.google.calendar.get_credentials", return_value=MagicMock()),
        patch("blumkin.providers.google.calendar.build", return_value=service),
    ):
        provider = GoogleWorkspaceProvider(cfg)
        payload = asyncio.run(
            provider.calendar_today(day=datetime(2026, 8, 30, tzinfo=UTC).date(), tz_name="UTC")
        )
    assert payload["date"] == "2026-08-30"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["id"] == "evt-1"
    assert item["subject"] == "Standup"
    assert item["location"] == "Room A"
    assert item["is_all_day"] is False


def test_calendar_freebusy_and_suggest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    service = MagicMock()
    service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "ada@example.com": {
                "busy": [
                    {"start": "2026-08-30T14:00:00Z", "end": "2026-08-30T15:00:00Z"},
                ]
            }
        }
    }
    start = datetime(2026, 8, 30, 13, 0, tzinfo=UTC)
    end = datetime(2026, 8, 30, 17, 0, tzinfo=UTC)
    with (
        patch("blumkin.providers.google.calendar.get_credentials", return_value=MagicMock()),
        patch("blumkin.providers.google.calendar.build", return_value=service),
    ):
        provider = GoogleWorkspaceProvider(cfg)
        freebusy = asyncio.run(
            provider.calendar_freebusy(with_emails=["ada@example.com"], start=start, end=end)
        )
        suggest = asyncio.run(
            provider.calendar_suggest(
                with_emails=["ada@example.com"],
                start=start,
                end=end,
                duration=timedelta(minutes=30),
                limit=3,
            )
        )
    assert freebusy["items"][0]["schedule"] == "ada@example.com"
    assert freebusy["items"][0]["busy"][0]["status"] == "busy"
    assert suggest["slots"]
    assert suggest["with"] == ["ada@example.com"]


def test_mail_inbox_and_get(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    list_body = {"messages": [{"id": "m1"}]}
    meta = {
        "id": "m1",
        "snippet": "hello",
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1725024000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Ada Lovelace <ada@example.com>"},
                {"name": "Subject", "value": "Hello"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Fri, 30 Aug 2024 12:00:00 +0000"},
            ]
        },
    }
    full = {
        **meta,
        "threadId": "t1",
        "payload": {
            **meta["payload"],
            "mimeType": "text/plain",
            "body": {"data": "aGVsbG8gd29ybGQ"},  # "hello world"
        },
    }
    messages = MagicMock()
    messages.list.return_value.execute.return_value = list_body
    messages.get.return_value.execute.side_effect = [meta, full]
    service = MagicMock()
    service.users.return_value.messages.return_value = messages
    with (
        patch("blumkin.providers.google.mail.get_credentials", return_value=MagicMock()),
        patch("blumkin.providers.google.mail.build", return_value=service),
    ):
        provider = GoogleWorkspaceProvider(cfg)
        inbox = asyncio.run(provider.mail_inbox(top=5))
        detail = asyncio.run(provider.mail_get(message_id="m1", body_type="text"))
    assert inbox["top"] == 5
    assert inbox["items"][0]["subject"] == "Hello"
    assert inbox["items"][0]["from_email"] == "ada@example.com"
    assert inbox["items"][0]["is_read"] is False
    assert detail["message"]["id"] == "m1"
    assert detail["message"]["body"] == "hello world"


def test_get_credentials_requires_auth_noninteractive(tmp_path: Path, monkeypatch) -> None:
    from blumkin.providers import google_auth

    monkeypatch.setenv("BLUMKIN_NONINTERACTIVE", "1")
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="Authentication required"):
        google_auth.get_credentials(cfg, allow_interactive=False)


def _cfg(config_dir: Path) -> BlumkinConfig:
    return BlumkinConfig(
        client_id="fake-google-desktop-client.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        files_scopes=False,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        provider=ProviderKind.GOOGLE,
        tenant_id="",
        wo1162425_scopes=False,
    )
