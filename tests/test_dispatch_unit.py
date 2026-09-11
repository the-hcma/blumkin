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


def _run_payload(
    skill_id: str, args: dict[str, Any], *, config: Any = _MS, provider: Any
) -> dict[str, Any]:
    return asyncio.run(run_skill(skill_id, args, config=config, provider=provider))


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


def test_mail_draft_attach_preserves_an_empty_element_for_the_loud_failure() -> None:
    # Review follow-up: an empty/whitespace-only --attach element must reach
    # mail_draft as an empty string, not be silently dropped - the provider's
    # _read_attachment(""), not dispatch, is what turns it into a loud
    # MailAttachError ("not a directory"). Silently dropping it here would send
    # the message with no attachment and exit 0 instead of failing.
    prov = _provider("mail_draft")
    _run(
        "mail.draft",
        {"to": ["a@x.com"], "subject": "s", "attach": [""]},
        provider=prov,
    )
    kwargs = prov.mail_draft.await_args.kwargs
    assert kwargs["attach"] == [""]


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
    # Review follow-up on #251: `calendar.suggest --with` is `multiple: True` but,
    # like every other comma-splitting `multiple` arg in the catalog (--to, --cc,
    # --id, ...), it also carries an explicit `coerce: "list"` entry - handled by
    # `_coerce`'s `coerce == "list"` branch, not the `arg.get("multiple")` one this
    # PR touches. That branch is unreachable for any cataloged `email` arg today,
    # so this only pins the (previously untested) coerce="list" path: the MCP shape
    # a schema-following client sends when it joins several addresses into one
    # string rather than an array.
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


def test_calendar_suggest_with_splits_a_comma_inside_one_list_element() -> None:
    # Review follow-up on #259: the CLI array shape (a real list, e.g. `--with`
    # passed once with an embedded comma, or the MCP array-with-one-joined-string
    # shape) must split the same way the bare MCP string does above - this is the
    # `coerce: "list"` contract every comma-splitting `multiple` arg shares
    # (--to, --cc, --id, --with, --optional), not something specific to --fields.
    # A bare email address never legitimately contains a literal comma, so
    # splitting here is always the intended outcome for this field.
    prov = _provider("calendar_suggest")
    _run(
        "calendar.suggest",
        {
            "with": ["a@x.com, b@y.com"],
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


# --------------------------------------------------------------------------- issue #257


def _items_payload(*items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"items": list(items), **extra}


def _items_provider(method_name: str, payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**{method_name: AsyncMock(return_value=payload)})


def test_postprocess_truncates_a_long_body_preview_by_default() -> None:
    long_preview = "x" * 500
    payload = _items_payload({"subject": "s", "body_preview": long_preview})
    result = _run_payload("mail.list", {"top": 10}, provider=_items_provider("mail_list", payload))
    preview = result["items"][0]["body_preview"]
    assert preview == "x" * 150 + "..."
    assert len(preview) == 153


def test_postprocess_does_not_truncate_a_short_body_preview() -> None:
    payload = _items_payload({"subject": "s", "body_preview": "short preview"})
    result = _run_payload("mail.list", {"top": 10}, provider=_items_provider("mail_list", payload))
    assert result["items"][0]["body_preview"] == "short preview"


def test_postprocess_truncation_boundary_is_exact() -> None:
    # Review follow-up: every other truncation test used a preview far longer
    # than 150 chars (or far shorter), so a `>` -> `>=` typo at the cutoff would
    # not have failed anything. Pin exactly-150 (unchanged, no ellipsis) and
    # exactly-151 (truncated) directly.
    at_limit = _items_payload({"subject": "s", "body_preview": "x" * 150})
    over_limit = _items_payload({"subject": "s", "body_preview": "x" * 151})
    at_result = _run_payload(
        "mail.list", {"top": 10}, provider=_items_provider("mail_list", at_limit)
    )
    over_result = _run_payload(
        "mail.list", {"top": 10}, provider=_items_provider("mail_list", over_limit)
    )
    assert at_result["items"][0]["body_preview"] == "x" * 150
    assert over_result["items"][0]["body_preview"] == "x" * 150 + "..."


def test_postprocess_fields_narrows_items_to_exactly_the_requested_keys() -> None:
    payload = _items_payload(
        {
            "created": "2026-01-01",
            "from_email": "a@x.com",
            "subject": "s",
            "body_preview": "p",
            "id": "m1",
            "to_email": "b@x.com",
        },
        count=1,
        folder="inbox",
    )
    result = _run_payload(
        "mail.list",
        {"top": 10, "fields": ["subject", "from_email"]},
        provider=_items_provider("mail_list", payload),
    )
    assert set(result["items"][0]) == {"subject", "from_email"}
    assert result["items"][0] == {"subject": "s", "from_email": "a@x.com"}
    assert result["count"] == 1
    assert result["folder"] == "inbox"


def test_postprocess_fields_preserves_requested_order_per_item() -> None:
    payload = _items_payload({"subject": "s", "from_email": "a@x.com"})
    forward = _run_payload(
        "mail.list",
        {"top": 10, "fields": ["subject", "from_email"]},
        provider=_items_provider("mail_list", payload),
    )
    reverse = _run_payload(
        "mail.list",
        {"top": 10, "fields": ["from_email", "subject"]},
        provider=_items_provider("mail_list", payload),
    )
    assert list(forward["items"][0]) == ["subject", "from_email"]
    assert list(reverse["items"][0]) == ["from_email", "subject"]


def test_postprocess_unknown_field_raises_usage_shaped_value_error() -> None:
    payload = _items_payload({"subject": "s", "from_email": "a@x.com"})
    with pytest.raises(ValueError, match="bogus_field") as exc:
        _run_payload(
            "mail.list",
            {"top": 10, "fields": ["bogus_field"]},
            provider=_items_provider("mail_list", payload),
        )
    assert "subject" in str(exc.value)
    assert "from_email" in str(exc.value)


def test_postprocess_fields_is_a_noop_on_an_empty_items_list() -> None:
    payload = _items_payload(count=0)
    result = _run_payload(
        "mail.list",
        {"top": 10, "fields": ["subject"]},
        provider=_items_provider("mail_list", payload),
    )
    assert result["items"] == []


def test_postprocess_fields_accepts_comma_separated_string() -> None:
    # The MCP shape: a bare comma-joined string.
    payload = _items_payload({"subject": "s", "from_email": "a@x.com", "id": "m1"})
    result = _run_payload(
        "mail.list",
        {"top": 10, "fields": "subject,from_email"},
        provider=_items_provider("mail_list", payload),
    )
    assert set(result["items"][0]) == {"subject", "from_email"}


def test_postprocess_fields_accepts_the_cli_repeatable_shape() -> None:
    # Review follow-up (blocker): Click's `multiple=True` never splits on commas
    # itself, so a single `--fields a,b` CLI invocation arrives as a *list*
    # containing one comma-joined string (["a,b"]), not the two-element list a
    # bare MCP string would produce. Previously only the MCP shape was tested,
    # which hid that this list form fell straight through `_as_list` unsplit and
    # was then rejected by `_filter_fields` as one unknown field.
    payload = _items_payload({"subject": "s", "from_email": "a@x.com", "id": "m1"})
    result = _run_payload(
        "mail.list",
        {"top": 10, "fields": ["subject,from_email"]},
        provider=_items_provider("mail_list", payload),
    )
    assert set(result["items"][0]) == {"subject", "from_email"}

    # The other CLI form: two separate --fields flags, already a real two-element
    # list with no embedded commas - must keep working unchanged.
    result = _run_payload(
        "mail.list",
        {"top": 10, "fields": ["subject", "from_email"]},
        provider=_items_provider("mail_list", payload),
    )
    assert set(result["items"][0]) == {"subject", "from_email"}


def test_postprocess_fields_validates_against_the_union_of_item_keys() -> None:
    # Review follow-up: mail.thread --full adds body/body_type to every item, but
    # only when --full was passed - validating against items[0] alone is
    # incidentally correct only because that key happens to be uniform within one
    # response. Pin the union explicitly with items that legitimately differ.
    payload = _items_payload(
        {"subject": "s1", "id": "m1"},
        {"subject": "s2", "id": "m2", "folder": "inbox"},
    )
    result = _run_payload(
        "mail.search",
        {"query": "q", "fields": ["folder"]},
        provider=_items_provider("mail_search", payload),
    )
    assert result["items"][0] == {"folder": None}
    assert result["items"][1] == {"folder": "inbox"}


@pytest.mark.parametrize(
    ("skill_id", "method_name", "arguments"),
    [
        (
            "calendar.freebusy",
            "calendar_freebusy",
            {"with": ["a@x.com"], "start": "2026-09-01T09:00", "end": "2026-09-01T18:00"},
        ),
        ("calendar.view", "calendar_view", {"from": "2026-09-01", "to": "2026-09-08"}),
        ("mail.inbox", "mail_inbox", {}),
        ("mail.list", "mail_list", {"top": 10}),
        ("mail.search", "mail_search", {"query": "q"}),
        ("mail.thread", "mail_thread", {"id": "m1"}),
    ],
)
def test_postprocess_applies_to_all_six_items_skills(
    skill_id: str, method_name: str, arguments: dict[str, Any]
) -> None:
    long_preview = "y" * 200
    if skill_id == "calendar.freebusy":
        # A schedule item has no body_preview key at all - must be a clean no-op,
        # not an error.
        item = {"email": "a@x.com"}
    else:
        item = {"subject": "s", "body_preview": long_preview}
    payload = _items_payload(item)
    result = _run_payload(skill_id, arguments, provider=_items_provider(method_name, payload))
    if skill_id == "calendar.freebusy":
        assert result["items"][0] == item
    else:
        assert result["items"][0]["body_preview"] == "y" * 150 + "..."


def test_postprocess_mail_thread_full_body_survives_truncation_and_fields() -> None:
    # Review follow-up: mail.thread is the one skill in _ITEMS_SKILLS whose items
    # gain a full `body`/`body_type` when `--full` is passed (mail.py:1277-1279).
    # The parametrized "all six skills" test above only exercises a short
    # subject/body_preview item for mail.thread, so nothing actually proves the
    # documented promise that --full's full body stays untouched while only
    # body_preview is capped - pin it directly against a real --full-shaped item.
    long_body = "b" * 300
    long_preview = "p" * 200
    payload = _items_payload(
        {"subject": "s", "body_preview": long_preview, "body": long_body, "body_type": "text"}
    )
    result = _run_payload(
        "mail.thread",
        {"id": "m1", "full": True, "fields": ["body", "body_type", "body_preview"]},
        provider=_items_provider("mail_thread", payload),
    )
    item = result["items"][0]
    assert item["body"] == long_body
    assert item["body_type"] == "text"
    assert item["body_preview"] == "p" * 150 + "..."


def test_postprocess_is_a_noop_for_skills_outside_items_skills() -> None:
    # A payload with no "items" key can't distinguish "skipped because
    # calendar.today isn't in _ITEMS_SKILLS" from "skipped because there's no
    # items list regardless of membership" - use an items-shaped payload (with a
    # long body_preview and a --fields request) so a typo'd or deleted membership
    # check would make this test fail.
    long_preview = "w" * 200
    payload = _items_payload({"subject": "s", "body_preview": long_preview})
    result = _run_payload(
        "calendar.today",
        {"fields": ["subject"]},
        provider=_items_provider("calendar_today", payload),
    )
    assert result == payload
    assert result["items"][0]["body_preview"] == long_preview


def test_postprocess_truncation_and_fields_compose() -> None:
    long_preview = "z" * 300
    payload = _items_payload({"subject": "s", "body_preview": long_preview, "id": "m1"})
    result = _run_payload(
        "mail.list",
        {"top": 10, "fields": ["body_preview", "subject"]},
        provider=_items_provider("mail_list", payload),
    )
    item = result["items"][0]
    assert set(item) == {"body_preview", "subject"}
    assert item["body_preview"] == "z" * 150 + "..."
