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
    _default_orderby,
    _validate_orderby,
    _well_known_folder,
    format_folders_human,
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


def test_mail_folders_reports_a_folder_count_cap(monkeypatch) -> None:
    """A partial tree must say so: the omitted ids cannot be discovered otherwise."""
    client = _client(monkeypatch)
    monkeypatch.setattr("blumkin.skills.mail._MAX_FOLDERS", 2)
    client.me.mail_folders.get = AsyncMock(
        return_value=_page([_folder(f"F{n}", f"id-{n}") for n in range(5)])
    )

    payload = asyncio.run(mail_folders())

    assert payload["truncated"] is True
    assert len(payload["folders"]) == 2
    assert "truncated" in format_folders_human(payload)[-1]


def test_mail_folders_reports_a_depth_cap(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr("blumkin.skills.mail._MAX_FOLDER_DEPTH", 0)
    client.me.mail_folders.get = AsyncMock(
        return_value=_page([_folder("Inbox", "inbox-id", child_folder_count=1)])
    )
    child = client.me.mail_folders.by_mail_folder_id.return_value.child_folders
    child.get = AsyncMock(return_value=_page([_folder("Deep", "deep-id")]))

    payload = asyncio.run(mail_folders())

    assert payload["truncated"] is True
    assert [item["name"] for item in payload["folders"]] == ["Inbox"]


def test_mail_folders_reports_no_truncation_for_a_complete_tree(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.mail_folders.get = AsyncMock(return_value=_page([_folder("Inbox", "inbox-id")]))

    payload = asyncio.run(mail_folders())

    assert payload["truncated"] is False
    assert payload["limits"] == {"max_depth": 6, "max_folders": 500}
    assert "truncated" not in " ".join(format_folders_human(payload))


def test_mail_list_not_found_mentions_a_truncated_listing(monkeypatch) -> None:
    """Otherwise the 'run mail folders' hint points at a command with the same caps."""
    client = _client(monkeypatch)
    monkeypatch.setattr("blumkin.skills.mail._MAX_FOLDERS", 1)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(side_effect=_odata_error(404, "ErrorItemNotFound"))
    client.me.mail_folders.get = AsyncMock(
        return_value=_page([_folder("Inbox", "inbox-id"), _folder("Deep", "deep-id")])
    )

    with pytest.raises(MailFolderNotFoundError, match="truncated"):
        asyncio.run(mail_list(folder="Deep"))


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

    payload = asyncio.run(mail_list(folder="sentitems"))

    assert payload["folder"] == "sentitems"
    assert payload["orderby"] == "sent"
    assert payload["items"][0]["sent"] == "2026-08-27T10:00Z"
    assert _query(messages.get).orderby == ["sentDateTime desc"]


def test_mail_list_drafts_order_by_created_date(monkeypatch) -> None:
    """Drafts were never sent, so sentDateTime is null on every row."""
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(
        return_value=_page([_message(created="2026-08-27T08:00Z", received=None)])
    )

    payload = asyncio.run(mail_list(folder="drafts"))

    assert payload["orderby"] == "created"
    assert payload["items"][0]["created"] == "2026-08-27T08:00Z"
    assert _query(messages.get).orderby == ["createdDateTime desc"]


def test_mail_list_outbox_orders_by_created_date(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(return_value=_page([]))

    payload = asyncio.run(mail_list(folder="outbox"))

    assert payload["orderby"] == "created"
    assert _query(messages.get).orderby == ["createdDateTime desc"]


def test_mail_list_prefers_a_real_folder_over_the_alias(monkeypatch) -> None:
    """A mailbox keeping both 'Sent Items' and a custom 'Sent' must reach its own folder."""
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(
        side_effect=[_odata_error(404, "ErrorItemNotFound"), _page([_message()])]
    )
    client.me.mail_folders.get = AsyncMock(return_value=_page([_folder("Sent", "custom-sent-id")]))

    payload = asyncio.run(mail_list(folder="Sent"))

    assert payload["folder"] == "custom-sent-id"
    assert payload["orderby"] == "received"
    assert client.me.mail_folders.by_mail_folder_id.call_args_list[-1].args == ("custom-sent-id",)


def test_mail_list_falls_back_to_the_alias_without_a_real_folder(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(side_effect=[_odata_error(404, "ErrorItemNotFound"), _page([])])
    client.me.mail_folders.get = AsyncMock(return_value=_page([_folder("Inbox", "inbox-id")]))

    payload = asyncio.run(mail_list(folder="trash"))

    assert payload["folder"] == "deleteditems"


def test_mail_list_reports_ambiguous_folder_names(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(side_effect=_odata_error(404, "ErrorItemNotFound"))
    client.me.mail_folders.get = AsyncMock(
        return_value=_page(
            [
                _folder("Receipts", "receipts-a", child_folder_count=1),
                _folder("Other", "other-id"),
            ]
        )
    )
    child = client.me.mail_folders.by_mail_folder_id.return_value.child_folders
    child.get = AsyncMock(return_value=_page([_folder("Receipts", "receipts-b")]))

    with pytest.raises(MailFolderNotFoundError, match="ambiguous"):
        asyncio.run(mail_list(folder="Receipts"))


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
    client.me.mail_folders.get = AsyncMock(return_value=_page([_folder("Inbox", "inbox-id")]))

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


def test_mail_list_does_not_blame_the_folder_for_query_errors(monkeypatch) -> None:
    """A --top above Graph's cap is a 400, but the folder is fine."""
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(side_effect=_odata_error(400, "invalidRequest"))

    with pytest.raises(ODataError):
        asyncio.run(mail_list(folder="inbox", top=1001))


def test_mail_list_treats_malformed_ids_as_folder_failures(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(side_effect=_odata_error(400, "ErrorInvalidIdMalformed"))
    client.me.mail_folders.get = AsyncMock(return_value=_page([]))

    with pytest.raises(MailFolderNotFoundError):
        asyncio.run(mail_list(folder="AAMkAD-bogus"))


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
        "outbound": True,
        "top": 10,
    }
    lines = format_list_human(payload)
    assert "sentitems (top 10, by sent): 1 message(s)" in lines[0]
    assert "to peer@example.com: Re: Sync" in lines[1]


def test_format_list_human_direction_follows_the_folder_not_the_sort() -> None:
    """--folder inbox --orderby created is still inbound mail."""
    inbound = {
        "folder": "inbox",
        "items": [
            {
                "created": "2026-08-27T09:00Z",
                "from_name": "Peer",
                "is_read": True,
                "subject": "Sync",
                "to_email": None,
            }
        ],
        "orderby": "created",
        "outbound": False,
        "top": 10,
    }
    assert "Peer: Sync" in format_list_human(inbound)[1]

    outbound = {
        "folder": "sentitems",
        "items": [
            {
                "is_read": True,
                "received": "2026-08-27T10:00Z",
                "subject": "Re: Sync",
                "to_email": "peer@example.com",
            }
        ],
        "orderby": "received",
        "outbound": True,
        "top": 10,
    }
    assert "to peer@example.com: Re: Sync" in format_list_human(outbound)[1]


def test_format_list_human_handles_an_empty_folder() -> None:
    payload = {"folder": "archive", "items": [], "orderby": "received", "top": 10}
    assert format_list_human(payload)[-1] == "  (none)"


def test_default_orderby_per_folder() -> None:
    assert _default_orderby(None) == "received"
    assert _default_orderby("inbox") == "received"
    assert _default_orderby("archive") == "received"
    assert _default_orderby("sentitems") == "sent"
    assert _default_orderby("drafts") == "created"
    assert _default_orderby("outbox") == "created"


def test_validate_orderby_normalizes_and_rejects() -> None:
    assert _validate_orderby("SENT") == "sent"
    assert _validate_orderby(" created ") == "created"
    with pytest.raises(ValueError, match="--orderby"):
        _validate_orderby("alphabetical")


def test_well_known_folder_matches_graph_names_only() -> None:
    assert _well_known_folder("Sent Items") == "sentitems"
    assert _well_known_folder("deleted-items") == "deleteditems"
    assert _well_known_folder("  inbox  ") == "inbox"
    assert _well_known_folder("JunkEmail") == "junkemail"
    # Aliases and ids are not well-known names: they go through folder resolution.
    assert _well_known_folder("sent") is None
    assert _well_known_folder("trash") is None
    assert _well_known_folder("AAMkAD00112233==") is None


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


def _message(
    *,
    created: str | None = None,
    received: str | None = "2026-08-27T09:00Z",
    sent: str | None = None,
) -> Any:
    return SimpleNamespace(
        body=None,
        body_preview="hi",
        created_date_time=created,
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
