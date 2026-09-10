"""Unit coverage for the stdio MCP adapter (``blumkin.mcp_server``).

Hermetic: the ``mcp`` extra is optional, so skip cleanly when it is absent, and
drive the low-level server with the in-memory legacy client (no subprocess, no
network). The provider is always mocked - these tests never touch Graph.
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from blumkin.exit_codes import EXIT_USAGE
from blumkin.providers.kind import ProviderKind

pytest.importorskip("mcp")

from mcp.client import Client  # noqa: E402  (after importorskip)

from blumkin.mcp_server import build_server, build_tools  # noqa: E402

_CFG = SimpleNamespace(default_tz="UTC", provider=ProviderKind.MICROSOFT, wo1162425_scopes=True)


def _drive(coro_factory: Any) -> Any:
    async def _run() -> Any:
        async with Client(build_server(), mode="legacy") as client:
            return await coro_factory(client)

    return asyncio.run(_run())


def _drive_server(server: Any, coro_factory: Any) -> Any:
    async def _run() -> Any:
        async with Client(server, mode="legacy") as client:
            return await coro_factory(client)

    return asyncio.run(_run())


def _list_tools() -> dict[str, Any]:
    result = _drive(lambda c: c.list_tools())
    return {tool.name: tool for tool in result.tools}


def test_catalog_maps_to_tools_one_to_one() -> None:
    tools = _list_tools()
    # Bespoke (provider-less) skills must never be advertised - a call would only
    # ever return an error.
    for absent in (
        "auth.login",
        "auth.logout",
        "auth.refresh",
        "auth.status",
        "doctor",
        "mail.signature",
        "mcp.serve",
        "skills.describe",
        "skills.list",
    ):
        assert absent not in tools, absent
    # Tool name is the skill id verbatim, and every schema is a JSON object.
    for name, tool in tools.items():
        assert tool.input_schema["type"] == "object"
        assert tool.annotations.open_world_hint is True
        assert "." in name


def test_read_skill_annotations_and_schema() -> None:
    today = _list_tools()["calendar.today"]
    assert today.annotations.read_only_hint is True
    assert today.annotations.destructive_hint is False
    assert today.meta is None
    assert "confirm" not in today.input_schema["properties"]


def test_notifying_skill_carries_confirm_and_interaction_meta() -> None:
    create = _list_tools()["calendar.create"]
    assert create.annotations.read_only_hint is False
    assert create.annotations.destructive_hint is True
    assert create.meta == {"anthropic/requiresUserInteraction": True}
    props = create.input_schema["properties"]
    assert "confirm" in props and "yes" not in props
    assert "confirm" in create.input_schema["required"]


def test_read_round_trip_returns_structured_and_text() -> None:
    prov = SimpleNamespace(calendar_today=AsyncMock(return_value={"events": [{"id": "e1"}]}))
    with (
        patch("blumkin.mcp_server.load_config", return_value=_CFG),
        patch("blumkin.skills.dispatch.get_provider", return_value=prov),
    ):
        result = _drive(lambda c: c.call_tool("calendar.today", {}))
    assert result.is_error is False
    assert result.structured_content == {"events": [{"id": "e1"}]}
    assert result.content[0].text == '{"events": [{"id": "e1"}]}'
    assert prov.calendar_today.await_count == 1


def test_confirm_only_skills_require_confirm_in_schema() -> None:
    """The `_CONFIRM_SKILLS` branch of `_needs_confirm`: mutating-but-not-notifying
    tools whose consent is only satisfiable via the synthetic `confirm`."""
    tools = _list_tools()
    for sid in ("mail.auto-reply", "meeting.transcription"):
        tool = tools[sid]
        assert "confirm" in tool.input_schema["required"], sid
        assert tool.meta == {"anthropic/requiresUserInteraction": True}, sid


def test_required_yes_skills_expose_confirm_not_a_deadlock() -> None:
    """mail delete/mark/move are mutates:true, notifies_others:false, `--yes` required.
    Without a synthetic `confirm` a schema-following client could never call them."""
    tools = _list_tools()
    for sid in ("mail.delete", "mail.mark", "mail.move"):
        schema = tools[sid].input_schema
        assert "confirm" in schema["required"], sid
        assert "yes" not in schema["properties"], sid


def test_notifying_tool_without_confirm_is_a_domain_error() -> None:
    with patch("blumkin.mcp_server.load_config", return_value=_CFG):
        result = _drive(lambda c: c.call_tool("chat.send", {"with": "Ada", "text": "hi"}))
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"] == "usage_error"
    assert "--yes is required" in result.structured_content["message"]


def test_confirm_true_passes_the_gate_and_is_not_forwarded() -> None:
    prov = SimpleNamespace(chat_send=AsyncMock(return_value={"sent": True}))
    with (
        patch("blumkin.mcp_server.load_config", return_value=_CFG),
        patch("blumkin.skills.dispatch.get_provider", return_value=prov),
    ):
        result = _drive(
            lambda c: c.call_tool("chat.send", {"chat_id": "c1", "text": "hi", "confirm": True})
        )
    assert result.is_error is False
    assert result.structured_content == {"sent": True}
    prov.chat_send.assert_awaited_once()
    assert "confirm" not in prov.chat_send.await_args.kwargs
    assert "yes" not in prov.chat_send.await_args.kwargs


def test_non_boolean_bool_args_are_rejected_never_a_silent_flip() -> None:
    """Every schema-advertised boolean must arrive as a real JSON bool - `bool("false")`
    must never become a silent yes (`confirm`) or a flipped flag (`enable`)."""
    prov = SimpleNamespace(
        chat_send=AsyncMock(return_value={}),
        meeting_transcription=AsyncMock(return_value={}),
    )
    with (
        patch("blumkin.mcp_server.load_config", return_value=_CFG),
        patch("blumkin.skills.dispatch.get_provider", return_value=prov),
    ):
        bad_confirm = _drive(
            lambda c: c.call_tool("chat.send", {"chat_id": "c1", "text": "hi", "confirm": "false"})
        )
        bad_flag = _drive(
            lambda c: c.call_tool(
                "meeting.transcription",
                {"event_id": "e1", "enable": "false", "confirm": True},
            )
        )
        smuggled_yes = _drive(
            lambda c: c.call_tool("chat.send", {"chat_id": "c1", "text": "hi", "yes": "false"})
        )
    for result in (bad_confirm, bad_flag, smuggled_yes):
        assert result.is_error is True
        assert result.structured_content["error"] == "usage_error"
    prov.chat_send.assert_not_awaited()
    prov.meeting_transcription.assert_not_awaited()


def test_auto_reply_on_off_collapse_round_trips_to_enable() -> None:
    prov = SimpleNamespace(mail_auto_reply=AsyncMock(return_value={"auto_reply": {}}))
    with (
        patch("blumkin.mcp_server.load_config", return_value=_CFG),
        patch("blumkin.skills.dispatch.get_provider", return_value=prov),
    ):
        _drive(
            lambda c: c.call_tool(
                "mail.auto-reply", {"on": True, "message": "brb", "confirm": True}
            )
        )
        assert prov.mail_auto_reply.await_args.kwargs["enable"] is True
        _drive(lambda c: c.call_tool("mail.auto-reply", {"on": False, "confirm": True}))
        assert prov.mail_auto_reply.await_args.kwargs["enable"] is False


def test_provider_exception_is_classified_into_structured_content() -> None:
    from blumkin.auth import MissingScopeError

    prov = SimpleNamespace(
        calendar_today=AsyncMock(
            side_effect=MissingScopeError(
                "missing Calendars.Read",
                current=frozenset(),
                missing=frozenset({"Calendars.Read"}),
            )
        )
    )
    with (
        patch("blumkin.mcp_server.load_config", return_value=_CFG),
        patch("blumkin.skills.dispatch.get_provider", return_value=prov),
    ):
        result = _drive(lambda c: c.call_tool("calendar.today", {}))
    assert result.is_error is True
    assert result.structured_content["error"] == "missing_scope"
    assert result.structured_content["hint"]


def test_unknown_tool_is_a_domain_error() -> None:
    with patch("blumkin.mcp_server.load_config", return_value=_CFG):
        result = _drive(lambda c: c.call_tool("calendar.telepathy", {}))
    assert result.is_error is True
    assert result.structured_content["error"] == "not_found"


def test_read_only_filter_drops_mutating_tools() -> None:
    server = build_server(read_only=True)
    tools = _drive_server(server, lambda c: c.list_tools())
    names = {tool.name for tool in tools.tools}
    assert "calendar.today" in names
    assert "calendar.create" not in names
    assert "chat.send" not in names


def test_only_prefix_filter_keeps_one_family() -> None:
    server = build_server(only=("mail",))
    tools = _drive_server(server, lambda c: c.list_tools())
    names = {tool.name for tool in tools.tools}
    assert names
    assert all(name == "mail" or name.startswith("mail.") for name in names)


def test_every_exposed_schema_is_a_valid_object_schema() -> None:
    for tool in build_tools():
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        for prop in schema["properties"].values():
            assert prop["type"] in {"string", "integer", "boolean", "array"}
            if prop["type"] == "array":
                assert prop["items"]["type"] in {"string", "integer", "boolean"}
        assert set(schema.get("required", [])) <= set(schema["properties"])


@contextmanager
def _reimport_mcp_server_with(sys_module_overrides: dict[str, Any | None]) -> Any:
    """Drop the cached ``blumkin.mcp_server`` so the lazy import re-runs its body
    under the given ``sys.modules`` overrides, then restore everything."""
    import blumkin

    saved_attr = getattr(blumkin, "mcp_server", None)
    saved_mod = sys.modules.pop("blumkin.mcp_server", None)
    if hasattr(blumkin, "mcp_server"):
        delattr(blumkin, "mcp_server")
    try:
        with patch.dict(sys.modules, sys_module_overrides):
            yield
    finally:
        if saved_mod is not None:
            sys.modules["blumkin.mcp_server"] = saved_mod
        if saved_attr is not None:
            setattr(blumkin, "mcp_server", saved_attr)  # noqa: B010


def test_cli_guard_when_the_mcp_extra_is_absent() -> None:
    from click.testing import CliRunner

    from blumkin.cli import main

    # `mcp` and every submodule mcp_server.py imports must be unavailable, so its
    # module body raises and is re-wrapped as a name=None ModuleNotFoundError.
    missing: dict[str, Any | None] = dict.fromkeys(
        ["mcp", "mcp.types", "mcp.server", "mcp.server.lowlevel", "mcp.server.stdio"], None
    )
    with _reimport_mcp_server_with(missing):
        result = CliRunner().invoke(main, ["mcp", "serve"])
    assert result.exit_code == EXIT_USAGE
    assert "blumkin[mcp]" in result.output


def test_cli_guard_does_not_swallow_an_unrelated_import_error() -> None:
    from click.testing import CliRunner

    from blumkin.cli import main

    # A genuinely missing non-`mcp` module (name set, not "mcp"/"mcp.*") is a real
    # bug - it must propagate, not be reported as a missing optional extra.
    with _reimport_mcp_server_with({"blumkin.mcp_server": None}):
        result = CliRunner().invoke(main, ["mcp", "serve"], catch_exceptions=True)
    assert isinstance(result.exception, ModuleNotFoundError)
    assert result.exception.name == "blumkin.mcp_server"


def test_cli_mcp_serve_honours_the_global_profile() -> None:
    from click.testing import CliRunner

    captured: dict[str, Any] = {}

    def _fake_serve(**kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("blumkin.mcp_server.serve", _fake_serve):
        from blumkin.cli import main

        r1 = CliRunner().invoke(main, ["--profile", "work", "mcp", "serve"])
        assert r1.exit_code == 0, r1.output
        assert captured["profile"] == "work"
        CliRunner().invoke(main, ["--profile", "work", "mcp", "serve", "--profile", "home"])
        assert captured["profile"] == "home"  # command-level wins


# ------------------------------------------------------------------ per-call profile (#227)

_TWO_PROFILES = [
    {
        "name": "personal",
        "provider": "google",
        "email": "me@gmail.com",
        "tags": [],
        "is_default": False,
    },
    {
        "name": "work",
        "provider": "microsoft",
        "email": "me@corp.com",
        "tags": ["corp"],
        "is_default": True,
    },
]


@contextmanager
def _profiles(items: list[dict[str, Any]]) -> Any:
    with patch("blumkin.mcp_server.list_profiles", return_value=items):
        yield


def test_multi_account_schema_requires_profile_and_exposes_profiles_list() -> None:
    tools = {tool.name: tool for tool in build_tools(profiles=_TWO_PROFILES)}
    schema = tools["calendar.today"].input_schema
    assert schema["properties"]["profile"]["enum"] == ["personal", "work"]
    assert "profile" in schema["required"]
    assert "ask them" in schema["properties"]["profile"]["description"]
    profiles_list = tools["profiles.list"]
    assert profiles_list.input_schema["properties"] == {}
    assert profiles_list.annotations is not None
    assert profiles_list.annotations.read_only_hint is True


def test_single_account_and_pinned_servers_have_no_profile_arg() -> None:
    single = {tool.name: tool for tool in build_tools(profiles=_TWO_PROFILES[:1])}
    pinned = {tool.name: tool for tool in build_tools(profiles=_TWO_PROFILES, pinned=True)}
    assert "profile" not in single["calendar.today"].input_schema["properties"]
    assert "profile" not in pinned["calendar.today"].input_schema["properties"]


def test_multi_account_call_without_profile_is_a_usage_error_listing_names() -> None:
    with _profiles(_TWO_PROFILES):
        result = _drive(lambda c: c.call_tool("calendar.today", {}))
    assert result.is_error is True
    assert result.structured_content["error"] == "usage_error"
    assert "personal" in result.structured_content["message"]
    assert "work" in result.structured_content["message"]


def test_multi_account_call_with_profile_loads_that_config_and_strips_the_arg() -> None:
    prov = SimpleNamespace(calendar_today=AsyncMock(return_value={"events": []}))
    captured: dict[str, Any] = {}

    def _fake_load_config(*, profile: str | None = None) -> Any:
        captured["profile"] = profile
        return _CFG

    with (
        _profiles(_TWO_PROFILES),
        patch("blumkin.mcp_server.load_config", side_effect=_fake_load_config),
        patch("blumkin.skills.dispatch.get_provider", return_value=prov),
    ):
        result = _drive(lambda c: c.call_tool("calendar.today", {"profile": "work"}))
    assert result.is_error is False
    assert captured["profile"] == "work"
    assert "profile" not in prov.calendar_today.await_args.kwargs


def test_pinned_server_rejects_any_explicit_profile_argument() -> None:
    # Rejected whether it names a different account or the pin itself - the arg is
    # not in the pinned schema at all.
    for sent in ("personal", "work"):
        with _profiles(_TWO_PROFILES):
            server = build_server(profile="work")
            result = _drive_server(
                server, lambda c, s=sent: c.call_tool("calendar.today", {"profile": s})
            )
        assert result.is_error is True, sent
        assert result.structured_content["error"] == "usage_error"
        assert "pinned" in result.structured_content["message"]


def test_unreadable_config_does_not_stop_the_server_from_starting() -> None:
    """A malformed config.toml raises TOMLDecodeError from list_profiles; the
    server must still build (schema falls back to no `profile` arg)."""
    with patch(
        "blumkin.mcp_server.list_profiles",
        side_effect=tomllib.TOMLDecodeError("bad toml", "x", 0),
    ):
        server = build_server()
        tools = _drive_server(server, lambda c: c.list_tools())
    names = {tool.name for tool in tools.tools}
    assert "calendar.today" in names
    today = next(t for t in tools.tools if t.name == "calendar.today")
    assert "profile" not in today.input_schema["properties"]


def test_profiles_list_tool_returns_the_safe_summary() -> None:
    with _profiles(_TWO_PROFILES):
        result = _drive(lambda c: c.call_tool("profiles.list", {}))
    assert result.is_error is False
    body = result.structured_content
    assert [p["name"] for p in body["profiles"]] == ["personal", "work"]
    assert body["profiles"][1] == {
        "name": "work",
        "provider": "microsoft",
        "email": "me@corp.com",
        "tags": ["corp"],
        "is_default": True,
    }
    assert body["pinned_profile"] is None


def test_server_instructions_name_every_profile_and_say_to_ask() -> None:
    with _profiles(_TWO_PROFILES):
        instructions = build_server().instructions
    assert instructions is not None
    assert "personal" in instructions
    assert "work" in instructions
    assert "ask them which account" in instructions


def test_pinned_server_instructions_name_the_pin() -> None:
    with _profiles(_TWO_PROFILES):
        instructions = build_server(profile="work").instructions
    assert instructions is not None
    assert "pinned to profile 'work'" in instructions


def test_provider_capability_mismatch_surfaces_as_usage_error() -> None:
    """A verb with no backend for the selected provider must fail at call time with
    a usage_error, never a bare 500 (issue #227 point 4). The provider's own
    "not supported for provider=X" ValueError already classifies that way; this
    pins it for the per-call `profile` path."""
    prov = SimpleNamespace(
        drive_read=AsyncMock(
            side_effect=ValueError("drive read is not supported for provider=microsoft")
        )
    )
    cfg = SimpleNamespace(
        default_tz="UTC", provider=ProviderKind.MICROSOFT, wo1162425_scopes=True, docs_scopes=True
    )
    with (
        _profiles(_TWO_PROFILES),
        patch("blumkin.mcp_server.load_config", return_value=cfg),
        patch("blumkin.skills.dispatch.get_provider", return_value=prov),
    ):
        result = _drive(
            lambda c: c.call_tool("drive.read", {"item_id": "01ABC", "profile": "work"})
        )
    assert result.is_error is True
    assert result.structured_content["error"] == "usage_error"
    assert "not supported" in result.structured_content["message"]


def test_config_skill_people_context_is_a_tool_and_needs_no_provider(tmp_path) -> None:
    """`people.context` (CONFIG_SKILLS) is auto-exposed like a read skill, takes the
    `profile` arg under a multi-profile config, and dispatches with no provider."""
    (tmp_path / "email-context.md").write_text(
        "- Sam <sam@example.com> - colleague\n", encoding="utf-8"
    )
    cfg = SimpleNamespace(
        config_dir=tmp_path,
        profile_dir=tmp_path / "profiles" / "work",
        profile="work",
        default_tz="UTC",
        provider=ProviderKind.MICROSOFT,
        wo1162425_scopes=True,
    )
    with _profiles(_TWO_PROFILES):
        tools = {t.name: t for t in build_tools(profiles=_TWO_PROFILES)}
        assert "profile" in tools["people.context"].input_schema["required"]
        assert tools["people.context"].annotations is not None
        assert tools["people.context"].annotations.read_only_hint is True
        with patch("blumkin.mcp_server.load_config", return_value=cfg):
            result = _drive(lambda c: c.call_tool("people.context", {"profile": "work"}))
    assert result.is_error is False
    assert [x["name"] for x in result.structured_content["contacts"]] == ["Sam"]
