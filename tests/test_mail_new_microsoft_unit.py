"""Microsoft `mail_new`: the Graph delta query for new inbox mail (issue #525)."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.message import Message
from msgraph.generated.models.o_data_errors.main_error import MainError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.users.item.mail_folders.item.messages.delta.delta_get_response import (
    DeltaGetResponse,
)

from blumkin.mail_sync import MailSyncResetError
from blumkin.skills.mail import mail_new

_SINCE = datetime(2026, 10, 28, 4, 30, tzinfo=UTC)
_BASE = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"


def _sync(fn: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(fn(*args, **kwargs))

    return wrapper


def _message(n: int, *, removed: bool = False) -> Message:
    if removed:
        return Message(id=f"gone-{n}", additional_data={"@removed": {"reason": "deleted"}})
    return Message(
        id=f"msg-{n}",
        subject=f"Receipt {n}",
        from_=Recipient(email_address=EmailAddress(address="me@example.com", name="Me")),
    )


def _page(*messages: Message, next_link: str | None = None, delta_link: str | None = None):
    return DeltaGetResponse(
        value=list(messages), odata_next_link=next_link, odata_delta_link=delta_link
    )


class _Delta:
    def __init__(self, pages: Mapping[Any, Any], calls: list[Any], key: str | None = None):
        self._pages, self._calls, self._key = pages, calls, key

    def with_url(self, url: str) -> _Delta:
        self._calls.append(("with_url", url))
        return _Delta(self._pages, self._calls, url)

    async def get(self, request_configuration: Any = None) -> Any:
        self._calls.append(("get", self._key, request_configuration))
        page = self._pages[self._key]
        if isinstance(page, Exception):
            raise page
        return page


def _client(pages: Mapping[Any, Any], calls: list[Any]) -> Any:
    folders: list[str] = []

    def by_id(folder_id: str) -> Any:
        folders.append(folder_id)
        return SimpleNamespace(messages=SimpleNamespace(delta=_Delta(pages, calls)))

    client = SimpleNamespace(
        me=SimpleNamespace(mail_folders=SimpleNamespace(by_mail_folder_id=by_id))
    )
    client.folders = folders  # type: ignore[attr-defined]
    return client


async def _run(pages: Mapping[Any, Any], *, state: str | None, calls: list[Any]):
    client = _client(pages, calls)
    from unittest.mock import patch

    with patch("blumkin.skills.mail.create_graph_client", return_value=client):
        payload = await mail_new(
            state=state, since=_SINCE, top=25, config=cast(Any, SimpleNamespace())
        )
    return payload, client


@_sync
async def test_the_first_read_asks_for_created_inbox_mail_since_then_and_returns_a_token() -> None:
    calls: list[Any] = []
    pages = {None: _page(_message(1), delta_link=f"{_BASE}?$deltatoken=abc")}
    payload, client = await _run(pages, state=None, calls=calls)
    assert client.folders == ["inbox"]
    assert [i["id"] for i in payload["items"]] == ["msg-1"]
    assert payload["items"][0]["from_email"] == "me@example.com"
    assert payload["state"] == f"{_BASE}?$deltatoken=abc"
    (_, key, config) = calls[0]
    assert key is None
    query = config.query_parameters
    assert query.change_type == "created"
    assert query.filter == "receivedDateTime ge 2026-10-28T04:30:00Z"
    assert query.orderby == ["receivedDateTime desc"]
    assert "id" in query.select and "from" in query.select and "subject" in query.select
    assert query.top is None


@_sync
async def test_next_links_are_followed_until_the_delta_link() -> None:
    calls: list[Any] = []
    nxt = f"{_BASE}?$skiptoken=2"
    pages = {
        None: _page(_message(1), next_link=nxt),
        nxt: _page(_message(2), delta_link=f"{_BASE}?$deltatoken=xyz"),
    }
    payload, _ = await _run(pages, state=None, calls=calls)
    assert [i["id"] for i in payload["items"]] == ["msg-1", "msg-2"]
    assert payload["state"].endswith("deltatoken=xyz")
    assert ("with_url", nxt) in calls


@_sync
async def test_a_later_read_continues_from_the_saved_link_without_a_filter() -> None:
    calls: list[Any] = []
    saved = f"{_BASE}?$deltatoken=abc"
    pages = {saved: _page(_message(3), delta_link=f"{_BASE}?$deltatoken=def")}
    payload, _ = await _run(pages, state=saved, calls=calls)
    assert [i["id"] for i in payload["items"]] == ["msg-3"]
    assert payload["state"].endswith("deltatoken=def")
    assert calls[0] == ("with_url", saved)
    assert calls[1][2] is None


@_sync
async def test_entries_marked_removed_are_skipped() -> None:
    pages = {None: _page(_message(1), _message(2, removed=True), delta_link=f"{_BASE}?t=1")}
    payload, _ = await _run(pages, state=None, calls=[])
    assert [i["id"] for i in payload["items"]] == ["msg-1"]


@pytest.mark.parametrize(
    "state",
    [
        "http://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?t=1",
        "https://evil.example/v1.0/me/mailFolders/inbox/messages/delta?t=1",
        "https://graph.microsoft.com.evil.example/v1.0/me/messages/delta?t=1",
        "https://user:pw@graph.microsoft.com/v1.0/me/messages/delta?t=1",
        "https://graph.microsoft.com/v1.0/me/messages",
        "not a url",
    ],
)
@_sync
async def test_a_saved_link_that_is_not_a_graph_delta_url_is_a_reset_and_sends_nothing(
    state: str,
) -> None:
    calls: list[Any] = []
    with pytest.raises(MailSyncResetError):
        await _run({}, state=state, calls=calls)
    assert calls == []


def _odata(code: str, status: int) -> ODataError:
    error = ODataError()
    error.response_status_code = status
    error.error = MainError(code=code)
    return error


@pytest.mark.parametrize(
    ("code", "status"), [("SyncStateNotFound", 410), ("resyncRequired", 410), ("Gone", 410)]
)
@_sync
async def test_an_expired_delta_state_is_a_reset(code: str, status: int) -> None:
    saved = f"{_BASE}?$deltatoken=old"
    with pytest.raises(MailSyncResetError):
        await _run({saved: _odata(code, status)}, state=saved, calls=[])


@_sync
async def test_other_graph_errors_are_not_swallowed() -> None:
    saved = f"{_BASE}?$deltatoken=old"
    with pytest.raises(ODataError):
        await _run({saved: _odata("ErrorAccessDenied", 403)}, state=saved, calls=[])


@_sync
async def test_a_page_with_neither_link_is_a_reset() -> None:
    with pytest.raises(MailSyncResetError):
        await _run({None: _page(_message(1))}, state=None, calls=[])


@_sync
async def test_an_endless_chain_of_next_links_is_cut_off_with_a_continuation() -> None:
    loop = f"{_BASE}?$skiptoken=again"
    pages = {None: _page(_message(1), next_link=loop), loop: _page(_message(2), next_link=loop)}
    payload, _ = await _run(pages, state=None, calls=[])
    assert payload["state"] == loop
    assert len(payload["items"]) >= 2


@_sync
async def test_the_first_read_sets_the_page_size_with_the_prefer_header_not_top() -> None:
    calls: list[Any] = []
    pages = {None: _page(_message(1), delta_link=f"{_BASE}?$deltatoken=abc")}
    await _run(pages, state=None, calls=calls)
    config = calls[0][2]
    assert config.query_parameters.top is None
    assert list(config.headers.try_get("Prefer")) == ["odata.maxpagesize=50"]


@_sync
async def test_a_burst_longer_than_the_page_cap_returns_a_partial_batch_and_resumes() -> None:
    from blumkin.skills import mail as mail_module

    calls: list[Any] = []
    links = [f"{_BASE}?$skiptoken={n}" for n in range(1, 4)]
    pages: dict[Any, Any] = {None: _page(_message(0), next_link=links[0])}
    pages[links[0]] = _page(_message(1), next_link=links[1])
    pages[links[1]] = _page(_message(2), next_link=links[2])
    pages[links[2]] = _page(_message(3), delta_link=f"{_BASE}?$deltatoken=end")
    from unittest.mock import patch

    with patch.object(mail_module, "_DELTA_MAX_PAGES", 2):
        first, _ = await _run(pages, state=None, calls=calls)
        assert [i["id"] for i in first["items"]] == ["msg-0", "msg-1"]
        assert first["state"] == links[1]
        second, _ = await _run(pages, state=first["state"], calls=[])
    assert [i["id"] for i in second["items"]] == ["msg-2", "msg-3"]
    assert second["state"].endswith("deltatoken=end")
