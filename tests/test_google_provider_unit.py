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


def test_people_resolve_unsupported(tmp_path: Path) -> None:
    provider = GoogleWorkspaceProvider(_cfg(tmp_path))
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
        patch("blumkin.providers.google.calendar.build_api_service", return_value=service),
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
        patch("blumkin.providers.google.calendar.build_api_service", return_value=service),
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
    assert suggest["treat_tentative"] == "busy"
    assert suggest["with"] == ["ada@example.com"]


def test_calendar_suggest_rejects_treat_tentative_free(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    start = datetime(2026, 8, 30, 13, 0, tzinfo=UTC)
    end = datetime(2026, 8, 30, 17, 0, tzinfo=UTC)
    provider = GoogleWorkspaceProvider(cfg)
    with pytest.raises(ValueError, match="treat-tentative free"):
        asyncio.run(
            provider.calendar_suggest(
                with_emails=["ada@example.com"],
                start=start,
                end=end,
                duration=timedelta(minutes=30),
                treat_tentative="free",
            )
        )


def test_mail_get_reports_has_attachments_from_parts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    full = {
        "id": "m2",
        "snippet": "see attached",
        "threadId": "t2",
        "labelIds": ["INBOX"],
        "internalDate": "1725024000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Ada <ada@example.com>"},
                {"name": "Subject", "value": "Files"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Fri, 30 Aug 2024 12:00:00 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"data": "c2VlIGF0dGFjaGVk"},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "body": {"attachmentId": "ATT123", "size": 12},
                },
            ],
        },
    }
    messages = MagicMock()
    messages.get.return_value.execute.return_value = full
    service = MagicMock()
    service.users.return_value.messages.return_value = messages
    with (
        patch("blumkin.providers.google.mail.get_credentials", return_value=MagicMock()),
        patch("blumkin.providers.google.mail.build_api_service", return_value=service),
    ):
        provider = GoogleWorkspaceProvider(cfg)
        detail = asyncio.run(provider.mail_get(message_id="m2", body_type="text"))
    assert detail["message"]["has_attachments"] is True
    assert detail["message"]["attachments"] == []


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
                {"name": "Cc", "value": "Bob <bob@example.com>"},
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
        patch("blumkin.providers.google.mail.build_api_service", return_value=service),
    ):
        provider = GoogleWorkspaceProvider(cfg)
        inbox = asyncio.run(provider.mail_inbox(top=5))
        detail = asyncio.run(provider.mail_get(message_id="m1", body_type="text"))
    assert inbox["top"] == 5
    assert inbox["items"][0]["subject"] == "Hello"
    assert inbox["items"][0]["from_email"] == "ada@example.com"
    assert inbox["items"][0]["is_read"] is False
    assert inbox["items"][0]["received"] == "2024-08-30T13:20:00+00:00"
    assert inbox["items"][0]["created"] == "2024-08-30T13:20:00+00:00"
    assert inbox["items"][0]["sent"] == "2024-08-30T12:00:00+00:00"
    assert inbox["filters"]["complete"] is True
    assert inbox["filters"]["scanned"] == 1
    assert inbox["orderby"] is None
    assert detail["message"]["id"] == "m1"
    assert detail["message"]["body"] == "hello world"
    assert detail["message"]["received"] == "2024-08-30T13:20:00+00:00"
    assert detail["message"]["sent"] == "2024-08-30T12:00:00+00:00"
    assert detail["message"]["cc"] == [{"email": "bob@example.com", "name": "Bob"}]


def test_mail_list_rejects_orderby(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    provider = GoogleWorkspaceProvider(cfg)
    with pytest.raises(ValueError, match="--orderby is not supported"):
        asyncio.run(provider.mail_list(folder="inbox", orderby="sent", top=5))


def test_mail_list_rejects_top_above_gmail_limit(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    provider = GoogleWorkspaceProvider(cfg)
    with pytest.raises(ValueError, match="--top must be <= 500"):
        asyncio.run(provider.mail_list(folder="inbox", top=501))


def test_mail_list_does_not_claim_complete_when_page_truncated(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    list_body = {"messages": [{"id": "m1"}], "nextPageToken": "page2"}
    meta = {
        "id": "m1",
        "snippet": "hello",
        "labelIds": ["INBOX"],
        "internalDate": "1725024000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Ada <ada@example.com>"},
                {"name": "Subject", "value": "Hello"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Fri, 30 Aug 2024 11:00:00 +0000"},
            ]
        },
    }
    messages = MagicMock()
    messages.list.return_value.execute.return_value = list_body
    messages.get.return_value.execute.return_value = meta
    service = MagicMock()
    service.users.return_value.messages.return_value = messages
    with (
        patch("blumkin.providers.google.mail.get_credentials", return_value=MagicMock()),
        patch("blumkin.providers.google.mail.build_api_service", return_value=service),
    ):
        payload = asyncio.run(GoogleWorkspaceProvider(cfg).mail_list(folder="inbox", top=1))
    assert payload["filters"]["complete"] is None
    assert payload["filters"]["scanned"] is None
    assert payload["items"][0]["received"] == "2024-08-30T13:20:00+00:00"
    assert payload["items"][0]["sent"] == "2024-08-30T11:00:00+00:00"


def test_auth_status_sets_auth_record_when_token_present(tmp_path: Path) -> None:
    from blumkin.providers import google_auth

    cfg = _cfg(tmp_path)
    status = google_auth.status_dict(cfg)
    assert status["token_cache"] is False
    assert status["auth_record"] is False
    cfg.google_token_path.write_text(
        "{"
        '"token": "x", "refresh_token": "y", "token_uri": "https://oauth2.googleapis.com/token", '
        '"client_id": "c", "client_secret": "", "scopes": []'
        "}"
    )
    status = google_auth.status_dict(cfg)
    assert status["token_cache"] is True
    assert status["auth_record"] is True


def test_build_gmail_query_quotes_multiword_subject_and_sender() -> None:
    from blumkin.providers.google.mail import _build_gmail_query

    q = _build_gmail_query(
        search=None,
        sender="Ada Lovelace <ada@example.com>",
        since=None,
        subject="Release notes",
        unread=False,
        until=None,
    )
    assert q == 'from:"Ada Lovelace <ada@example.com>" subject:"Release notes"'


def test_client_config_reads_desktop_oauth_file(tmp_path: Path) -> None:
    from blumkin.providers import google_auth

    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {'
        '"client_id": "fake-google-desktop-client.apps.googleusercontent.com", '
        '"client_secret": "fake-google-client-secret"}}'
    )
    cfg = _cfg(tmp_path, oauth_file=oauth)
    installed = google_auth._client_config(cfg)["installed"]
    assert installed["client_secret"] == "fake-google-client-secret"
    assert installed["client_id"] == "fake-google-desktop-client.apps.googleusercontent.com"


def test_get_credentials_requires_auth_noninteractive(tmp_path: Path, monkeypatch) -> None:
    from blumkin.providers import google_auth

    monkeypatch.setenv("BLUMKIN_NONINTERACTIVE", "1")
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="Authentication required"):
        google_auth.get_credentials(cfg, allow_interactive=False)


def test_load_credentials_prefers_oauth_file_client_secret(tmp_path: Path) -> None:
    from blumkin.providers import google_auth

    oauth = tmp_path / "desktop-client.json"
    oauth.write_text(
        '{"installed": {'
        '"client_id": "fake-google-desktop-client.apps.googleusercontent.com", '
        '"client_secret": "rotated-desktop-secret"}}'
    )
    cfg = _cfg(tmp_path, oauth_file=oauth)
    cfg.google_token_path.write_text(
        "{"
        '"client_id": "fake-google-desktop-client.apps.googleusercontent.com", '
        '"client_secret": "stale-token-secret", '
        '"refresh_token": "fake-refresh", '
        '"token": "fake-access", '
        '"token_uri": "https://oauth2.googleapis.com/token", '
        '"scopes": []'
        "}"
    )
    captured: dict = {}

    def _capture(info, scopes=None):
        captured.clear()
        captured.update(info)
        creds = MagicMock()
        creds.valid = True
        return creds

    with patch.object(google_auth.Credentials, "from_authorized_user_info", side_effect=_capture):
        creds = google_auth._load_credentials(cfg)
    assert creds is not None
    assert captured["client_secret"] == "rotated-desktop-secret"


def _cfg(config_dir: Path, *, oauth_file: Path | None = None) -> BlumkinConfig:
    path = oauth_file
    if path is None:
        path = config_dir / "desktop-client.json"
        if not path.is_file():
            path.write_text(
                '{"installed": {'
                '"client_id": "fake-google-desktop-client.apps.googleusercontent.com", '
                '"client_secret": "fake-google-client-secret"}}'
            )
    return BlumkinConfig(
        client_id="fake-google-desktop-client.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        files_scopes=False,
        google_oauth_client_file=path,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        provider=ProviderKind.GOOGLE,
        tenant_id="",
        wo1162425_scopes=False,
    )
