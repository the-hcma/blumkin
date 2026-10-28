"""Google `mail_new`: Gmail history for new inbox mail (issue #525)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.mail_sync import MailSyncResetError
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind

_GOOGLE_MAIL = "blumkin.providers.google.mail"
_SINCE = datetime(2026, 10, 28, 4, 30, tzinfo=UTC)


def _cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    oauth.write_text(
        '{"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s"}}'
    )
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="UTC",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        mail_signature=MailSignatureConfig(),
        preferences=PreferencesConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def _message(mid: str) -> dict[str, Any]:
    return {
        "id": mid,
        "labelIds": ["INBOX"],
        "internalDate": "1793161800000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Me <me@example.com>"},
                {"name": "Subject", "value": f"Receipt {mid}"},
            ]
        },
    }


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}", uri="x")


def _history_entry(*ids: str, labels: tuple[str, ...] = ("INBOX",)) -> dict[str, Any]:
    return {"messagesAdded": [{"message": {"id": i, "labelIds": list(labels)}} for i in ids]}


class _Service:
    """A Gmail service double: profile, history pages and message metadata."""

    def __init__(self) -> None:
        self.profile: dict[str, Any] = {"historyId": "100"}
        self.history_pages: list[Any] = []
        self.messages: dict[str, Any] = {}
        self.history_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        users = MagicMock()
        users.getProfile.side_effect = lambda **kw: SimpleNamespace(
            execute=lambda num_retries=0: self.profile
        )

        def history_list(**kwargs: Any) -> Any:
            self.history_calls.append(kwargs)
            page = self.history_pages.pop(0)

            def run(num_retries: int = 0) -> Any:
                if isinstance(page, Exception):
                    raise page
                return page

            return SimpleNamespace(execute=run)

        def get(**kwargs: Any) -> Any:
            self.get_calls.append(kwargs["id"])

            def run(num_retries: int = 0) -> Any:
                value = self.messages[kwargs["id"]]
                if isinstance(value, Exception):
                    raise value
                return value

            return SimpleNamespace(execute=run)

        users.history.return_value.list.side_effect = history_list
        users.messages.return_value.get.side_effect = get
        self.service = MagicMock()
        self.service.users.return_value = users


def _run(fake: _Service, tmp_path: Path, *, state: str | None) -> dict[str, Any]:
    with patch(f"{_GOOGLE_MAIL}._gmail_service", return_value=fake.service):
        return asyncio.run(
            GoogleWorkspaceProvider(_cfg(tmp_path)).mail_new(state=state, since=_SINCE, top=25)
        )


def test_the_first_read_takes_the_current_history_id_and_lists_nothing(tmp_path: Path) -> None:
    fake = _Service()
    payload = _run(fake, tmp_path, state=None)
    assert payload == {"items": [], "state": "100"}
    assert fake.history_calls == [] and fake.get_calls == []


def test_a_later_read_returns_each_new_inbox_message_once_across_pages(tmp_path: Path) -> None:
    fake = _Service()
    fake.history_pages = [
        {
            "history": [_history_entry("m1", "m2"), _history_entry("m2")],
            "historyId": "110",
            "nextPageToken": "p2",
        },
        {"history": [_history_entry("m3")], "historyId": "120"},
    ]
    fake.messages = {mid: _message(mid) for mid in ("m1", "m2", "m3")}
    payload = _run(fake, tmp_path, state="100")
    assert [i["id"] for i in payload["items"]] == ["m1", "m2", "m3"]
    assert payload["items"][0]["from_email"] == "me@example.com"
    assert payload["state"] == "120"
    assert fake.get_calls == ["m1", "m2", "m3"]
    first, second = fake.history_calls
    assert first["startHistoryId"] == "100"
    assert first["historyTypes"] == ["messageAdded"]
    assert first["labelId"] == "INBOX"
    assert "pageToken" not in first and second["pageToken"] == "p2"


def test_a_read_with_no_changes_lists_nothing_and_moves_the_state(tmp_path: Path) -> None:
    fake = _Service()
    fake.history_pages = [{"historyId": "105"}]
    payload = _run(fake, tmp_path, state="100")
    assert payload == {"items": [], "state": "105"}
    assert fake.get_calls == []


def test_a_message_that_was_deleted_before_it_could_be_read_is_skipped(tmp_path: Path) -> None:
    fake = _Service()
    fake.history_pages = [{"history": [_history_entry("m1", "m2")], "historyId": "110"}]
    fake.messages = {"m1": _http_error(404), "m2": _message("m2")}
    payload = _run(fake, tmp_path, state="100")
    assert [i["id"] for i in payload["items"]] == ["m2"]


def test_an_entry_that_is_not_in_the_inbox_is_skipped(tmp_path: Path) -> None:
    fake = _Service()
    fake.history_pages = [
        {
            "history": [_history_entry("m1", labels=("SENT",)), _history_entry("m2")],
            "historyId": "110",
        }
    ]
    fake.messages = {"m2": _message("m2")}
    payload = _run(fake, tmp_path, state="100")
    assert [i["id"] for i in payload["items"]] == ["m2"]


def test_a_stale_history_id_is_a_reset(tmp_path: Path) -> None:
    fake = _Service()
    fake.history_pages = [_http_error(404)]
    with pytest.raises(MailSyncResetError):
        _run(fake, tmp_path, state="1")


def test_other_gmail_errors_are_not_swallowed(tmp_path: Path) -> None:
    fake = _Service()
    fake.history_pages = [_http_error(500)]
    with pytest.raises(HttpError):
        _run(fake, tmp_path, state="1")


def test_a_profile_without_a_history_id_cannot_start_a_sync(tmp_path: Path) -> None:
    fake = _Service()
    fake.profile = {}
    with pytest.raises(MailSyncResetError):
        _run(fake, tmp_path, state=None)


def test_a_history_that_never_ends_is_a_reset(tmp_path: Path) -> None:
    fake = _Service()
    fake.history_pages = [
        {"history": [_history_entry("m1")], "historyId": "1", "nextPageToken": f"p{n}"}
        for n in range(30)
    ]
    fake.messages = {"m1": _message("m1")}
    with pytest.raises(MailSyncResetError):
        _run(fake, tmp_path, state="1")
