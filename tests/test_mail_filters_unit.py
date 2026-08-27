"""Filtering and searching mail lists (issue #55, item 2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from blumkin.skills.mail import (
    format_list_human,
    mail_inbox,
    mail_list,
)


def test_mail_list_matches_a_sender_locally_because_graph_will_not_sort_it(monkeypatch) -> None:
    """Graph answers InefficientFilter for contains() plus $orderby, folder or not."""
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(
        return_value=_page([_msg("Rebecca Doe", "budget"), _msg("Sam Lee", "budget")])
    )

    payload = asyncio.run(mail_list(sender="rebecca"))

    assert _query(client.me.messages.get).filter is None
    assert [item["from_name"] for item in payload["items"]] == ["Rebecca Doe"]
    assert payload["filters"]["matched_locally"] is True


def test_mail_list_matches_a_sender_by_address_too(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(
        return_value=_page([_msg("Sam Lee", "budget", address="rebecca@example.com")])
    )

    payload = asyncio.run(mail_list(sender="Rebecca"))

    assert len(payload["items"]) == 1


def test_mail_list_requires_every_text_filter_to_match(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(
        return_value=_page([_msg("Rebecca Doe", "lunch"), _msg("Rebecca Doe", "budget")])
    )

    payload = asyncio.run(mail_list(sender="Rebecca", subject="budget"))

    assert [item["subject"] for item in payload["items"]] == ["budget"]


def test_mail_list_still_filters_read_state_server_side(monkeypatch) -> None:
    """isRead and date comparisons do survive $orderby, so they stay in the query."""
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([]))

    asyncio.run(mail_list(subject="budget", unread=True))

    assert _query(client.me.messages.get).filter == "isRead eq false"


def test_mail_list_stops_scanning_at_the_cap(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr("blumkin.skills.mail._MAX_SCANNED", 3)
    client.me.messages.get = AsyncMock(
        return_value=_page(
            [_msg("Sam Lee", "lunch") for _ in range(5)], next_link="https://graph/next"
        )
    )

    payload = asyncio.run(mail_list(sender="Rebecca"))

    assert payload["items"] == []
    assert payload["filters"]["scanned"] == 5
    assert payload["filters"]["complete"] is False
    client.me.messages.with_url.assert_not_called()


def test_mail_list_calls_a_scan_complete_when_it_reaches_the_end_at_the_cap(monkeypatch) -> None:
    """Hitting the cap on the final page still read everything, so it is not truncated."""
    client = _client(monkeypatch)
    monkeypatch.setattr("blumkin.skills.mail._MAX_SCANNED", 5)
    client.me.messages.get = AsyncMock(
        return_value=_page([_msg("Sam Lee", "lunch") for _ in range(5)])
    )

    payload = asyncio.run(mail_list(sender="Rebecca"))

    assert payload["filters"]["scanned"] == 5
    assert payload["filters"]["complete"] is True


def test_mail_list_does_not_match_across_the_address_and_name_boundary(monkeypatch) -> None:
    """Joining the fields would match text present in neither of them."""
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(
        return_value=_page([_msg("Bob", "budget", address="alice@example.com")])
    )

    payload = asyncio.run(mail_list(sender="m b"))

    assert payload["items"] == []


def test_mail_list_stops_scanning_once_top_matches_are_found(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(
        return_value=_page([_msg("Rebecca Doe", f"note {n}") for n in range(5)])
    )

    payload = asyncio.run(mail_list(sender="Rebecca", top=2))

    assert len(payload["items"]) == 2
    assert payload["filters"]["scanned"] == 2
    assert payload["filters"]["complete"] is True


def test_mail_list_pages_while_scanning(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(
        return_value=_page([_msg("Sam Lee", "lunch")], next_link="https://graph/next")
    )
    client.me.messages.with_url.return_value.get = AsyncMock(
        return_value=_page([_msg("Rebecca Doe", "budget")])
    )

    payload = asyncio.run(mail_list(sender="Rebecca"))

    assert [item["from_name"] for item in payload["items"]] == ["Rebecca Doe"]
    assert payload["filters"]["scanned"] == 2
    client.me.messages.with_url.assert_called_once_with("https://graph/next")


def test_mail_list_does_not_scan_without_a_text_filter(monkeypatch) -> None:
    """A plain listing must stay one request for `--top` messages."""
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([_msg("Sam Lee", "lunch")]))

    payload = asyncio.run(mail_list(top=5))

    assert _query(client.me.messages.get).top == 5
    assert payload["filters"]["matched_locally"] is False
    assert payload["filters"]["complete"] is True


def test_mail_list_bounds_dates_half_open(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([]))

    asyncio.run(
        mail_list(
            since=datetime(2026, 8, 1, tzinfo=UTC),
            until=datetime(2026, 8, 8, tzinfo=UTC),
        )
    )

    criteria = _query(client.me.messages.get).filter
    assert criteria == (
        "receivedDateTime ge 2026-08-01T00:00:00Z and receivedDateTime lt 2026-08-08T00:00:00Z"
    )


def test_mail_list_bounds_dates_on_the_field_it_sorts_by(monkeypatch) -> None:
    """Drafts have a null receivedDateTime, so bounding that would drop every message."""
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(return_value=_page([]))

    asyncio.run(mail_list(folder="drafts", since=datetime(2026, 8, 1, tzinfo=UTC)))

    assert _query(messages.get).filter == "createdDateTime ge 2026-08-01T00:00:00Z"


def test_mail_list_converts_naive_and_offset_bounds_to_utc(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([]))

    asyncio.run(mail_list(since=datetime(2026, 8, 1, 12, 30)))

    assert _query(client.me.messages.get).filter == "receivedDateTime ge 2026-08-01T12:30:00Z"


def test_mail_list_sends_no_filter_when_nothing_was_asked_for(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([]))

    asyncio.run(mail_list())

    assert _query(client.me.messages.get).filter is None


def test_mail_list_searches_without_a_sort(monkeypatch) -> None:
    """Graph answers SearchWithOrderBy when both are sent, so a search drops the sort."""
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([]))

    payload = asyncio.run(mail_list(search="quarterly budget"))

    query = _query(client.me.messages.get)
    assert query.search == '"quarterly budget"'
    assert query.orderby is None
    assert query.filter is None
    assert payload["orderby"] is None


def test_mail_list_rejects_search_with_a_filter() -> None:
    with pytest.raises(ValueError, match=r"--search cannot be combined with --from, --unread"):
        asyncio.run(mail_list(search="budget", sender="Rebecca", unread=True))


def test_mail_list_rejects_search_with_an_explicit_orderby() -> None:
    with pytest.raises(ValueError, match="--search cannot be combined with --orderby"):
        asyncio.run(mail_list(search="budget", orderby="received"))


def test_mail_list_rejects_a_search_containing_a_double_quote() -> None:
    with pytest.raises(ValueError, match="double quote"):
        asyncio.run(mail_list(search='say "hello"'))


def test_mail_list_rejects_an_empty_search() -> None:
    with pytest.raises(ValueError, match="--search cannot be empty"):
        asyncio.run(mail_list(search="   "))


def test_mail_list_rejects_an_inverted_date_range() -> None:
    with pytest.raises(ValueError, match="--until must be after --since"):
        asyncio.run(
            mail_list(
                since=datetime(2026, 8, 8, tzinfo=UTC),
                until=datetime(2026, 8, 1, tzinfo=UTC),
            )
        )


def test_mail_list_reports_the_filters_it_applied(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.me.messages.get = AsyncMock(return_value=_page([]))

    payload = asyncio.run(
        mail_list(sender="Rebecca", unread=True, since=datetime(2026, 8, 1, tzinfo=UTC))
    )

    assert payload["filters"] == {
        "complete": True,
        "from": "Rebecca",
        "matched_locally": True,
        "scanned": 0,
        "search": None,
        "since": "2026-08-01T00:00:00Z",
        "subject": None,
        "unread": True,
        "until": None,
    }


def test_mail_inbox_passes_filters_through(monkeypatch) -> None:
    client = _client(monkeypatch)
    messages = client.me.mail_folders.by_mail_folder_id.return_value.messages
    messages.get = AsyncMock(return_value=_page([]))
    client.me.messages.get = AsyncMock(return_value=_page([]))

    payload = asyncio.run(mail_inbox(sender="Rebecca", unread=True))

    assert _query(client.me.messages.get).filter == "isRead eq false"
    assert payload["filters"]["from"] == "Rebecca"


def test_format_list_human_reports_relevance_order_for_a_search() -> None:
    lines = format_list_human(
        {
            "filters": {"search": "budget"},
            "folder": "inbox",
            "items": [],
            "orderby": None,
            "top": 10,
        }
    )

    assert "by relevance" in lines[0]
    assert lines[1] == "  filters: search='budget'"


def test_format_list_human_names_the_active_filters() -> None:
    """A thin result set should explain itself rather than look like an empty mailbox."""
    lines = format_list_human(
        {
            "filters": {"from": "Rebecca", "since": "2026-08-01T00:00:00Z", "unread": True},
            "folder": "inbox",
            "items": [],
            "orderby": "received",
            "top": 10,
        }
    )

    assert lines[1] == "  filters: from='Rebecca', since='2026-08-01T00:00:00Z', unread only"


def test_format_list_human_stays_quiet_without_filters() -> None:
    lines = format_list_human(
        {"filters": {}, "folder": "inbox", "items": [], "orderby": "received", "top": 10}
    )

    assert lines == ["inbox (top 10, by received): 0 message(s)", "  (none)"]


def _client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr("blumkin.skills.mail.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.mail.load_config",
        lambda: SimpleNamespace(client_id="x", default_tz="UTC"),
    )
    return client


def _msg(name: str, subject: str, *, address: str = "someone@example.com") -> SimpleNamespace:
    return SimpleNamespace(
        body=None,
        body_preview=subject,
        created_date_time=None,
        from_=SimpleNamespace(email_address=SimpleNamespace(address=address, name=name)),
        has_attachments=False,
        id=f"msg-{name}-{subject}",
        is_read=True,
        received_date_time="2026-08-27T09:00Z",
        sent_date_time=None,
        subject=subject,
        to_recipients=None,
    )


def _page(value: list[Any], *, next_link: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(odata_next_link=next_link, value=value)


def _query(get_mock: AsyncMock) -> Any:
    await_args = get_mock.await_args
    assert await_args is not None
    return await_args.args[0].query_parameters
