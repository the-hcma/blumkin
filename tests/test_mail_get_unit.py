"""Single-message reads (issue #55, item 1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

from blumkin.skills.mail import (
    MailMessageNotFoundError,
    format_get_human,
    mail_get,
)


def test_mail_get_asks_graph_to_convert_the_body(monkeypatch) -> None:
    """Outlook's own text rendering beats stripping tags out of HTML locally."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_message(body="plain words", body_type=BodyType.Text))

    payload = asyncio.run(mail_get(message_id="msg-1"))

    assert payload["message"]["body"] == "plain words"
    assert payload["message"]["body_type"] == "text"
    assert _headers(item.get).get("prefer") == {'outlook.body-content-type="text"'}


def test_mail_get_converts_locally_when_graph_ignores_the_preference(monkeypatch) -> None:
    """The response says what it actually sent, so honour that over what we asked for."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(
        return_value=_message(body="<p>Hi <b>there</b></p>", body_type=BodyType.Html)
    )

    payload = asyncio.run(mail_get(message_id="msg-1"))

    assert payload["message"]["body"] == "Hi there"
    assert payload["message"]["body_type"] == "text"


def test_mail_get_labels_a_text_body_as_text_even_when_html_was_asked_for(monkeypatch) -> None:
    """The label must follow the body: claiming HTML over plain text misleads a parser."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_message(body="plain words", body_type=BodyType.Text))

    payload = asyncio.run(mail_get(message_id="msg-1", body_type="html"))

    assert payload["message"]["body"] == "plain words"
    assert payload["message"]["body_type"] == "text"


def test_mail_get_returns_html_untouched_when_asked(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_message(body="<p>Hi</p>", body_type=BodyType.Html))

    payload = asyncio.run(mail_get(message_id="msg-1", body_type="html"))

    assert payload["message"]["body"] == "<p>Hi</p>"
    assert payload["message"]["body_type"] == "html"
    assert _headers(item.get).get("prefer") == {'outlook.body-content-type="html"'}


def test_mail_get_reports_participants_and_timestamps(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_message())

    message = asyncio.run(mail_get(message_id="msg-1"))["message"]

    assert message["from_name"] == "Rebecca Doe"
    assert message["from_email"] == "rebecca@example.com"
    assert message["to"] == [{"email": "me@example.com", "name": "Me"}]
    assert message["cc"] == [{"email": "cc@example.com", "name": None}]
    assert message["received"] == "2026-08-27T09:00Z"
    assert message["conversation_id"] == "conv-1"
    assert message["is_read"] is True
    assert message["is_draft"] is False


def test_mail_get_skips_the_attachment_call_without_attachments(monkeypatch) -> None:
    """A second round-trip per read is worth avoiding when Graph already said none."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_message(has_attachments=False))
    item.attachments.get = AsyncMock(return_value=_page([]))

    payload = asyncio.run(mail_get(message_id="msg-1"))

    assert payload["message"]["attachments"] == []
    item.attachments.get.assert_not_awaited()


def test_mail_get_summarizes_attachments(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(return_value=_message(has_attachments=True))
    item.attachments.get = AsyncMock(return_value=_page([_attachment()]))

    attachments = asyncio.run(mail_get(message_id="msg-1"))["message"]["attachments"]

    assert [item["name"] for item in attachments] == ["report.pdf"]
    assert attachments[0]["size"] == 1024


def test_mail_get_rejects_an_empty_id() -> None:
    with pytest.raises(ValueError, match="--id"):
        asyncio.run(mail_get(message_id="   "))


def test_mail_get_reports_a_malformed_id_as_not_found(monkeypatch) -> None:
    """Graph answers a bad id with 400, which would otherwise surface as a graph error."""
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(side_effect=_odata_error(400, "ErrorInvalidIdMalformed"))

    with pytest.raises(MailMessageNotFoundError, match="not-a-real-id"):
        asyncio.run(mail_get(message_id="not-a-real-id"))


def test_mail_get_lets_query_errors_stay_graph_errors(monkeypatch) -> None:
    client = _client(monkeypatch)
    item = client.me.messages.by_message_id.return_value
    item.get = AsyncMock(side_effect=_odata_error(400, "invalidRequest"))

    with pytest.raises(ODataError):
        asyncio.run(mail_get(message_id="msg-1"))


def test_mail_get_reports_a_missing_message_as_not_found(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.by_message_id.return_value.get = AsyncMock(return_value=None)

    with pytest.raises(MailMessageNotFoundError, match="msg-gone"):
        asyncio.run(mail_get(message_id="msg-gone"))


def test_format_get_human_leads_with_the_subject_then_the_body() -> None:
    lines = format_get_human(
        {
            "message": {
                "attachments": [{"id": "att-1", "name": "report.pdf", "size": 1024}],
                "body": "first line\nsecond line",
                "cc": [],
                "from_email": "rebecca@example.com",
                "from_name": "Rebecca Doe",
                "is_read": False,
                "received": "2026-08-27T09:00Z",
                "subject": "Quarterly sync",
                "to": [{"email": "me@example.com", "name": "Me"}],
            }
        }
    )

    assert lines[0] == "Quarterly sync"
    assert "  from: Rebecca Doe <rebecca@example.com>" in lines
    assert "  to: Me <me@example.com>" in lines
    assert "  flags: unread" in lines
    assert any("report.pdf" in line for line in lines)
    assert lines[-2:] == ["first line", "second line"]


def test_format_get_human_survives_a_bare_message() -> None:
    lines = format_get_human({"message": {}})

    assert lines[0] == "(no subject)"
    assert "  from: (unknown sender)" in lines
    assert "  date: (no date)" in lines
    assert lines[-1] == "(no body)"


def _attachment() -> SimpleNamespace:
    return SimpleNamespace(
        content_type="application/pdf",
        id="att-1",
        is_inline=False,
        name="report.pdf",
        odata_type="#microsoft.graph.fileAttachment",
        size=1024,
    )


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
    return client


def _headers(get_mock: AsyncMock) -> dict[str, set[str]]:
    """Header names come back normalized to lower case by kiota's collection."""
    await_args = get_mock.await_args
    assert await_args is not None
    collection = await_args.args[0].headers
    return {name: collection.get(name) for name in collection.keys()}


def _message(
    *,
    body: str | None = "hello",
    body_type: BodyType = BodyType.Text,
    has_attachments: bool = False,
) -> Any:
    return SimpleNamespace(
        body=None if body is None else SimpleNamespace(content=body, content_type=body_type),
        body_preview="hello",
        cc_recipients=[_recipient("cc@example.com", None)],
        conversation_id="conv-1",
        created_date_time="2026-08-27T08:59Z",
        from_=_recipient("rebecca@example.com", "Rebecca Doe"),
        has_attachments=has_attachments,
        id="msg-1",
        internet_message_id="<abc@example.com>",
        is_draft=False,
        is_read=True,
        received_date_time="2026-08-27T09:00Z",
        sent_date_time="2026-08-27T08:58Z",
        subject="Quarterly sync",
        to_recipients=[_recipient("me@example.com", "Me")],
        web_link="https://outlook.example.com/msg-1",
    )


def _odata_error(status: int, code: str) -> ODataError:
    error = ODataError()
    error.response_status_code = status
    error.error = MainError(code=code, message=code)
    return error


def _page(value: list[Any], *, next_link: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(odata_next_link=next_link, value=value)


def _recipient(address: str, name: str | None) -> SimpleNamespace:
    return SimpleNamespace(email_address=SimpleNamespace(address=address, name=name))
