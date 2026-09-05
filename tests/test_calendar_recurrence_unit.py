"""Hermetic coverage for recurring ``calendar create`` (issue #158).

Covers the shared flag parser, the two provider mappings (Graph
``patternedRecurrence`` and Google ``RRULE``), and the CLI wiring.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from msgraph.generated.models.day_of_week import DayOfWeek
from msgraph.generated.models.recurrence_pattern_type import RecurrencePatternType
from msgraph.generated.models.recurrence_range_type import RecurrenceRangeType

from blumkin.cli import main
from blumkin.config import BlumkinConfig, MailSignatureConfig
from blumkin.exit_codes import EXIT_USAGE
from blumkin.providers.google import calendar as google_calendar
from blumkin.providers.google_provider import GoogleWorkspaceProvider
from blumkin.providers.kind import ProviderKind
from blumkin.skills.calendar_writes import (
    Recurrence,
    calendar_create,
    format_create_human,
    parse_recurrence,
    recurrence_payload,
    recurrence_rrule,
)

_GOOGLE_CAL = "blumkin.providers.google.calendar"
_START = datetime(2026, 9, 22, 13, 5, tzinfo=ZoneInfo("America/New_York"))  # a Tuesday


# --------------------------------------------------------------------------- parser


def test_parse_recurrence_weekly_defaults() -> None:
    rec = parse_recurrence(repeat="weekly")
    assert rec == Recurrence(freq="weekly", interval=1, days=(), count=None, until=None)


def test_parse_recurrence_days_are_normalized_and_sorted() -> None:
    rec = parse_recurrence(repeat="weekly", days="fri, Mon,tuesday")
    assert rec.days == ("MO", "TU", "FR")


def test_parse_recurrence_until_is_parsed() -> None:
    assert parse_recurrence(repeat="daily", until="2026-12-31").until == date(2026, 12, 31)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"repeat": "yearly"}, "must be one of"),
        ({"repeat": "weekly", "interval": 0}, "interval"),
        ({"repeat": "weekly", "until": "2026-12-31", "count": 5}, "only one of"),
        ({"repeat": "weekly", "until": "nonsense"}, "invalid --until"),
        ({"repeat": "weekly", "count": 0}, "count"),
        ({"repeat": "daily", "days": "mon"}, "only applies with --repeat weekly"),
        ({"repeat": "weekly", "days": "funday"}, "invalid --days"),
        ({"repeat": "weekly", "days": " , "}, "at least one weekday"),
    ],
)
def test_parse_recurrence_rejects(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_recurrence(**kwargs)


def test_recurrence_payload_rejects_until_before_start() -> None:
    rec = Recurrence(freq="weekly", until=date(2026, 9, 1))
    with pytest.raises(ValueError, match="is before --start"):
        recurrence_payload(rec, _START)


def test_recurrence_payload_echo_shapes() -> None:
    weekly = recurrence_payload(Recurrence(freq="weekly", count=3), _START)
    assert weekly == {"freq": "weekly", "interval": 1, "days": ["tu"], "count": 3}
    monthly = recurrence_payload(Recurrence(freq="monthly"), _START)
    assert monthly == {"freq": "monthly", "interval": 1, "day_of_month": 22, "ends": "never"}


def test_recurrence_payload_rejects_days_without_the_start_weekday() -> None:
    # _START is a Tuesday.
    rec = Recurrence(freq="weekly", days=("MO", "WE"))
    with pytest.raises(ValueError, match="must include the --start weekday"):
        recurrence_payload(rec, _START)


# --------------------------------------------------------------------------- rrule


def test_recurrence_rrule_weekly_with_days_and_until() -> None:
    rec = Recurrence(freq="weekly", interval=2, days=("MO", "WE"), until=date(2026, 12, 31))
    (line,) = recurrence_rrule(rec, _START)
    assert line == "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;UNTIL=20270101T045959Z"


def test_recurrence_rrule_daily_with_count() -> None:
    (line,) = recurrence_rrule(Recurrence(freq="daily", count=10), _START)
    assert line == "RRULE:FREQ=DAILY;INTERVAL=1;COUNT=10"


def test_recurrence_rrule_monthly_open_ended() -> None:
    (line,) = recurrence_rrule(Recurrence(freq="monthly"), _START)
    assert line == "RRULE:FREQ=MONTHLY;INTERVAL=1"


# --------------------------------------------------------------------------- Graph


def _graph_created() -> SimpleNamespace:
    return SimpleNamespace(
        id="evt-new",
        subject="1:1",
        start=None,
        end=None,
        is_all_day=False,
        is_organizer=True,
        location=None,
        organizer=None,
        response_status=None,
        online_meeting=None,
    )


def _graph_client(monkeypatch) -> MagicMock:
    client = MagicMock()
    client.me.events.post = AsyncMock(return_value=_graph_created())
    monkeypatch.setattr("blumkin.skills.calendar_writes.create_graph_client", lambda _cfg: client)
    monkeypatch.setattr(
        "blumkin.skills.calendar_writes.load_config",
        lambda: SimpleNamespace(default_tz="America/New_York", client_id="x"),
    )
    return client


def test_graph_calendar_create_weekly_recurrence(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    payload = asyncio.run(
        calendar_create(
            subject="1:1",
            with_emails=["sam@example.com"],
            start_raw="2026-09-21T13:05",  # a Monday
            duration="45m",
            recurrence=parse_recurrence(repeat="weekly", days="mon,wed", until="2026-12-31"),
            teams=False,
            tz_name="America/New_York",
        )
    )
    posted = client.me.events.post.await_args.args[0]
    assert posted.recurrence.pattern.type == RecurrencePatternType.Weekly
    assert posted.recurrence.pattern.days_of_week == [DayOfWeek.Monday, DayOfWeek.Wednesday]
    assert posted.recurrence.pattern.first_day_of_week == DayOfWeek.Monday
    assert posted.recurrence.range.type == RecurrenceRangeType.EndDate
    assert posted.recurrence.range.start_date == date(2026, 9, 21)
    assert posted.recurrence.range.end_date == date(2026, 12, 31)
    assert payload["recurrence"] == {
        "freq": "weekly",
        "interval": 1,
        "days": ["mo", "we"],
        "until": "2026-12-31",
    }


def test_graph_calendar_create_rejects_until_before_start_without_posting(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    with pytest.raises(ValueError, match="is before --start"):
        asyncio.run(
            calendar_create(
                subject="1:1",
                with_emails=[],
                start_raw="2026-09-22T13:05",
                recurrence=parse_recurrence(repeat="weekly", until="2020-01-01"),
                teams=False,
                tz_name="America/New_York",
            )
        )
    client.me.events.post.assert_not_awaited()


def test_graph_calendar_create_monthly_count(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    asyncio.run(
        calendar_create(
            subject="Review",
            with_emails=[],
            start_raw="2026-09-22T13:05",
            recurrence=parse_recurrence(repeat="monthly", interval=2, count=6),
            teams=False,
            tz_name="America/New_York",
        )
    )
    pattern = client.me.events.post.await_args.args[0].recurrence.pattern
    assert pattern.type == RecurrencePatternType.AbsoluteMonthly
    assert pattern.interval == 2
    assert pattern.day_of_month == 22
    rng = client.me.events.post.await_args.args[0].recurrence.range
    assert rng.type == RecurrenceRangeType.Numbered
    assert rng.number_of_occurrences == 6


def test_graph_calendar_create_daily_open_ended(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    asyncio.run(
        calendar_create(
            subject="Standup",
            with_emails=[],
            start_raw="2026-09-22T09:00",
            recurrence=parse_recurrence(repeat="daily", interval=3),
            teams=False,
            tz_name="America/New_York",
        )
    )
    recurrence = client.me.events.post.await_args.args[0].recurrence
    assert recurrence.pattern.type == RecurrencePatternType.Daily
    assert recurrence.pattern.interval == 3
    assert recurrence.range.type == RecurrenceRangeType.NoEnd
    assert recurrence.range.start_date == date(2026, 9, 22)


def test_graph_calendar_create_single_event_has_no_recurrence(monkeypatch) -> None:
    client = _graph_client(monkeypatch)
    payload = asyncio.run(
        calendar_create(
            subject="One-off",
            with_emails=[],
            start_raw="2026-09-22T13:05",
            teams=False,
            tz_name="America/New_York",
        )
    )
    assert client.me.events.post.await_args.args[0].recurrence is None
    assert "recurrence" not in payload


# --------------------------------------------------------------------------- Google


def _google_cfg(config_dir: Path) -> BlumkinConfig:
    oauth = config_dir / "desktop-client.json"
    oauth.write_text('{"installed": {"client_id": "id.apps.googleusercontent.com"}}')
    return BlumkinConfig(
        client_id="id.apps.googleusercontent.com",
        config_dir=config_dir,
        default_tz="America/New_York",
        email="",
        files_scopes=False,
        google_oauth_client_file=oauth,
        graph_timeout_seconds=60.0,
        legacy_flat=True,
        mail_signature=MailSignatureConfig(),
        profile="default",
        provider=ProviderKind.GOOGLE,
        tags=(),
        tenant_id="",
        wo1162425_scopes=False,
    )


def test_google_calendar_create_sets_rrule_and_echo(tmp_path: Path) -> None:
    service = MagicMock()
    insert = service.events.return_value.insert
    insert.return_value.execute.return_value = {
        "id": "g-evt",
        "summary": "1:1",
        "start": {"dateTime": "2026-09-22T13:05:00-04:00"},
        "end": {"dateTime": "2026-09-22T13:50:00-04:00"},
    }
    with patch.multiple(
        _GOOGLE_CAL,
        get_credentials=MagicMock(return_value=MagicMock()),
        build_api_service=MagicMock(return_value=service),
    ):
        payload = asyncio.run(
            GoogleWorkspaceProvider(_google_cfg(tmp_path)).calendar_create(
                subject="1:1",
                with_emails=["sam@example.com"],
                start_raw="2026-09-22T13:05",
                duration="45m",
                recurrence=parse_recurrence(repeat="weekly", count=12),
            )
        )
    assert insert.call_args.kwargs["body"]["recurrence"] == [
        "RRULE:FREQ=WEEKLY;INTERVAL=1;COUNT=12"
    ]
    assert payload["recurrence"] == {
        "freq": "weekly",
        "interval": 1,
        "days": ["tu"],
        "count": 12,
    }


@pytest.mark.parametrize(
    ("recurrence_kwargs", "match"),
    [
        ({"repeat": "weekly", "until": "2020-01-01"}, "is before --start"),
        # _START "2026-09-22" is a Tuesday.
        ({"repeat": "weekly", "days": "mon,wed"}, "must include the --start weekday"),
    ],
)
def test_google_calendar_create_rejects_bad_recurrence_before_any_insert(
    tmp_path: Path, recurrence_kwargs: dict, match: str
) -> None:
    service = MagicMock()
    with (
        patch.multiple(
            _GOOGLE_CAL,
            get_credentials=MagicMock(return_value=MagicMock()),
            build_api_service=MagicMock(return_value=service),
        ),
        pytest.raises(ValueError, match=match),
    ):
        asyncio.run(
            google_calendar.calendar_create(
                subject="1:1",
                with_emails=["sam@example.com"],
                start_raw="2026-09-22T13:05",
                recurrence=parse_recurrence(**recurrence_kwargs),
                config=_google_cfg(tmp_path),
            )
        )
    # Google's events.insert is a non-idempotent POST that mails attendees; the
    # rejection must land before it (mirrors the Graph post.assert_not_awaited).
    service.events.return_value.insert.assert_not_called()


# --------------------------------------------------------------------------- CLI


def test_cli_help_lists_repeat() -> None:
    result = CliRunner().invoke(main, ["calendar", "create", "--help"])
    assert result.exit_code == 0
    assert "--repeat" in result.output
    assert "--days" in result.output


def test_cli_days_without_weekly_is_usage_error() -> None:
    result = CliRunner().invoke(
        main,
        [
            "calendar",
            "create",
            "--subject",
            "x",
            "--start",
            "2026-09-22T13:05",
            "--repeat",
            "daily",
            "--days",
            "mon",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "weekly" in result.stderr


def test_cli_recurrence_value_error_classifies_as_usage_not_auth(monkeypatch) -> None:
    # recurrence_payload raises this ValueError deep in the skill; pin that the
    # CLI's generic handler classifies that message as usage_error / exit 2, not
    # auth_required / exit 3. Patch the provider so the test is hermetic and only
    # exercises the classification (recurrence_payload firing before any network
    # call is pinned by the Graph/Google create tests above).
    async def _raise_recurrence_error(**_kwargs):
        raise ValueError("--until 2020-01-01 is before --start 2026-09-22")

    monkeypatch.setattr(
        "blumkin.cli._workspace",
        lambda: SimpleNamespace(calendar_create=_raise_recurrence_error),
    )
    result = CliRunner().invoke(
        main,
        [
            "calendar",
            "create",
            "--subject",
            "x",
            "--start",
            "2026-09-22T13:05",
            "--repeat",
            "weekly",
            "--until",
            "2020-01-01",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.stderr)["error"] == "usage_error"


def test_cli_recurrence_flags_require_repeat() -> None:
    result = CliRunner().invoke(
        main,
        [
            "calendar",
            "create",
            "--subject",
            "x",
            "--start",
            "2026-09-22T13:05",
            "--count",
            "5",
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_USAGE
    assert "require --repeat" in result.stderr


def test_format_create_human_shows_recurrence() -> None:
    lines = format_create_human(
        {
            "event": {"id": "e", "start": "s", "end": "e2", "subject": "1:1"},
            "recurrence": {"freq": "weekly", "interval": 1, "days": ["mo"], "until": "2026-12-31"},
        }
    )
    assert any("repeats: weekly on mo until 2026-12-31" in line for line in lines)
