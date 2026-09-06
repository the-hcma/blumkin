"""Unit coverage for ``blumkin.skills.dispatch.run_skill`` (the shared execution path)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from blumkin.providers.kind import ProviderKind
from blumkin.skills.dispatch import run_skill, skill_method_name
from blumkin.skills.errors import ConsentRequiredError, ScopeAddonDisabledError

_MS = SimpleNamespace(default_tz="UTC", provider=ProviderKind.MICROSOFT, wo1162425_scopes=True)
_MS_NO_ADDON = SimpleNamespace(
    default_tz="UTC", provider=ProviderKind.MICROSOFT, wo1162425_scopes=False
)
_GOOGLE = SimpleNamespace(default_tz="UTC", provider=ProviderKind.GOOGLE, wo1162425_scopes=False)


def _provider(*methods: str) -> SimpleNamespace:
    return SimpleNamespace(**{name: AsyncMock(return_value={"ok": True}) for name in methods})


def _run(skill_id: str, args: dict[str, Any], *, config: Any = _MS, provider: Any) -> None:
    asyncio.run(run_skill(skill_id, args, config=config, provider=provider))


def test_method_name_resolution_including_overrides() -> None:
    assert skill_method_name("calendar.today") == "calendar_today"
    assert skill_method_name("mail.auto-reply") == "mail_auto_reply"
    assert skill_method_name("chat.attachments") == "chat_attachments_list"
    assert skill_method_name("mail.attachments") == "mail_attachments_list"


def test_param_rename_and_tz_name() -> None:
    prov = _provider("calendar_today")
    _run("calendar.today", {"date": "2026-09-10", "tz": "America/New_York"}, provider=prov)
    kwargs = prov.calendar_today.await_args.kwargs
    assert kwargs == {"day": date(2026, 9, 10), "tz_name": "America/New_York"}


def test_calendar_view_folds_from_to_into_a_midnight_range() -> None:
    prov = _provider("calendar_view")
    _run("calendar.view", {"from": "2026-09-01", "to": "2026-09-08", "tz": "UTC"}, provider=prov)
    kwargs = prov.calendar_view.await_args.kwargs
    assert kwargs["start"] == datetime(2026, 9, 1, tzinfo=kwargs["start"].tzinfo)
    assert kwargs["end"] == datetime(2026, 9, 8, tzinfo=kwargs["end"].tzinfo)
    assert "tz" not in kwargs and "tz_name" not in kwargs


def test_mail_list_since_is_tz_aware_only_when_present() -> None:
    prov = _provider("mail_list")
    _run("mail.list", {"from": "dana", "since": "2026-09-01T09:00", "tz": "UTC"}, provider=prov)
    kwargs = prov.mail_list.await_args.kwargs
    assert kwargs["sender"] == "dana"
    assert isinstance(kwargs["since"], datetime) and kwargs["since"].tzinfo is not None
    assert "until" not in kwargs


def test_freebusy_and_suggest_coercions() -> None:
    prov = _provider("calendar_suggest")
    _run(
        "calendar.suggest",
        {
            "with": ["a@x.com"],
            "start": "2026-09-01T09:00",
            "end": "2026-09-01T18:00",
            "duration": "45m",
            "limit": 5,
        },
        provider=prov,
    )
    kwargs = prov.calendar_suggest.await_args.kwargs
    assert kwargs["with_emails"] == ["a@x.com"]
    assert kwargs["duration"] == timedelta(minutes=45)
    assert isinstance(kwargs["start"], datetime)


def test_calendar_create_recurrence_and_require_repeat() -> None:
    prov = _provider("calendar_create")
    _run(
        "calendar.create",
        {
            "subject": "x",
            "start": "2026-09-22T09:00",
            "repeat": "weekly",
            "count": 3,
            "interval": 1,
            "with": [],
            "teams": True,
            "yes": True,
        },
        provider=prov,
    )
    assert prov.calendar_create.await_args.kwargs["recurrence"] is not None

    with pytest.raises(ValueError, match="require --repeat"):
        _run(
            "calendar.create",
            {
                "subject": "x",
                "start": "2026-09-22T09:00",
                "count": 3,
                "interval": 1,
                "with": [],
                "teams": True,
                "yes": True,
            },
            provider=_provider("calendar_create"),
        )


def test_rsvp_zone_precheck_raises_on_bad_zone() -> None:
    from zoneinfo import ZoneInfoNotFoundError

    with pytest.raises(ZoneInfoNotFoundError):
        _run(
            "calendar.decline",
            {"today_pending": True, "tz": "Not/AZone", "yes": True},
            provider=_provider("calendar_decline"),
        )


def test_auto_reply_tristate_and_dirty_guard() -> None:
    prov = _provider("mail_auto_reply")
    _run("mail.auto-reply", {"on": False, "off": False}, provider=prov)
    assert prov.mail_auto_reply.await_args.kwargs["enable"] is None

    with pytest.raises(ValueError, match="pass --on"):
        _run("mail.auto-reply", {"on": False, "off": False, "message": "brb"}, provider=prov)


def test_consent_gate_fires_for_notify_and_required_yes_skills() -> None:
    with pytest.raises(ConsentRequiredError):
        _run("chat.send", {"with": "Ada", "text": "hi"}, provider=_provider("chat_send"))
    with pytest.raises(ConsentRequiredError):
        _run("mail.delete", {"id": ["m1"]}, provider=_provider("mail_delete"))
    # confirm= is an accepted synonym for --yes (the MCP path)
    _run("mail.delete", {"id": ["m1"], "confirm": True}, provider=_provider("mail_delete"))


def test_wo1162425_gate_microsoft_only() -> None:
    with pytest.raises(ScopeAddonDisabledError):
        _run(
            "chat.send",
            {"with": "Ada", "text": "hi", "yes": True},
            config=_MS_NO_ADDON,
            provider=_provider("chat_send"),
        )
    # Google grants the equivalent via its own consent - no gate.
    _run(
        "people.resolve",
        {"name": "Ada"},
        config=_GOOGLE,
        provider=_provider("people_resolve"),
    )


def test_wo1162425_gate_runs_before_consent() -> None:
    # chat.send with neither --yes nor the add-on scope: the add-on error wins,
    # matching the CLI ordering.
    with pytest.raises(ScopeAddonDisabledError):
        _run(
            "chat.send",
            {"with": "Ada", "text": "hi"},
            config=_MS_NO_ADDON,
            provider=_provider("chat_send"),
        )
