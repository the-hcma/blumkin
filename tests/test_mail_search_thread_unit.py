"""Hermetic coverage for ``mail search`` + ``mail thread`` (issue #178)."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httplib2
import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig, PreferencesConfig
from blumkin.exit_codes import EXIT_NOT_FOUND
from blumkin.providers.google import mail as google_mail
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.mail import (
    MailMessageNotFoundError,
    format_search_human,
    format_thread_human,
    mail_search,
    mail_thread,
)


def _msg(mid: str, *, subject: str, folder_id: str | None = None, conv: str = "c1"):
    return SimpleNamespace(
        id=mid,
        subject=subject,
        body_preview="preview",
        body=SimpleNamespace(content="<p>preview</p>", content_type=None),
        conversation_id=conv,
        parent_folder_id=folder_id,
        has_attachments=False,
        is_read=True,
        importance=None,
        created_date_time="2026-08-01T09:00:00+00:00",
        sent_date_time="2026-08-01T09:00:00+00:00",
        received_date_time=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        from_=SimpleNamespace(email_address=SimpleNamespace(name="Sam", address="sam@example.com")),
        to_recipients=[],
    )


def _graph(monkeypatch, *, messages_get_return) -> MagicMock:
    client = MagicMock()
    client.me.messages.get = AsyncMock(return_value=messages_get_return)
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    return client


# --------------------------------------------------------------------------- Graph search


def test_graph_search_tags_each_hit_with_its_folder(monkeypatch) -> None:
    hits = SimpleNamespace(value=[_msg("m1", subject="renewal", folder_id="F1")])
    client = _graph(monkeypatch, messages_get_return=hits)
    client.me.mail_folders.by_mail_folder_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(display_name="Receipts")
    )
    payload = asyncio.run(mail_search(query="renewal"))
    assert payload["count"] == 1
    assert payload["items"][0]["folder"] == "Receipts"
    assert payload["query"] == "renewal"
    client.me.mail_folders.by_mail_folder_id.assert_called_once_with("F1")


def test_graph_search_filters_since_until_locally(monkeypatch) -> None:
    early = _msg("old", subject="old")
    early.received_date_time = datetime(2026, 7, 1, tzinfo=UTC)
    keep = _msg("keep", subject="keep")
    keep.received_date_time = datetime(2026, 8, 15, tzinfo=UTC)
    _graph(monkeypatch, messages_get_return=SimpleNamespace(value=[early, keep]))
    payload = asyncio.run(
        mail_search(
            query="x",
            since=datetime(2026, 8, 1, tzinfo=UTC),
            until=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    assert [i["id"] for i in payload["items"]] == ["keep"]


def test_graph_search_non_dated_complete_reflects_the_page_fill(monkeypatch) -> None:
    # A short page is exhaustive; a page filled to exactly --top is not.
    _graph(monkeypatch, messages_get_return=SimpleNamespace(value=[_msg("m1", subject="x")]))
    assert asyncio.run(mail_search(query="x", top=3))["complete"] is True

    full = [_msg(f"m{i}", subject="x") for i in range(3)]
    _graph(monkeypatch, messages_get_return=SimpleNamespace(value=full))
    assert asyncio.run(mail_search(query="x", top=3))["complete"] is False


def test_graph_search_rejects_empty_query(monkeypatch) -> None:
    _graph(monkeypatch, messages_get_return=SimpleNamespace(value=[]))
    with pytest.raises(ValueError, match="--query is required"):
        asyncio.run(mail_search(query="   "))


def test_graph_search_rejects_a_double_quote(monkeypatch) -> None:
    _graph(monkeypatch, messages_get_return=SimpleNamespace(value=[]))
    with pytest.raises(ValueError, match="double quote"):
        asyncio.run(mail_search(query='subject:"quarterly report"'))


def test_graph_search_drops_null_dated_messages_when_a_bound_is_set(monkeypatch) -> None:
    # A $search hit from Drafts has no receivedDateTime; it must not pass both bounds.
    draft = _msg("draft", subject="draft")
    draft.received_date_time = None
    draft.sent_date_time = None
    draft.created_date_time = None
    keep = _msg("keep", subject="keep")
    keep.received_date_time = datetime(2026, 8, 15, tzinfo=UTC)
    _graph(monkeypatch, messages_get_return=SimpleNamespace(value=[draft, keep]))
    payload = asyncio.run(mail_search(query="x", until=datetime(2026, 9, 1, tzinfo=UTC)))
    assert [i["id"] for i in payload["items"]] == ["keep"]


# --------------------------------------------------------------------------- Graph thread


def test_graph_thread_orders_the_conversation(monkeypatch) -> None:
    client = _graph(
        monkeypatch,
        messages_get_return=SimpleNamespace(
            value=[_msg("a", subject="Re: hi"), _msg("b", subject="Re: hi")]
        ),
    )
    client.me.messages.by_message_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(conversation_id="c1")
    )
    payload = asyncio.run(mail_thread(message_id="a"))
    assert payload["conversation_id"] == "c1"
    assert [i["id"] for i in payload["items"]] == ["a", "b"]
    posted = client.me.messages.get.await_args.args[0]
    assert "conversationId eq 'c1'" in posted.query_parameters.filter


def test_graph_thread_walks_pages(monkeypatch) -> None:
    p2 = SimpleNamespace(value=[_msg("c", subject="Re: hi")], odata_next_link=None)
    p1 = SimpleNamespace(value=[_msg("a", subject="Re: hi"), _msg("b", subject="Re: hi")])
    p1.odata_next_link = "next"
    client = MagicMock()
    client.me.messages.get = AsyncMock(return_value=p1)
    client.me.messages.with_url.return_value.get = AsyncMock(return_value=p2)
    client.me.messages.by_message_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(conversation_id="c1")
    )
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(default_tz="UTC", client_id="x"),
    )
    payload = asyncio.run(mail_thread(message_id="a"))
    assert [i["id"] for i in payload["items"]] == ["a", "b", "c"]


def test_graph_thread_full_merges_each_body(monkeypatch) -> None:
    client = _graph(
        monkeypatch, messages_get_return=SimpleNamespace(value=[_msg("a", subject="hi")])
    )
    client.me.messages.by_message_id.return_value.get = AsyncMock(
        return_value=SimpleNamespace(conversation_id="c1")
    )

    async def _fake_get(*, message_id, body_type, config):  # noqa: ANN001
        return {"message": {"body": f"body-of-{message_id}", "body_type": body_type}}

    monkeypatch.setattr("blumkin.skills.mail.mail_get", _fake_get)
    payload = asyncio.run(mail_thread(message_id="a", full=True, body_type="html"))
    assert payload["items"][0]["body"] == "body-of-a"
    assert payload["items"][0]["body_type"] == "html"


def test_graph_search_dated_overfetches_and_marks_incomplete(monkeypatch) -> None:
    from datetime import UTC, datetime

    msgs = [_msg(f"m{i}", subject="x") for i in range(5)]
    client = _graph(monkeypatch, messages_get_return=SimpleNamespace(value=msgs))
    payload = asyncio.run(mail_search(query="x", top=2, since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert payload["complete"] is None  # a date filter can't guarantee exhaustiveness
    assert payload["count"] == 2
    posted = client.me.messages.get.await_args.args[0]
    assert posted.query_parameters.top >= 60  # over-fetched a relevance window


def test_graph_thread_missing_message_is_not_found(monkeypatch) -> None:
    client = _graph(monkeypatch, messages_get_return=SimpleNamespace(value=[]))
    from msgraph.generated.models.o_data_errors.o_data_error import ODataError

    err = ODataError()
    err.response_status_code = 404
    client.me.messages.by_message_id.return_value.get = AsyncMock(side_effect=err)
    with pytest.raises(MailMessageNotFoundError):
        asyncio.run(mail_thread(message_id="nope"))


# --------------------------------------------------------------------------- Google


def _google_cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    oauth.write_text('{"installed": {"client_id": "id.apps.googleusercontent.com"}}')
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


def _gmsg(mid: str, *, subject: str, labels: list[str], thread: str = "t1") -> dict:
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": labels,
        "snippet": "snippet",
        "internalDate": "1754038800000",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sam <sam@example.com>"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Sat, 1 Aug 2026 09:00:00 +0000"},
            ]
        },
    }


def _google_patched(service: MagicMock):
    return patch.multiple(
        "blumkin.providers.google.mail",
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    )


def test_google_search_uses_q_and_tags_folder(tmp_path: Path) -> None:
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]
    }
    users.messages.return_value.get.return_value.execute.return_value = _gmsg(
        "m1", subject="renewal", labels=["SENT"]
    )
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_search(query="renewal from:dana")
        )
    assert users.messages.return_value.list.call_args.kwargs["q"] == "renewal from:dana"
    assert payload["items"][0]["folder"] == "Sent Items"


def test_google_search_archive_folder_label(tmp_path: Path) -> None:
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]
    }
    users.messages.return_value.get.return_value.execute.return_value = _gmsg(
        "m1", subject="x", labels=["CATEGORY_PERSONAL"]
    )
    with _google_patched(service):
        payload = asyncio.run(GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_search(query="x"))
    assert payload["items"][0]["folder"] == "Archive"


def test_google_thread_reads_the_gmail_thread(tmp_path: Path) -> None:
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.get.return_value.execute.return_value = {"threadId": "t9"}
    users.threads.return_value.get.return_value.execute.return_value = {
        "messages": [
            _gmsg("a", subject="hi", labels=["INBOX"], thread="t9"),
            _gmsg("b", subject="Re: hi", labels=["SENT"], thread="t9"),
        ]
    }
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_thread(message_id="a")
        )
    assert payload["conversation_id"] == "t9"
    assert [i["id"] for i in payload["items"]] == ["a", "b"]
    assert users.threads.return_value.get.call_args.kwargs["id"] == "t9"


def test_google_search_dated_query_and_truncation(tmp_path: Path) -> None:
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}],
        "nextPageToken": "more",
    }
    users.messages.return_value.get.return_value.execute.return_value = _gmsg(
        "m1", subject="invoice", labels=["INBOX"]
    )
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_search(
                query="invoice",
                since=datetime(2026, 8, 1, tzinfo=UTC),
                until=datetime(2026, 9, 1, tzinfo=UTC),
            )
        )
    q = users.messages.return_value.list.call_args.kwargs["q"]
    assert "after:" in q and "before:" in q
    assert payload["complete"] is False  # nextPageToken => truncated


def test_google_thread_full_extracts_each_body(tmp_path: Path) -> None:
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.get.return_value.execute.return_value = {"threadId": "t9"}
    full_msg = _gmsg("a", subject="hi", labels=["INBOX"], thread="t9")
    full_msg["payload"]["mimeType"] = "text/plain"
    full_msg["payload"]["body"] = {"data": base64.urlsafe_b64encode(b"the full body text").decode()}
    users.threads.return_value.get.return_value.execute.return_value = {"messages": [full_msg]}
    with _google_patched(service):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).mail_thread(
                message_id="a", full=True, body_type="text"
            )
        )
    assert users.threads.return_value.get.call_args.kwargs["format"] == "full"
    assert payload["items"][0]["body"] == "the full body text"
    assert payload["items"][0]["body_type"] == "text"


def test_google_thread_missing_message_is_not_found(tmp_path: Path) -> None:
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = (
        HttpError(httplib2.Response({"status": 404}), b"gone")
    )
    with _google_patched(service), pytest.raises(MailMessageNotFoundError):
        asyncio.run(google_mail.mail_thread(message_id="nope", config=_google_cfg(tmp_path)))


# --------------------------------------------------------------------------- formatter / CLI


def test_formatters() -> None:
    s = format_search_human(
        {"query": "q", "items": [{"received": "d", "subject": "S", "folder": "F"}]}
    )
    assert "search 'q'" in s[0]
    assert any("[F]" in line for line in s)
    t = format_thread_human({"conversation_id": "c", "items": [{"received": "d", "subject": "S"}]})
    assert "conversation c" in t[0]


def test_cli_search_and_thread_help() -> None:
    for verb in ("search", "thread"):
        out = CliRunner().invoke(main, ["mail", verb, "--help"]).output
        assert "--" in out


def test_cli_thread_not_found(monkeypatch) -> None:
    async def _raise(**_kwargs):
        raise MailMessageNotFoundError("message not found: e")

    monkeypatch.setattr("blumkin.cli._workspace", lambda: SimpleNamespace(mail_thread=_raise))
    result = CliRunner().invoke(main, ["mail", "thread", "--id", "e", "--json"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.stderr)["error"] == "not_found"
