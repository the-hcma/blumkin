"""The booking guard: no meeting on a weekend, a holiday, off-hours or a busy slot (issue #716)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from blumkin.booking_guard import (
    CHECKS,
    Finding,
    allowed_checks,
    check_booking,
    schedule_findings,
    slot_findings,
    slot_report,
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
    assert any("your own address is not known" in w for w in payload["booking_checks"]["warnings"])


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


def test_an_invitee_busy_at_the_events_current_slot_is_a_real_conflict() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")
    same_slot = [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]
    provider = _provider(
        [_item(busy=same_slot), _item("ana@example.com", busy=same_slot)],
        calendar_get=AsyncMock(return_value=event),
    )

    with pytest.raises(BookingGuardError) as caught:
        _update(provider, with_emails=["ana@example.com"])

    # only the guest's block counts: the organizer's is the event itself
    assert [(f.check, f.who) for f in caught.value.findings] == [("busy", "ana@example.com")]


def test_exclusion_times_out_of_range_are_a_config_error_and_24_00_ends_the_day() -> None:
    from blumkin.config import ProviderConfigError

    (whole_day,) = _booking_exclusions(["thu 00:00-24:00"])

    assert whole_day.end == time.max
    with pytest.raises(ProviderConfigError, match="out of range"):
        _booking_exclusions(["thu 00:00-25:30"])


def test_moving_an_event_ignores_the_busy_block_of_people_already_on_it() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")
    event["event"]["attendees"] = [{"email": "ana@example.com"}]
    whole = [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]
    provider = _provider(
        [_item(busy=whole), _item("ana@example.com", busy=whole)],
        calendar_get=AsyncMock(return_value=event),
    )

    assert _update(provider, start_raw="2026-11-10T10:15", duration="30m") is not None


def test_a_busy_block_clipped_to_the_new_slot_is_still_the_event_itself() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")
    clipped = [  # Google clips to the window asked about: the overlap of old and new slots
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:15:00-05:00", "status": "busy"}
    ]
    provider = _provider([_item(busy=clipped)], calendar_get=AsyncMock(return_value=event))

    assert _update(provider, start_raw="2026-11-10T10:15", duration="30m") is not None


def test_a_different_meeting_inside_the_old_slot_is_still_a_conflict() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T11:00:00-05:00")
    other = [
        {"end": "2026-11-10T10:45:00-05:00", "start": "2026-11-10T10:30:00-05:00", "status": "busy"}
    ]
    provider = _provider([_item(busy=other)], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError):
        _update(provider, start_raw="2026-11-10T10:15", duration="45m")


def test_a_block_matching_the_old_start_and_the_clipped_end_is_not_the_event() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T11:00:00-05:00")
    mixed = [  # old start, but the end of the clipped overlap: a different meeting
        {"end": "2026-11-10T10:45:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]
    provider = _provider([_item(busy=mixed)], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError):
        _update(provider, start_raw="2026-11-10T10:15", duration="30m")


def test_converting_an_all_day_event_to_a_meeting_checks_the_new_slot() -> None:
    event = _event("2026-11-10T00:00:00-05:00", "2026-11-11T00:00:00-05:00")
    event["event"]["is_all_day"] = True
    provider = _provider([_item()], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError) as caught:
        _update(provider, all_day=False, start_raw="2026-11-14T10:00")  # a Saturday

    assert "weekend" in {f.check for f in caught.value.findings}


def test_an_update_that_leaves_an_all_day_event_all_day_is_not_checked() -> None:
    event = _event("2026-11-14T00:00:00-05:00", "2026-11-15T00:00:00-05:00")
    event["event"]["is_all_day"] = True
    provider = _provider([_item()], calendar_get=AsyncMock(return_value=event))

    assert _update(provider, start_raw="2026-11-14T10:00") is None


def test_two_meetings_in_the_old_slot_only_one_is_the_event_itself() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")
    twice = [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ] * 2
    provider = _provider([_item(busy=twice)], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError) as caught:
        _update(provider, start_raw="2026-11-10T10:15", duration="30m")

    assert [f.check for f in caught.value.findings] == ["busy"]


def test_a_preserved_duration_is_added_in_utc_across_a_dst_change() -> None:
    event = _event("2027-03-13T10:00:00-05:00", "2027-03-13T12:00:00-05:00")  # two hours
    late = [  # 03:30-04:30 EDT is the last hour of a two-hour event starting 01:30 EST
        {"end": "2027-03-14T04:30:00-04:00", "start": "2027-03-14T04:00:00-04:00", "status": "busy"}
    ]
    provider = _provider([_item(busy=late)], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError) as caught:
        _update(provider, start_raw="2027-03-14T01:30")

    assert "busy" in {f.check for f in caught.value.findings}


def test_a_bare_no_all_day_checks_the_midnight_meeting_it_creates() -> None:
    event = _event("2026-11-10T00:00:00-05:00", "2026-11-11T00:00:00-05:00")
    event["event"]["is_all_day"] = True
    provider = _provider([_item()], calendar_get=AsyncMock(return_value=event))

    with pytest.raises(BookingGuardError) as caught:
        _update(provider, all_day=False)

    assert "hours" in {f.check for f in caught.value.findings}  # 00:00-00:30 is off-hours


def test_the_refusal_text_carries_the_warnings_about_what_was_not_checked() -> None:
    error = BookingGuardError([Finding("holiday", "x")], warnings=["could not read availability"])

    assert "not checked: could not read availability" in str(error)


# --------------------------------------------------------------------------- calendar check (#720)


def _check(provider: Any, start: str, **extra: Any) -> dict[str, Any]:
    kwargs = {"start_raw": start, "tz_name": "America/New_York", **extra}
    return asyncio.run(slot_report(provider, _config(), kwargs))


def test_check_reports_a_clear_slot_as_ok_with_what_was_checked() -> None:
    report = _check(_provider([_item()]), "2026-11-10T10:00")

    assert report["ok"] is True and report["findings"] == [] and report["warnings"] == []
    assert set(report["checked"]) >= {"busy", "exclusion", "holiday", "hours", "weekend"}
    assert report["slot"]["start"].startswith("2026-11-10T10:00")
    assert report["slot"]["end"].startswith("2026-11-10T10:30")  # the 30m default


def test_check_reports_every_finding_and_never_refuses() -> None:
    report = _check(_provider([_item()]), "2026-11-14T10:00")  # a Saturday

    assert report["ok"] is False
    assert {f["check"] for f in report["findings"]} >= {"weekend", "hours"}


def test_check_reports_a_busy_guest_by_address_and_asks_freebusy_for_both_people() -> None:
    busy = [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]
    provider = _provider([_item(), _item("ana@example.com", busy=busy)])

    report = _check(provider, "2026-11-10T10:00", with_emails=["ana@example.com", "ME@example.com"])

    assert [(f["check"], f["who"]) for f in report["findings"]] == [("busy", "ana@example.com")]
    asked = provider.calendar_freebusy.await_args.kwargs["with_emails"]
    assert asked == ["me@example.com", "ana@example.com"]  # you once, the guest once


def test_check_does_not_call_a_guest_free_when_their_schedule_could_not_be_read() -> None:
    provider = _provider([_item(), _item("ana@example.com", error="no access")])

    report = _check(provider, "2026-11-10T10:00", with_emails=["ana@example.com"])

    assert report["ok"] is False and report["findings"] == []
    assert any("ana@example.com" in w for w in report["warnings"])


def test_check_uses_the_duration_and_rejects_a_date_only_start_and_a_missing_zone() -> None:
    report = _check(_provider([_item()]), "2026-11-10T10:00", duration="2h")
    assert report["slot"]["end"].startswith("2026-11-10T12:00")

    with pytest.raises(ValueError, match="needs a time"):
        _check(_provider([_item()]), "2026-11-10")
    with pytest.raises(ValueError, match="no time zone"):
        asyncio.run(
            slot_report(
                _provider([_item()]),
                _config(default_tz=""),
                {"start_raw": "2026-11-10T10:00", "tz_name": None},
            )
        )


def test_check_books_nothing() -> None:
    provider = _provider([_item()])

    _check(provider, "2026-11-10T10:00")

    provider.calendar_create.assert_not_awaited()


def test_check_human_output_names_the_verdict_the_findings_and_what_was_not_checked() -> None:
    from blumkin.skills.calendar import format_slot_check_human

    provider = _provider([_item(), _item("ana@example.com", error="no access")])
    report = _check(provider, "2026-11-14T10:00", with_emails=["ana@example.com"])

    text = "\n".join(format_slot_check_human(report))

    assert "NOT clear to propose" in text and "weekend:" in text and "warning:" in text
    clear = "\n".join(format_slot_check_human(_check(_provider([_item()]), "2026-11-10T10:00")))
    assert "OK to propose" in clear


def test_the_check_command_reaches_the_skill_with_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from blumkin.cli import main

    seen: dict[str, Any] = {}

    async def fake_run_skill(skill_id: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        seen.update({"skill": skill_id, **arguments})
        return {
            "checked": ["weekend"],
            "findings": [],
            "ok": True,
            "slot": {"end": "e", "start": "s", "timezone": "America/New_York"},
            "warnings": [],
        }

    monkeypatch.setattr("blumkin.cli.run_skill", fake_run_skill)
    monkeypatch.setattr("blumkin.cli._load_config", lambda: _config())
    monkeypatch.setattr("blumkin.cli._workspace", lambda: object())

    result = CliRunner().invoke(
        main,
        ["calendar", "check", "--start", "2026-11-12T10:00", "--duration", "1h",
         "--with", "ana@example.com", "--with", "bo@example.com", "--json"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert seen["skill"] == "calendar.check" and seen["duration"] == "1h"
    assert seen["start"] == "2026-11-12T10:00" and seen["with"] == [
        "ana@example.com",
        "bo@example.com",
    ]


def test_both_providers_hand_calendar_check_to_the_shared_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.providers.google_provider import GoogleWorkspaceProvider
    from blumkin.providers.microsoft import MicrosoftWorkspaceProvider

    seen: list[dict[str, Any]] = []

    async def fake_report(provider: Any, config: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        seen.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("blumkin.booking_guard.slot_report", fake_report)
    for cls in (GoogleWorkspaceProvider, MicrosoftWorkspaceProvider):
        provider = cls.__new__(cls)
        provider._config = _config()
        asyncio.run(provider.calendar_check(start_raw="2026-11-10T10:00", with_emails=("a@x.com",)))

    assert [k["with_emails"] for k in seen] == [["a@x.com"], ["a@x.com"]]
    assert all(k["start_raw"] == "2026-11-10T10:00" for k in seen)


def test_check_on_google_a_free_slot_is_ok_and_says_hours_were_not_checked() -> None:
    provider = _provider([_google_item("me@example.com"), _google_item("ana@example.com")])

    report = _check(provider, "2026-11-10T10:00", with_emails=["ana@example.com"])

    assert report["ok"] is True and report["findings"] == []
    assert "busy" in report["checked"] and "hours" not in report["checked"]
    assert sum("no working hours known" in w for w in report["warnings"]) == 2


def test_check_on_google_a_busy_slot_is_still_not_ok() -> None:
    busy = [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]
    provider = _provider([_google_item("me@example.com", busy=busy)])

    report = _check(provider, "2026-11-10T10:00")

    assert report["ok"] is False and [f["check"] for f in report["findings"]] == ["busy"]


def test_check_is_not_ok_when_a_guests_busy_time_could_not_be_read() -> None:
    unreadable = {**_google_item("ana@example.com"), "error": "notFound"}
    provider = _provider([_google_item("me@example.com"), unreadable])

    report = _check(provider, "2026-11-10T10:00", with_emails=["ana@example.com"])

    assert report["ok"] is False and report["findings"] == []


def test_check_is_not_ok_when_the_availability_lookup_itself_fails() -> None:
    report = _check(_provider(RuntimeError("down")), "2026-11-10T10:00")

    assert report["ok"] is False
    assert any("could not read availability" in w for w in report["warnings"])


def test_check_human_output_strips_terminal_control_characters_but_json_is_untouched() -> None:
    from blumkin.skills.calendar import format_slot_check_human

    nasty = "ana\x1b[31m@example.com"
    provider = _provider([_item(), _item(nasty, error="boom\x1b]0;owned\x07")])
    report = _check(provider, "2026-11-10T10:00", with_emails=[nasty])

    text = "\n".join(format_slot_check_human(report))

    assert "\x1b" not in text and "\x07" not in text
    assert any("\x1b" in w for w in report["warnings"])  # the JSON payload is left as it is


def test_check_rejects_a_zero_length_duration_before_asking_freebusy() -> None:
    provider = _provider([_item()])

    with pytest.raises(ValueError, match="duration must be positive"):
        _check(provider, "2026-11-10T10:00", duration="0m")

    provider.calendar_freebusy.assert_not_awaited()


def test_check_asks_for_a_timed_start_and_never_suggests_all_day() -> None:
    with pytest.raises(ValueError) as caught:
        _check(_provider([_item()]), "2026-11-10")

    assert "all-day" not in str(caught.value)


# --------------------------------------------------------------------------- busy text (#726)


def _block(start: str, end: str, status: str) -> dict[str, Any]:
    return {"end": end, "start": start, "status": status}


def _busy_message(block: dict[str, Any], who: str = "ana@example.com") -> str:
    found, _, _ = schedule_findings(*_slot("2026-11-16", 14), _item(busy=[block]), who=who)
    return found[0].message


def test_a_multi_day_out_of_office_block_names_its_dates_and_says_what_it_is() -> None:
    text = _busy_message(_block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof"))

    assert text == "out of office Mon 2026-11-16 03:00 → Thu 2026-11-19 03:00"
    assert "03:00-03:00" not in text


def test_a_full_day_block_from_midnight_to_midnight_names_its_dates() -> None:
    text = _busy_message(_block("2026-11-16T00:00:00-05:00", "2026-11-17T00:00:00-05:00", "oof"))

    assert text == "out of office Mon 2026-11-16 00:00 → Tue 2026-11-17 00:00"
    assert "00:00-00:00" not in text


def test_a_same_day_meeting_keeps_its_clock_times_and_status() -> None:
    text = _busy_message(_block("2026-11-16T14:00:00-05:00", "2026-11-16T14:30:00-05:00", "busy"))

    assert text == "busy 14:00-14:30 (busy)"


def test_a_same_day_out_of_office_block_says_out_of_office() -> None:
    text = _busy_message(_block("2026-11-16T13:00:00-05:00", "2026-11-16T17:00:00-05:00", "oof"))

    assert text == "out of office 13:00-17:00"


def test_a_multi_day_block_that_is_not_out_of_office_still_names_its_dates() -> None:
    text = _busy_message(
        _block("2026-11-15T00:00:00-05:00", "2026-11-17T00:00:00-05:00", "tentative")
    )

    assert text == "busy Sun 2026-11-15 00:00 → Tue 2026-11-17 00:00 (tentative)"


def test_your_own_out_of_office_block_is_a_note_and_not_a_conflict() -> None:
    oof = _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof")

    found, warnings, checked = schedule_findings(
        *_slot("2026-11-16", 14), _item(busy=[oof]), who="you"
    )

    assert found == [] and "busy" in checked
    assert len(warnings) == 1 and "out of office Mon 2026-11-16 03:00" in warnings[0]
    assert "not counted as a conflict" in warnings[0]


def test_a_real_meeting_on_your_schedule_still_blocks_next_to_your_oof_note() -> None:
    blocks = [
        _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof"),
        _block("2026-11-16T14:00:00-05:00", "2026-11-16T14:30:00-05:00", "busy"),
    ]

    found, warnings, _ = schedule_findings(*_slot("2026-11-16", 14), _item(busy=blocks), who="you")

    assert [f.message for f in found] == ["busy 14:00-14:30 (busy)"] and len(warnings) == 1


def test_an_invitees_out_of_office_block_is_a_conflict() -> None:
    oof = _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof")

    found, warnings, _ = schedule_findings(
        *_slot("2026-11-16", 14), _item("ana@example.com", busy=[oof]), who="ana@example.com"
    )

    assert [(f.check, f.who) for f in found] == [("busy", "ana@example.com")] and warnings == []


def test_check_stays_ok_when_only_your_own_out_of_office_note_is_present() -> None:
    oof = _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof")
    provider = _provider([_item(busy=[oof])])

    report = _check(provider, "2026-11-16T14:00")

    assert report["ok"] is True and report["findings"] == []
    assert any("out of office" in w for w in report["warnings"])


def test_the_guard_lets_you_book_over_your_own_oof_note_but_blocks_an_invitees() -> None:
    event = _event("2026-11-16T14:00:00-05:00", "2026-11-16T14:30:00-05:00")
    oof = [_block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof")]
    with_guest = _provider(
        [_item(busy=oof), _item("ana@example.com", busy=oof)],
        calendar_get=AsyncMock(return_value=event),
    )

    with pytest.raises(BookingGuardError) as caught:
        _update(with_guest, with_emails=["ana@example.com"])

    assert [(f.check, f.who) for f in caught.value.findings] == [("busy", "ana@example.com")]

    alone = _provider([_item(busy=oof)], calendar_get=AsyncMock(return_value=event))
    report = _update(alone, start_raw="2026-11-16T15:00")

    assert report is not None
    assert any(w.endswith("tell the user") for w in report["warnings"])


def test_check_human_output_shows_your_out_of_office_note_as_a_warning() -> None:
    from blumkin.skills.calendar import format_slot_check_human

    oof = _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof")

    text = "\n".join(
        format_slot_check_human(_check(_provider([_item(busy=[oof])]), "2026-11-16T14:00"))
    )

    assert "OK to propose" in text
    assert "warning: your calendar marks you out of office Mon 2026-11-16 03:00" in text


def test_create_books_over_your_own_oof_note_and_the_result_carries_the_warning() -> None:
    oof = [_block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof")]
    provider = _provider([_item(busy=oof)])

    payload = _create(provider, "2026-11-16T15:00")

    provider.calendar_create.assert_awaited_once()
    warnings = payload["booking_checks"]["warnings"]
    assert any(
        "out of office Mon 2026-11-16 03:00" in w and w.endswith("tell the user") for w in warnings
    )


# --------------------------------------------------------------------------- the organizer (#725)


def _busy_block() -> list[dict[str, Any]]:
    return [
        {"end": "2026-11-10T10:30:00-05:00", "start": "2026-11-10T10:00:00-05:00", "status": "busy"}
    ]


def test_the_signed_in_account_is_checked_and_not_the_profile_email_label() -> None:
    provider = _provider(
        [_item("real@example.com", busy=_busy_block())],
        calendar_organizer=AsyncMock(return_value="real@example.com"),
    )

    report = _check(provider, "2026-11-10T10:00")

    asked = provider.calendar_freebusy.await_args.kwargs["with_emails"]
    assert asked == ["real@example.com"]  # not the label, me@example.com
    assert [(f["check"], f["who"]) for f in report["findings"]] == [("busy", "you")]
    assert any(
        "profile email me@example.com is not the signed-in account real@example.com" in w
        for w in report["warnings"]
    )


def test_google_asks_about_the_primary_calendar_and_does_not_flag_the_label() -> None:
    provider = _provider(
        [_item("primary", busy=_busy_block())],
        calendar_organizer=AsyncMock(return_value="primary"),
    )

    report = _check(provider, "2026-11-10T10:00")

    assert provider.calendar_freebusy.await_args.kwargs["with_emails"] == ["primary"]
    assert [f["check"] for f in report["findings"]] == ["busy"]
    assert not any("signed-in account" in w for w in report["warnings"])


def test_a_failed_organizer_lookup_falls_back_to_the_label_and_says_so() -> None:
    provider = _provider(
        [_item("me@example.com")], calendar_organizer=AsyncMock(side_effect=RuntimeError("down"))
    )

    report = _check(provider, "2026-11-10T10:00")

    assert provider.calendar_freebusy.await_args.kwargs["with_emails"] == ["me@example.com"]
    assert any("could not resolve the signed-in account" in w for w in report["warnings"])
    assert report["ok"] is False  # the label may be another account: not a complete check


def test_a_blank_organizer_answer_with_no_label_is_not_called_free() -> None:
    provider = _provider([_item()], calendar_organizer=AsyncMock(return_value=" "))

    report = asyncio.run(
        slot_report(
            provider,
            _config(email=""),
            {"start_raw": "2026-11-10T10:00", "tz_name": "America/New_York"},
        )
    )

    assert report["ok"] is False
    assert any("your own address is not known" in w for w in report["warnings"])


def test_the_signed_in_account_is_not_checked_twice_when_it_is_also_a_guest() -> None:
    provider = _provider(
        [_item("real@example.com"), _item("ana@example.com")],
        calendar_organizer=AsyncMock(return_value="real@example.com"),
    )

    _check(
        provider,
        "2026-11-10T10:00",
        with_emails=["REAL@example.com", "ana@example.com"],
    )

    asked = provider.calendar_freebusy.await_args.kwargs["with_emails"]
    assert asked == ["real@example.com", "ana@example.com"]  # you once, the guest once


def test_the_providers_answer_who_the_organizer_is(monkeypatch: pytest.MonkeyPatch) -> None:
    from blumkin.providers.google_provider import GoogleWorkspaceProvider
    from blumkin.providers.microsoft import MicrosoftWorkspaceProvider

    seen: list[Any] = []

    async def fake_mailbox(*, config: Any = None) -> str:
        seen.append(config)
        return "real@example.com"

    monkeypatch.setattr("blumkin.providers.microsoft.calendar_organizer", fake_mailbox)
    google = GoogleWorkspaceProvider.__new__(GoogleWorkspaceProvider)
    microsoft = MicrosoftWorkspaceProvider.__new__(MicrosoftWorkspaceProvider)
    microsoft._config = _config()

    assert asyncio.run(google.calendar_organizer()) == "primary"
    assert asyncio.run(microsoft.calendar_organizer()) == "real@example.com"
    assert len(seen) == 1


def test_a_profile_email_that_is_not_the_signed_in_account_is_still_checked_as_a_guest() -> None:
    provider = _provider(
        [_item("real@example.com"), _item("me@example.com")],
        calendar_organizer=AsyncMock(return_value="real@example.com"),
    )

    _check(provider, "2026-11-10T10:00", with_emails=["me@example.com"])

    asked = provider.calendar_freebusy.await_args.kwargs["with_emails"]
    assert asked == ["real@example.com", "me@example.com"]  # the label is someone else here


def test_on_google_a_guest_listed_by_the_profile_label_is_checked_not_skipped() -> None:
    # `_config()` labels the profile `me@example.com`; Google answers with the `primary` alias.
    provider = _provider(
        [_item("primary"), _item("me@example.com", busy=_busy_block()), _item("ana@example.com")],
        calendar_organizer=AsyncMock(return_value="primary"),
    )

    report = _check(provider, "2026-11-10T10:00", with_emails=["me@example.com", "ana@example.com"])

    asked = provider.calendar_freebusy.await_args.kwargs["with_emails"]
    assert asked == ["primary", "me@example.com", "ana@example.com"]
    assert [(f["check"], f["who"]) for f in report["findings"]] == [
        ("busy", "me@example.com")
    ]  # the busy guest is found, the free primary calendar does not mask it


def test_microsoft_organizer_lookup_failure_raises_and_never_returns_the_label() -> None:
    from types import SimpleNamespace as NS
    from unittest.mock import patch

    from msgraph.generated.models.o_data_errors.o_data_error import ODataError

    from blumkin.auth import AuthRequiredError
    from blumkin.skills.calendar import calendar_organizer

    client = NS(me=NS(get=AsyncMock(side_effect=ODataError())))
    with patch("blumkin.skills.calendar.create_graph_client", return_value=client):
        with pytest.raises(AuthRequiredError):
            asyncio.run(calendar_organizer(config=_config()))


def test_a_call_with_no_meeting_slot_never_looks_up_the_organizer() -> None:
    organizer = AsyncMock(return_value="real@example.com")
    provider = _provider(calendar_organizer=organizer, calendar_get=AsyncMock())

    all_day = asyncio.run(
        run_skill(
            "calendar.create",
            {"subject": "OOO", "start": "2026-11-16", "all_day": True, "tz": "America/New_York"},
            config=_config(),
            provider=provider,
        )
    )
    metadata_only = _update(provider, subject="New title")

    assert metadata_only is None and "booking_checks" not in all_day
    organizer.assert_not_awaited()
    provider.calendar_freebusy.assert_not_awaited()


def test_a_call_with_a_slot_still_looks_up_the_organizer_once() -> None:
    organizer = AsyncMock(return_value="real@example.com")
    provider = _provider([_item("real@example.com")], calendar_organizer=organizer)

    _check(provider, "2026-11-10T10:00")

    organizer.assert_awaited_once()


# --------------------------------------------------------------------------- whose OOO (#727)


def _oof_block() -> dict[str, Any]:
    return _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof")


def _oof_event(*, mine: bool, show_as: str = "oof") -> dict[str, Any]:
    return {"id": "e1", "is_organizer": mine, "show_as": show_as}


def _with_events(*events: dict[str, Any], complete: bool = True) -> Any:
    view = AsyncMock(return_value={"complete": complete, "items": list(events)})
    return _provider([_item(busy=[_oof_block()])], calendar_view=view)


def test_an_out_of_office_event_organized_by_someone_else_is_a_note() -> None:
    provider = _with_events(_oof_event(mine=False))

    report = _check(provider, "2026-11-16T14:00")

    assert report["ok"] is True and report["findings"] == []
    assert any("organized by someone else" in w for w in report["warnings"])
    start = provider.calendar_view.await_args.kwargs["start"]
    assert start.isoformat().startswith("2026-11-16T14:00")  # the slot window, not the block


def test_an_out_of_office_event_you_organized_is_your_own_time_off_and_a_conflict() -> None:
    report = _check(_with_events(_oof_event(mine=True)), "2026-11-16T14:00")

    assert report["ok"] is False
    assert [(f["check"], f["who"]) for f in report["findings"]] == [("busy", "you")]
    assert report["findings"][0]["message"].startswith("out of office Mon 2026-11-16 03:00")


def test_one_of_your_own_events_among_a_colleagues_makes_it_a_conflict() -> None:
    provider = _with_events(_oof_event(mine=False), _oof_event(mine=True))

    assert _check(provider, "2026-11-16T14:00")["ok"] is False


def test_an_out_of_office_block_no_event_explains_is_treated_as_yours() -> None:
    # The block may come from a calendar the view did not list: the safe reading is a conflict.
    provider = _with_events(_oof_event(mine=True, show_as="busy"))  # not an out-of-office event

    report = _check(provider, "2026-11-16T14:00")

    assert report["ok"] is False and [f["check"] for f in report["findings"]] == ["busy"]


def test_unreadable_or_incomplete_events_leave_the_block_a_note() -> None:
    failing = _provider(
        [_item(busy=[_oof_block()])], calendar_view=AsyncMock(side_effect=RuntimeError("down"))
    )

    for provider in (failing, _with_events(_oof_event(mine=True), complete=False)):
        report = _check(provider, "2026-11-16T14:00")

        assert report["ok"] is True and report["findings"] == []
        assert any("could not be read" in w for w in report["warnings"])


def test_the_events_are_only_read_when_your_schedule_has_an_out_of_office_block() -> None:
    view = AsyncMock(return_value={"complete": True, "items": []})
    provider = _provider([_item()], calendar_view=view)

    _check(provider, "2026-11-10T10:00")

    view.assert_not_awaited()


def test_an_invitees_out_of_office_block_is_still_a_conflict_without_reading_events() -> None:
    view = AsyncMock(return_value={"complete": True, "items": []})
    provider = _provider(
        [_item(), _item("ana@example.com", busy=[_oof_block()])], calendar_view=view
    )

    report = _check(provider, "2026-11-16T14:00", with_emails=["ana@example.com"])

    assert [(f["check"], f["who"]) for f in report["findings"]] == [("busy", "ana@example.com")]
    view.assert_not_awaited()


def test_the_guard_blocks_a_booking_in_your_own_time_off_and_allows_a_colleagues() -> None:
    with pytest.raises(BookingGuardError) as caught:
        _create(_with_events(_oof_event(mine=True)), "2026-11-16T15:00")

    assert [f.check for f in caught.value.findings] == ["busy"]
    colleague = _with_events(_oof_event(mine=False))
    payload = _create(colleague, "2026-11-16T15:00")

    colleague.calendar_create.assert_awaited_once()
    assert any("organized by someone else" in w for w in payload["booking_checks"]["warnings"])


def test_the_event_payload_carries_the_availability_marking() -> None:
    from zoneinfo import ZoneInfo

    from blumkin.skills.calendar import _event_to_dict

    def event(**extra: Any) -> Any:
        return SimpleNamespace(
            end=None, id="e", is_all_day=True, is_organizer=False, location=None,
            online_meeting=None, organizer=None, response_status=None, start=None,
            subject="OOO", **extra,
        )  # fmt: skip

    tz = ZoneInfo("UTC")
    assert _event_to_dict(event(show_as=SimpleNamespace(value="oof")), tz)["show_as"] == "oof"
    assert _event_to_dict(event(), tz)["show_as"] is None


def test_the_event_being_moved_does_not_vouch_for_a_colleagues_out_of_office_block() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")  # id evt-1
    blocks = [
        _block("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00", "oof"),  # the moved event
        _block("2026-11-10T03:00:00-05:00", "2026-11-12T03:00:00-05:00", "oof"),  # a colleague's
    ]
    view = AsyncMock(
        return_value={
            "complete": True,
            "items": [
                {"id": "evt-1", "is_organizer": True, "show_as": "oof"},
                {"id": "colleague", "is_organizer": False, "show_as": "oof"},
            ],
        }
    )
    provider = _provider(
        [_item(busy=blocks)], calendar_get=AsyncMock(return_value=event), calendar_view=view
    )

    report = _update(provider, start_raw="2026-11-10T10:15")

    assert report is not None  # not refused: the colleague's block stays a note
    assert any("organized by someone else" in w for w in report["warnings"])


def test_calendar_view_asks_graph_for_the_availability_marking() -> None:
    """The ownership check reads `show_as`: if it left the select list, every OOO looked yours."""
    from unittest.mock import patch

    from blumkin.skills.calendar import calendar_view

    seen: dict[str, Any] = {}

    class Builder:
        async def get(self, request_config: Any) -> Any:
            seen["select"] = list(request_config.query_parameters.select)
            return SimpleNamespace(value=[], odata_next_link=None)

    async def fake_builder(client: Any, calendar: Any) -> Any:
        return Builder()

    with (
        patch("blumkin.skills.calendar.create_graph_client", return_value=object()),
        patch("blumkin.skills.calendar._graph_calendar_view_builder", fake_builder),
    ):
        asyncio.run(
            calendar_view(
                start=datetime(2026, 11, 16, tzinfo=_NY),
                end=datetime(2026, 11, 17, tzinfo=_NY),
                config=_config(),
            )
        )

    assert {"seriesMasterId", "showAs"} <= set(seen["select"])


def test_two_out_of_office_blocks_with_one_explaining_event_are_treated_as_yours() -> None:
    blocks = [
        _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof"),
        _block("2026-11-16T13:00:00-05:00", "2026-11-16T17:00:00-05:00", "oof"),
    ]
    view = AsyncMock(
        return_value={
            "complete": True,
            "items": [{"id": "c1", "is_organizer": False, "show_as": "oof"}],
        }
    )
    provider = _provider([_item(busy=blocks)], calendar_view=view)

    report = _check(provider, "2026-11-16T14:00")

    assert report["ok"] is False  # one block has no event behind it: the safe reading
    assert [f["check"] for f in report["findings"]] == ["busy", "busy"]


def test_two_out_of_office_blocks_each_with_a_colleagues_event_are_notes() -> None:
    blocks = [
        _block("2026-11-16T03:00:00-05:00", "2026-11-19T03:00:00-05:00", "oof"),
        _block("2026-11-16T13:00:00-05:00", "2026-11-16T17:00:00-05:00", "oof"),
    ]
    items = [{"id": f"c{i}", "is_organizer": False, "show_as": "oof"} for i in (1, 2)]
    view = AsyncMock(return_value={"complete": True, "items": items})
    provider = _provider([_item(busy=blocks)], calendar_view=view)

    report = _check(provider, "2026-11-16T14:00")

    assert report["ok"] is True and report["findings"] == []


def test_moving_a_recurring_series_leaves_its_occurrences_out_of_the_evidence() -> None:
    event = _event("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00")
    blocks = [
        _block("2026-11-10T10:00:00-05:00", "2026-11-10T10:30:00-05:00", "oof"),
        _block("2026-11-10T03:00:00-05:00", "2026-11-12T03:00:00-05:00", "oof"),
    ]
    items = [  # the occurrence of the series being moved, and a colleague's event
        {"id": "occ-1", "series_master_id": "evt-1", "is_organizer": True, "show_as": "oof"},
        {"id": "colleague", "is_organizer": False, "show_as": "oof"},
    ]
    view = AsyncMock(return_value={"complete": True, "items": items})
    provider = _provider(
        [_item(busy=blocks)], calendar_get=AsyncMock(return_value=event), calendar_view=view
    )

    report = _update(provider, start_raw="2026-11-10T10:15")

    assert report is not None and any("organized by someone else" in w for w in report["warnings"])


def test_an_event_that_does_not_say_who_organized_it_leaves_the_block_a_note_not_a_colleagues() -> (
    None
):
    event = {"id": "e1", "is_organizer": None, "show_as": "oof"}

    report = _check(_with_events(event), "2026-11-16T14:00")

    assert report["ok"] is True and report["findings"] == []
    assert any("do not say who organized them" in w for w in report["warnings"])
    assert not any("organized by someone else" in w for w in report["warnings"])


def test_a_missing_is_organizer_stays_unknown_in_the_event_payload() -> None:
    from zoneinfo import ZoneInfo

    from blumkin.skills.calendar import _event_to_dict

    def event(is_organizer: bool | None) -> Any:
        return SimpleNamespace(
            end=None, id="e", is_all_day=False, is_organizer=is_organizer, location=None,
            online_meeting=None, organizer=None, response_status=None, start=None, subject="x",
        )  # fmt: skip

    tz = ZoneInfo("UTC")
    assert _event_to_dict(event(None), tz)["is_organizer"] is None
    assert _event_to_dict(event(True), tz)["is_organizer"] is True
    assert _event_to_dict(event(False), tz)["is_organizer"] is False
