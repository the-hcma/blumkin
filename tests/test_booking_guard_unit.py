"""The booking guard: no meeting on a weekend, a holiday, off-hours or a busy slot (issue #716)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from blumkin.booking_guard import (
    CHECKS,
    allowed_checks,
    check_booking,
    schedule_findings,
    slot_findings,
)
from blumkin.config import BookingExclusion, PreferencesConfig, _booking_exclusions, _holidays
from blumkin.providers.kind import ProviderKind
from blumkin.skills.dispatch import run_skill
from blumkin.skills.errors import BookingGuardError, classify_exception

_NY = ZoneInfo("America/New_York")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
_PREFS = PreferencesConfig(
    booking_exclusions=(
        BookingExclusion(
            day=3, end=datetime(2026, 1, 1, 12, 0).time(), start=datetime(2026, 1, 1).time(),
            text="thu 00:00-12:00",
        ),
    ),
    holidays=((date(2026, 11, 11), "Veterans Day"),),
)  # fmt: skip


def _slot(day: str, hour: int, minutes: int = 30) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=_NY)
    return start, start.replace(hour=hour + minutes // 60, minute=minutes % 60)


def _item(
    address: str = "me@example.com",
    *,
    busy: list[dict[str, Any]] | None = None,
    hours: dict[str, Any] | None = None,
    view: str = "0000",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "availability_view": view,
        "busy": busy or [],
        "error": error,
        "schedule": address,
        "working_hours": hours
        if hours is not None
        else {
            "days_of_week": _WEEKDAYS,
            "end": "17:00:00",
            "start": "08:00:00",
            "timezone": "America/New_York",
        },
    }


def _config(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "account_type": "organizational",
        "default_tz": "America/New_York",
        "email": "me@example.com",
        "preferences": _PREFS,
        "provider": ProviderKind.MICROSOFT,
        "wo1162425_scopes": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _provider(items: list[dict[str, Any]] | Exception | None = None, **extra: Any) -> Any:
    freebusy = (
        AsyncMock(side_effect=items)
        if isinstance(items, Exception)
        else AsyncMock(return_value={"items": [_item()] if items is None else items})
    )
    return SimpleNamespace(
        calendar_create=AsyncMock(return_value={"event": {"id": "evt-1"}}),
        calendar_freebusy=freebusy,
        **extra,
    )


def _create(provider: Any, start: str, **args: Any) -> dict[str, Any]:
    arguments = {"subject": "Sync", "start": start, "tz": "America/New_York", **args}
    return asyncio.run(run_skill("calendar.create", arguments, config=_config(), provider=provider))


# --------------------------------------------------------------------------- the pure checks


def test_a_tuesday_morning_passes_every_slot_check() -> None:
    assert slot_findings(*_slot("2026-11-10", 10), _PREFS) == []


def test_a_saturday_is_a_weekend_and_a_listed_date_is_a_holiday_with_its_name() -> None:
    weekend = slot_findings(*_slot("2026-11-14", 10), _PREFS)
    holiday = slot_findings(*_slot("2026-11-11", 10), _PREFS)

    assert [f.check for f in weekend] == ["weekend"]
    assert [f.check for f in holiday] == ["holiday"] and "Veterans Day" in holiday[0].message


def test_an_excluded_window_is_refused_and_the_afternoon_of_that_day_is_not() -> None:
    morning = slot_findings(*_slot("2026-11-12", 10), _PREFS)  # Thursday
    afternoon = slot_findings(*_slot("2026-11-12", 13), _PREFS)

    assert [f.check for f in morning] == ["exclusion"] and "thu 00:00-12:00" in morning[0].message
    assert afternoon == []


def test_a_slot_ending_exactly_at_midnight_does_not_touch_the_next_day() -> None:
    start = datetime(2026, 11, 13, 23, 30, tzinfo=_NY)  # Friday night, ends Saturday 00:00

    assert slot_findings(start, datetime(2026, 11, 14, 0, 0, tzinfo=_NY), _PREFS) == []
    assert [
        f.check for f in slot_findings(start, datetime(2026, 11, 14, 0, 30, tzinfo=_NY), _PREFS)
    ] == ["weekend"]


def test_working_hours_are_read_in_the_persons_own_time_zone() -> None:
    start, end = _slot("2026-11-10", 10)  # 10:00 in New York is 15:00 in Lisbon
    lisbon = _item(
        "ana@example.com",
        hours={
            "days_of_week": _WEEKDAYS,
            "end": "17:00:00",
            "start": "09:00:00",
            "timezone": "Europe/Lisbon",
        },
    )
    late = _slot("2026-11-10", 13)  # 13:00 New York is 18:00 Lisbon: after their day

    ok, warnings, checked = schedule_findings(start, end, lisbon, who="ana@example.com")
    out, _, _ = schedule_findings(*late, lisbon, who="ana@example.com")

    assert ok == [] and warnings == [] and checked == {"hours", "busy"}
    assert [f.check for f in out] == ["hours"] and "ana@example.com" in out[0].text()


def test_a_day_that_is_not_a_working_day_for_them_is_refused() -> None:
    sunday_hours = _item(
        "ana@example.com",
        hours={
            "days_of_week": ["monday"],
            "end": "17:00:00",
            "start": "08:00:00",
            "timezone": "America/New_York",
        },
    )

    found, _, _ = schedule_findings(*_slot("2026-11-10", 10), sunday_hours, who="ana@example.com")

    assert [f.check for f in found] == ["hours"] and "not a working day" in found[0].message


def test_a_busy_slot_blocks_but_free_and_working_elsewhere_do_not() -> None:
    start, end = _slot("2026-11-10", 10)

    def busy(status: str) -> dict[str, Any]:
        return _item(
            busy=[
                {
                    "end": "2026-11-10T10:30:00-05:00",
                    "start": "2026-11-10T10:00:00-05:00",
                    "status": status,
                }
            ]
        )

    assert [f.check for f in schedule_findings(start, end, busy("busy"), who="you")[0]] == ["busy"]
    assert [f.check for f in schedule_findings(start, end, busy("tentative"), who="you")[0]] == [
        "busy"
    ]
    assert schedule_findings(start, end, busy("free"), who="you")[0] == []
    assert schedule_findings(start, end, busy("workingElsewhere"), who="you")[0] == []


def test_the_event_being_moved_is_not_its_own_conflict() -> None:
    start, end = _slot("2026-11-10", 10)
    own = _item(
        busy=[
            {
                "end": "2026-11-10T10:30:00-05:00",
                "start": "2026-11-10T10:00:00-05:00",
                "status": "busy",
            }
        ]
    )

    assert schedule_findings(start, end, own, who="you", own_slot=(start, end))[0] == []
    assert [f.check for f in schedule_findings(start, end, own, who="you")[0]] == ["busy"]


@pytest.mark.parametrize(
    ("item", "text"),
    [
        (None, "freebusy returned nothing for"),
        (_item(error="no access"), "could not read"),
        (_item(hours={}), "no working hours known"),
        (
            _item(
                hours={
                    "days_of_week": _WEEKDAYS,
                    "end": "17:00:00",
                    "start": "08:00:00",
                    "timezone": "Eastern Standard Time",
                }
            ),
            "unknown time zone",
        ),
        (_item(view="", busy=None), "nothing usable"),
    ],
)
def test_what_could_not_be_read_is_a_warning_and_never_a_pass(item: Any, text: str) -> None:
    found, warnings, checked = schedule_findings(
        *_slot("2026-11-10", 10), item, who="ana@example.com"
    )

    assert found == [] and any(text in warning for warning in warnings)
    assert checked != {"hours", "busy"}  # a check that was not made is not reported as made


def test_allow_names_checks_one_at_a_time_and_refuses_an_unknown_one() -> None:
    assert allowed_checks(None) == frozenset()
    assert allowed_checks(["weekend", "busy,hours"]) == {"weekend", "busy", "hours"}
    with pytest.raises(ValueError, match="--allow takes one of"):
        allowed_checks(["lunch"])
    assert set(CHECKS) == {"busy", "exclusion", "holiday", "hours", "weekend"}


# --------------------------------------------------------------------------- the config


def test_holidays_and_exclusions_are_read_from_the_config_text() -> None:
    assert _holidays(["2026-11-26 Thanksgiving Day", "2026-11-11 Veterans Day", "2026-12-24"]) == (
        (date(2026, 11, 11), "Veterans Day"),
        (date(2026, 11, 26), "Thanksgiving Day"),
        (date(2026, 12, 24), ""),
    )
    rules = _booking_exclusions(["thu 00:00-12:00", "Fri"])
    assert [(r.day, r.start.hour, r.end.hour) for r in rules] == [(3, 0, 12), (4, 0, 23)]
    assert _holidays(None) == () and _booking_exclusions(None) == ()


@pytest.mark.parametrize("bad", ["next tuesday", "2026-13-40", "2026/11/11", 5])
def test_a_bad_holiday_is_refused_at_config_load(bad: Any) -> None:
    from blumkin.providers.kind import ProviderConfigError

    with pytest.raises(ProviderConfigError, match="holidays"):
        _holidays([bad])
    with pytest.raises(ProviderConfigError, match="booking_exclusions"):
        _booking_exclusions(["noday 09:00-10:00"])
    with pytest.raises(ProviderConfigError, match="end after it starts"):
        _booking_exclusions(["mon 12:00-09:00"])


# --------------------------------------------------------------------------- the dispatch hook


def test_a_holiday_is_refused_before_anything_is_booked() -> None:
    provider = _provider()

    with pytest.raises(BookingGuardError, match="holiday: Wednesday 2026-11-11 is a holiday"):
        _create(provider, "2026-11-11T10:00")

    provider.calendar_create.assert_not_awaited()


def test_the_refusal_is_a_usage_error_that_tells_the_agent_not_to_override_on_its_own() -> None:
    provider = _provider()
    with pytest.raises(BookingGuardError) as caught:
        _create(provider, "2026-11-14T10:00")  # a Saturday

    info = classify_exception(caught.value)

    assert info.slug == "booking_blocked" and info.exit_code == 2
    assert "--allow weekend" in (info.hint or "")
    assert "Do not retry with --allow on your own" in (info.agent_instructions or "")


def test_allow_overrides_exactly_the_named_check_and_the_result_says_so() -> None:
    provider = _provider()

    payload = _create(provider, "2026-11-11T10:00", allow=["holiday"])

    provider.calendar_create.assert_awaited_once()
    checks = payload["booking_checks"]
    assert checks["allowed"] == ["holiday: Wednesday 2026-11-11 is a holiday (Veterans Day)"]
    assert "hours" in checks["checked"] and "busy" in checks["checked"]
    # Allowing one check never allows another.
    with pytest.raises(BookingGuardError, match="weekend"):
        _create(provider, "2026-11-14T10:00", allow=["holiday"])


def test_a_clear_tuesday_is_booked_and_reports_what_was_checked() -> None:
    provider = _provider()

    payload = _create(provider, "2026-11-10T10:00")

    provider.calendar_freebusy.assert_awaited_once()
    assert provider.calendar_freebusy.await_args.kwargs["with_emails"] == ["me@example.com"]
    assert payload["booking_checks"] == {
        "allowed": [],
        "checked": ["busy", "exclusion", "holiday", "hours", "weekend"],
        "warnings": [],
    }


def test_a_busy_slot_refuses_the_booking() -> None:
    busy = _item(
        busy=[
            {
                "end": "2026-11-10T10:45:00-05:00",
                "start": "2026-11-10T10:15:00-05:00",
                "status": "busy",
            }
        ]
    )
    provider = _provider([busy])

    with pytest.raises(BookingGuardError, match="busy: busy 10:15-10:45"):
        _create(provider, "2026-11-10T10:00")

    provider.calendar_create.assert_not_awaited()


def test_an_unreadable_schedule_warns_and_still_books() -> None:
    provider = _provider(RuntimeError("graph is down"))

    payload = _create(provider, "2026-11-10T10:00")

    provider.calendar_create.assert_awaited_once()
    assert any(
        "could not read availability (RuntimeError)" in w
        for w in payload["booking_checks"]["warnings"]
    )
    assert "hours" not in payload["booking_checks"]["checked"]


def test_an_all_day_event_is_not_a_meeting_and_is_not_checked() -> None:
    provider = _provider()

    payload = _create(provider, "2026-11-11", all_day=True)

    provider.calendar_freebusy.assert_not_awaited()
    assert "booking_checks" not in payload


def test_an_unknown_allow_name_is_a_usage_error_before_anything_is_read() -> None:
    provider = _provider()

    with pytest.raises(ValueError, match="--allow takes one of"):
        _create(provider, "2026-11-10T10:00", allow=["lunch"])

    provider.calendar_freebusy.assert_not_awaited()


def test_a_config_without_an_address_is_checked_on_its_dates_and_says_so() -> None:
    provider = _provider()

    payload = asyncio.run(
        run_skill(
            "calendar.create",
            {"subject": "Sync", "start": "2026-11-10T10:00", "tz": "America/New_York"},
            config=_config(email=""),
            provider=provider,
        )
    )

    provider.calendar_freebusy.assert_not_awaited()
    assert any(
        "your own address is not in the config" in w for w in payload["booking_checks"]["warnings"]
    )


# --------------------------------------------------------------------------- an update


def _event(start: str, end: str, attendees: list[str] | None = None) -> dict[str, Any]:
    return {
        "event": {
            "attendees": [{"email": a} for a in attendees or []],
            "end": end,
            "id": "evt-1",
            "is_all_day": False,
            "start": start,
        }
    }


def _update(provider: Any, **kwargs: Any) -> Any:
    base = {"event_id": "evt-1", "tz_name": "America/New_York"}
    return asyncio.run(
        check_booking(provider, _config(), "calendar.update", {**base, **kwargs}, {"allow": []})
    )


def test_an_update_that_changes_neither_time_nor_attendees_is_not_checked() -> None:
    provider = _provider(calendar_get=AsyncMock())

    assert _update(provider, subject="New title") is None

    provider.calendar_freebusy.assert_not_awaited()
    provider.calendar_get.assert_not_awaited()


def test_inviting_someone_checks_their_hours_and_the_event_is_not_its_own_conflict() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")
    own_busy = [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]
    night = _item(
        "ana@example.com",
        hours={
            "days_of_week": _WEEKDAYS,
            "end": "17:00:00",
            "start": "09:00:00",
            "timezone": "Asia/Tokyo",
        },
    )
    provider = _provider([_item(busy=own_busy), night], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError) as caught:
        _update(provider, with_emails=["ana@example.com", "ME@example.com"])

    assert [f.check for f in caught.value.findings] == [
        "hours"
    ]  # not "busy": it is the event itself
    assert caught.value.findings[0].who == "ana@example.com"
    asked = provider.calendar_freebusy.await_args.kwargs["with_emails"]
    assert asked == ["me@example.com", "ana@example.com"]  # the organizer once, the guest once


def test_moving_an_event_keeps_its_length_and_checks_the_new_slot() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T11:00:00-05:00")
    provider = _provider([_item()], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError, match="weekend"):
        _update(provider, start_raw="2026-11-14T10:00")

    assert provider.calendar_freebusy.await_args.kwargs[
        "end"
    ] - provider.calendar_freebusy.await_args.kwargs["start"] == (
        datetime(2026, 11, 14, 11, 0, tzinfo=_NY) - datetime(2026, 11, 14, 10, 0, tzinfo=_NY)
    )


def test_an_event_that_cannot_be_read_is_not_waved_through() -> None:
    provider = _provider(calendar_get=AsyncMock(return_value={"event": {}}))

    with pytest.raises(ValueError, match="could not read the event's current time"):
        _update(provider, start_raw="2026-11-10T10:00")


# --------------------------------------------------------------------------- review follow-ups


def _google_item(address: str, busy: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the Google provider returns: no availability string and no working hours."""
    return {
        "availability_view": None,
        "busy": busy or [],
        "error": None,
        "schedule": address,
        "timezone": None,
        "working_hours": None,
    }


def test_a_free_google_schedule_is_a_result_not_an_unreadable_one() -> None:
    found, warnings, checked = schedule_findings(
        *_slot("2026-11-10", 10), _google_item("ana@example.com"), who="ana@example.com"
    )

    assert found == [] and checked == {"busy"}  # busy WAS checked; hours were not
    assert warnings == ["no working hours known for ana@example.com: hours not checked"]


def test_a_busy_google_slot_blocks_and_a_microsoft_empty_view_still_warns() -> None:
    busy = [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]

    found, _, _ = schedule_findings(
        *_slot("2026-11-10", 10), _google_item("a@x.com", busy), who="a@x.com"
    )
    _, warnings, checked = schedule_findings(
        *_slot("2026-11-10", 10), _item(view="", busy=None), who="ana@example.com"
    )

    assert [f.check for f in found] == ["busy"]
    assert any("nothing usable" in w for w in warnings) and "busy" not in checked


def test_a_google_free_slot_is_booked_with_a_warning_about_the_hours_it_could_not_see() -> None:
    provider = _provider([_google_item("me@example.com")])

    payload = _create(provider, "2026-11-10T10:00")

    provider.calendar_create.assert_awaited_once()
    assert "busy" in payload["booking_checks"]["checked"]
    assert any("no working hours known" in w for w in payload["booking_checks"]["warnings"])


def test_a_bare_weekday_exclusion_covers_the_whole_day_to_the_last_minute() -> None:
    prefs = PreferencesConfig(booking_exclusions=_booking_exclusions(["fri"]))
    last_minute = datetime(2026, 11, 13, 23, 59, tzinfo=_NY)

    found = slot_findings(last_minute, datetime(2026, 11, 13, 23, 59, 30, tzinfo=_NY), prefs)

    assert [f.check for f in found] == ["exclusion"]


def test_a_date_only_start_on_an_update_is_rejected_like_it_is_on_create() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")
    provider = _provider([_item()], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(ValueError, match="date"):
        _update(provider, start_raw="2026-11-11")


def test_holidays_and_exclusions_load_from_a_profiles_preferences(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.config import load_config

    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc"\n'
        "[profiles.default.preferences]\n"
        'holidays = ["2026-11-11 Veterans Day"]\n'
        'booking_exclusions = ["thu 00:00-12:00"]\n'
    )
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))

    prefs = load_config().preferences

    assert prefs.holidays == ((date(2026, 11, 11), "Veterans Day"),)
    assert [(r.day, r.text) for r in prefs.booking_exclusions] == [(3, "thu 00:00-12:00")]


@pytest.mark.parametrize(
    "args",
    [
        ["calendar", "create", "--subject", "S", "--start", "2026-11-14T10:00"],
        ["calendar", "update", "--event-id", "e1", "--subject", "S", "--yes"],
    ],
)
def test_the_allow_option_of_both_calendar_commands_reaches_the_skill(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from blumkin.cli import main

    seen: dict[str, Any] = {}

    async def fake_run_skill(skill_id: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        seen.update(arguments)
        return {"event": {"id": "e1"}}

    monkeypatch.setattr("blumkin.cli.run_skill", fake_run_skill)
    monkeypatch.setattr("blumkin.cli._load_config", lambda: _config())
    monkeypatch.setattr("blumkin.cli._workspace", lambda: object())

    result = CliRunner().invoke(main, [*args, "--allow", "weekend", "--allow", "busy", "--json"])

    assert result.exit_code == 0, result.output
    assert seen["allow"] == ["weekend", "busy"]
    bad = CliRunner().invoke(main, [*args, "--allow", "lunch", "--json"])
    assert bad.exit_code == 2  # not one of the five checks
