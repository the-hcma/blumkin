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


def test_mail_draft_attach_is_a_list_of_whole_paths_not_characters() -> None:
    # Regression for issue #250: an MCP client may send a single path as a bare
    # string rather than a one-element array. Without `multiple: True` on the
    # `--attach` spec, `_coerce` passed that string straight through, and
    # `mail_draft` iterated it character by character.
    prov = _provider("mail_draft")
    _run(
        "mail.draft",
        {"to": ["a@x.com"], "subject": "s", "attach": "/tmp/report.pdf"},
        provider=prov,
    )
    kwargs = prov.mail_draft.await_args.kwargs
    assert kwargs["attach"] == ["/tmp/report.pdf"]


def test_mail_update_draft_attach_is_a_list_of_whole_paths_not_characters() -> None:
    prov = _provider("mail_update_draft")
    _run(
        "mail.update-draft",
        {"id": "m1", "attach": "/tmp/report.pdf"},
        provider=prov,
    )
    kwargs = prov.mail_update_draft.await_args.kwargs
    assert kwargs["attach"] == ["/tmp/report.pdf"]


def test_mail_draft_attach_does_not_split_a_comma_in_the_filename() -> None:
    # Review follow-up on #250/#251: comma-splitting a bare string is a documented
    # convenience for `email` args only. `--attach` is `type: path`, so a single
    # path containing a literal comma (a real, if unusual, filename) must survive
    # as one whole path, not be torn into two nonexistent ones.
    prov = _provider("mail_draft")
    _run(
        "mail.draft",
        {"to": ["a@x.com"], "subject": "s", "attach": "/tmp/Q3, final.pdf"},
        provider=prov,
    )
    kwargs = prov.mail_draft.await_args.kwargs
    assert kwargs["attach"] == ["/tmp/Q3, final.pdf"]


def test_mail_draft_attach_list_of_paths_is_untouched() -> None:
    # A schema-following MCP client sends an array, not a string - each element is
    # already a whole path and must not be re-split on commas either.
    prov = _provider("mail_draft")
    _run(
        "mail.draft",
        {"to": ["a@x.com"], "subject": "s", "attach": ["/tmp/Q3, final.pdf", "/tmp/b.txt"]},
        provider=prov,
    )
    kwargs = prov.mail_draft.await_args.kwargs
    assert kwargs["attach"] == ["/tmp/Q3, final.pdf", "/tmp/b.txt"]


def test_calendar_suggest_with_still_splits_a_comma_separated_string() -> None:
    # Review follow-up on #251: split_commas=arg_type == "email" must still split
    # for `email` multiples like --with - only `path`/`string` multiples opt out.
    # `calendar.suggest --with` (catalog: type email, multiple True) is the MCP
    # shape a schema-following client sends when it joins several addresses into
    # one string rather than an array.
    prov = _provider("calendar_suggest")
    _run(
        "calendar.suggest",
        {
            "with": "a@x.com, b@y.com",
            "start": "2026-09-01T09:00",
            "end": "2026-09-01T18:00",
            "duration": "45m",
        },
        provider=prov,
    )
    kwargs = prov.calendar_suggest.await_args.kwargs
    assert kwargs["with_emails"] == ["a@x.com", "b@y.com"]


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
            "no_teams": False,
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


def test_calendar_create_defaults_with_emails_for_a_solo_hold() -> None:
    # Over MCP `--with` can be omitted; the provider kwarg has no default.
    prov = _provider("calendar_create")
    _run(
        "calendar.create",
        {"subject": "Focus", "start": "2026-09-22T09:00", "no_teams": True, "yes": True},
        provider=prov,
    )
    assert prov.calendar_create.await_args.kwargs["with_emails"] == []


def test_no_teams_flag_is_negated_into_the_teams_kwarg() -> None:
    prov = _provider("calendar_create")
    _run(
        "calendar.create",
        {"subject": "hold", "start": "2026-09-22T09:00", "with": [], "no_teams": True, "yes": True},
        provider=prov,
    )
    assert prov.calendar_create.await_args.kwargs["teams"] is False

    prov = _provider("calendar_update")
    _run("calendar.update", {"event_id": "e1", "no_teams": False, "yes": True}, provider=prov)
    assert prov.calendar_update.await_args.kwargs["teams"] is True

    # omitted -> not passed, provider keeps its default (leave unchanged)
    prov = _provider("calendar_update")
    _run("calendar.update", {"event_id": "e1", "no_teams": None, "yes": True}, provider=prov)
    assert "teams" not in prov.calendar_update.await_args.kwargs


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
