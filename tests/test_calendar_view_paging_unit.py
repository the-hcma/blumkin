"""`calendar view` follows every page and says when it could not (#655)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from blumkin.config import BlumkinConfig
from blumkin.providers.google import calendar as google_calendar
from blumkin.skills import calendar as graph_calendar
from blumkin.skills.calendar import calendar_today, calendar_view, format_view_human

_NY = "America/New_York"
_START = datetime(2026, 9, 1, tzinfo=ZoneInfo(_NY))


def _graph_event(number: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"evt-{number}",
        subject=f"Meeting {number}",
        is_all_day=False,
        is_organizer=True,
        start=SimpleNamespace(date_time="2026-09-01T14:00:00Z"),
        end=SimpleNamespace(date_time="2026-09-01T14:30:00Z"),
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=None,
    )


def _graph(monkeypatch, first, followers) -> MagicMock:
    """A client whose first page is `first` and whose next links return `followers` in turn."""
    client = MagicMock()
    client.me.calendar.calendar_view.get = AsyncMock(return_value=first)
    pages = iter(followers)
    seen: list[str] = []

    def with_url(url: str) -> MagicMock:
        seen.append(url)
        follower = MagicMock()
        follower.get = AsyncMock(side_effect=lambda: next(pages))
        return follower

    client.me.calendar.calendar_view.with_url = with_url
    client.seen_links = seen
    monkeypatch.setattr("blumkin.skills.calendar.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar.load_config",
        lambda: SimpleNamespace(default_tz=_NY, client_id="x"),
    )
    return client


def test_graph_view_follows_the_next_link_until_the_range_is_exhausted(monkeypatch) -> None:
    first = SimpleNamespace(value=[_graph_event(n) for n in range(10)], odata_next_link="link-2")
    second = SimpleNamespace(value=[_graph_event(n) for n in range(10, 15)], odata_next_link=None)
    client = _graph(monkeypatch, first, [second])

    payload = asyncio.run(calendar_view(start=_START, end=_START.replace(day=15)))

    assert [item["id"] for item in payload["items"]] == [f"evt-{n}" for n in range(15)]
    assert payload["complete"] is True
    assert client.seen_links == ["link-2"]


def test_graph_view_asks_for_a_big_page(monkeypatch) -> None:
    client = _graph(monkeypatch, SimpleNamespace(value=[], odata_next_link=None), [])
    asyncio.run(calendar_view(start=_START, end=_START.replace(day=2)))

    config = client.me.calendar.calendar_view.get.await_args.args[0]
    assert config.query_parameters.top == graph_calendar._VIEW_PAGE_SIZE


def test_graph_view_that_hits_the_cap_says_it_is_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(graph_calendar, "_VIEW_MAX_PAGES", 3)
    page = lambda: SimpleNamespace(value=[_graph_event(1)], odata_next_link="more")  # noqa: E731
    client = _graph(monkeypatch, page(), [page(), page(), page()])

    payload = asyncio.run(calendar_view(start=_START, end=_START.replace(day=15)))

    assert len(payload["items"]) == 3
    assert payload["complete"] is False
    assert len(client.seen_links) == 2  # the first page plus two follow-ups, then the cap
    lines = format_view_human(payload)
    assert any("incomplete" in line for line in lines)


def test_graph_view_with_no_events_is_complete(monkeypatch) -> None:
    _graph(monkeypatch, SimpleNamespace(value=[]), [])
    payload = asyncio.run(calendar_view(start=_START, end=_START.replace(day=2)))
    assert payload["items"] == [] and payload["complete"] is True
    assert not any("incomplete" in line for line in format_view_human(payload))


def test_graph_today_carries_the_completeness(monkeypatch) -> None:
    _graph(monkeypatch, SimpleNamespace(value=[_graph_event(1)], odata_next_link=None), [])
    payload = asyncio.run(calendar_today(tz_name=_NY))
    assert payload["complete"] is True


def _google_event(number: int) -> dict:
    return {
        "id": f"evt-{number}",
        "summary": f"Meeting {number}",
        "start": {"dateTime": "2026-09-01T10:00:00-04:00"},
        "end": {"dateTime": "2026-09-01T10:30:00-04:00"},
    }


def _google(pages: list[dict]) -> tuple[MagicMock, Any]:
    service = MagicMock()
    service.events.return_value.list.return_value.execute.side_effect = pages
    patched = patch.multiple(
        google_calendar,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
        _resolve_calendar_id=MagicMock(return_value="primary"),
    )
    return service, patched


def test_google_view_follows_the_page_token_until_the_range_is_exhausted() -> None:
    service, patched = _google(
        [
            {"items": [_google_event(n) for n in range(250)], "nextPageToken": "t2"},
            {"items": [_google_event(n) for n in range(250, 260)]},
        ]
    )
    with patched:
        payload = asyncio.run(
            google_calendar.calendar_view(
                start=_START,
                end=_START.replace(day=15),
                config=cast("BlumkinConfig", SimpleNamespace(default_tz=_NY)),
            )
        )

    assert len(payload["items"]) == 260
    assert payload["complete"] is True
    calls = service.events.return_value.list.call_args_list
    assert [c.kwargs["pageToken"] for c in calls] == [None, "t2"]
    assert all(c.kwargs["maxResults"] == google_calendar._VIEW_PAGE_SIZE for c in calls)


def test_google_view_that_hits_the_cap_says_it_is_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(google_calendar, "_VIEW_MAX_PAGES", 2)
    full = {"items": [_google_event(1)], "nextPageToken": "more"}
    _service, patched = _google([full, full, full])
    with patched:
        payload = asyncio.run(
            google_calendar.calendar_view(
                start=_START,
                end=_START.replace(day=15),
                config=cast("BlumkinConfig", SimpleNamespace(default_tz=_NY)),
            )
        )

    assert len(payload["items"]) == 2
    assert payload["complete"] is False
