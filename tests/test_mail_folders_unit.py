"""Folder-targeted mail reads (issue #47)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.skills.mail import (
    MailFolderNotFoundError,
    _resolve_mail_folder,
    _resolve_orderby,
    format_list_human,
    mail_folders,
    mail_inbox,
    mail_list,
)


def test_mail_folders_walks_nested_folders(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.mail_folders.get = AsyncMock(
        return_value=_page([_folder("Inbox", "inbox-id", child_folder_count=1)])
    )
    child_builder = client.me.mail_folders.by_mail_folder_id.return_value.child_folders
    child_builder.get = AsyncMock(return_value=_page([_folder("Receipts", "receipts-id")]))

    payload = asyncio.run(mail_folders())

    assert [item["path"] for item in payload["folders"]] == ["Inbox", "Inbox/Receipts"]
    assert payload["folders"][1]["id"] == "receipts-id"
    assert payload["folders"][0]["unread"] == 2
    client.me.mail_folders.by_mail_folder_id.assert_called_once_with("inbox-id")


def test_mail_folders_follows_pagination(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.mail_folders.get = AsyncMock(
        return_value=_page([_folder("Inbox", "inbox-id")], next_link="https://graph/next")
    )
    client.me.mail_folders.with_url.return_value.get = AsyncMock(
        return_value=_page([_folder("Archive", "archive-id")])
    )

    payload = asyncio.run(mail_folders())

    assert [item["name"] for item in payload["folders"]] == ["Inbox", "Archive"]
    client.me.mail_folders.with_url.assert_called_once_with("https://graph/next")


def test_mail_inbox_keeps_its_payload_shape(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([_message()]))

    payload = asyncio.run(mail_inbox(top=5))

    assert set(payload) == {"items", "top"}
    assert payload["top"] == 5
    client.me.mail_folders.by_mail_folder_id.assert_not_called()


def test_mail_list_defaults_to_the_whole_mailbox(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([_message()]))

    payload = asyncio.run(mail_list(top=3))

    assert payload["folder"] is None
    assert payload["orderby"] == "received"
    assert payload["items"][0]["id"] == "msg-1"
    client.me.mail_folders.by_mail_folder_id.assert_not_called()
    assert _query(client.me.messages.get).orderby == ["receivedDateTime desc"]


def test_mail_list_resolves_a_well_known_folder(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(return_value=_page([_message()]))

    payload = asyncio.run(mail_list(folder="sentitems"))

    assert payload["folder"] == "sentitems"
    client.me.mail_folders.by_mail_folder_id.assert_called_once_with("sentitems")


def test_mail_list_resolves_a_raw_folder_id(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(return_value=_page([]))

    payload = asyncio.run(mail_list(folder="AAMkAD00112233=="))

    assert payload["folder"] == "AAMkAD00112233=="
    client.me.mail_folders.by_mail_folder_id.assert_called_once_with("AAMkAD00112233==")


def test_mail_list_sent_folder_orders_by_sent_date(monkeypatch) -> None:
    """receivedDateTime is null on Sent Items, so the default ordering must switch."""
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(
        return_value=_page([_message(received=None, sent="2026-08-27T10:00Z")])
    )

    payload = asyncio.run(mail_list(folder="sent"))

    assert payload["folder"] == "sentitems"
    assert payload["orderby"] == "sent"
    assert payload["items"][0]["sent"] == "2026-08-27T10:00Z"
    assert _query(messages.get).orderby == ["sentDateTime desc"]


def test_mail_list_explicit_orderby_overrides_the_folder_default(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(return_value=_page([]))

    payload = asyncio.run(mail_list(folder="sentitems", orderby="received"))

    assert payload["orderby"] == "received"
    assert _query(messages.get).orderby == ["receivedDateTime desc"]


def test_mail_list_rejects_a_bad_orderby() -> None:
    with pytest.raises(ValueError, match="--orderby"):
        asyncio.run(mail_list(orderby="alphabetical"))


def test_mail_list_rejects_an_empty_folder() -> None:
    with pytest.raises(ValueError, match="--folder"):
        asyncio.run(mail_list(folder="   "))


def test_mail_list_rejects_a_bad_top() -> None:
    with pytest.raises(ValueError, match="--top"):
        asyncio.run(mail_list(top=0))


def test_mail_list_maps_unknown_folder_to_a_clear_error(monkeypatch) -> None:
    """An unresolvable folder should not surface as a raw Graph exception."""
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(side_effect=_odata_error(404, "ErrorItemNotFound"))

    with pytest.raises(MailFolderNotFoundError) as excinfo:
        asyncio.run(mail_list(folder="notafolder"))

    message = str(excinfo.value)
    assert "notafolder" in message
    assert "sentitems" in message
    assert "blumkin mail folders" in message


def test_mail_list_reraises_unrelated_graph_errors(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    error = _odata_error(500, "InternalServerError")
    messages.get = AsyncMock(side_effect=error)

    with pytest.raises(ODataError):
        asyncio.run(mail_list(folder="archive"))


def test_format_list_human_shows_recipients_for_sent_mail() -> None:
    payload = {
        "folder": "sentitems",
        "items": [
            {
                "is_read": True,
                "sent": "2026-08-27T10:00Z",
                "subject": "Re: Sync",
                "to_email": "peer@example.com",
            }
        ],
        "orderby": "sent",
        "top": 10,
    }
    lines = format_list_human(payload)
    assert "sentitems (top 10, by sent): 1 message(s)" in lines[0]
    assert "to peer@example.com: Re: Sync" in lines[1]


def test_format_list_human_handles_an_empty_folder() -> None:
    payload = {"folder": "archive", "items": [], "orderby": "received", "top": 10}
    assert format_list_human(payload)[-1] == "  (none)"


def test_resolve_mail_folder_accepts_aliases_and_spellings() -> None:
    assert _resolve_mail_folder("sent") == "sentitems"
    assert _resolve_mail_folder("Sent Items") == "sentitems"
    assert _resolve_mail_folder("deleted-items") == "deleteditems"
    assert _resolve_mail_folder("trash") == "deleteditems"
    assert _resolve_mail_folder("JunkEmail") == "junkemail"
    assert _resolve_mail_folder("  inbox  ") == "inbox"
    assert _resolve_mail_folder(None) is None


def test_resolve_mail_folder_passes_ids_through_untouched() -> None:
    assert _resolve_mail_folder("AAMkAD00112233==") == "AAMkAD00112233=="


def test_resolve_orderby_defaults_per_folder() -> None:
    assert _resolve_orderby(None, None) == "received"
    assert _resolve_orderby(None, "inbox") == "received"
    assert _resolve_orderby(None, "sentitems") == "sent"
    assert _resolve_orderby(None, "drafts") == "sent"
    assert _resolve_orderby("SENT", "inbox") == "sent"


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
    return client


def _folder(name: str, folder_id: str, *, child_folder_count: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        child_folder_count=child_folder_count,
        display_name=name,
        id=folder_id,
        total_item_count=7,
        unread_item_count=2,
    )


def _message(*, received: str | None = "2026-08-27T09:00Z", sent: str | None = None) -> Any:
    return SimpleNamespace(
        body=None,
        body_preview="hi",
        from_=None,
        has_attachments=False,
        id="msg-1",
        is_read=True,
        received_date_time=received,
        sent_date_time=sent,
        subject="Sync",
        to_recipients=None,
    )


def _odata_error(status: int, code: str) -> ODataError:
    error = ODataError()
    error.response_status_code = status
    error.error = MainError(code=code, message=code)
    return error


def _page(value: list[Any], *, next_link: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(odata_next_link=next_link, value=value)


def _query(get_mock: AsyncMock) -> Any:
    await_args = get_mock.await_args
    assert await_args is not None
    return await_args.args[0].query_parameters
