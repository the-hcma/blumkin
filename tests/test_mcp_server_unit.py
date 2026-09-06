"""Unit coverage for the stdio MCP adapter (``blumkin.mcp_server``).

Hermetic: the ``mcp`` extra is optional, so skip cleanly when it is absent, and
drive the low-level server with the in-memory legacy client (no subprocess, no
network). The provider is always mocked - these tests never touch Graph.
"""

from __future__ import annotations

import asyncio
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
    assert "auth.login" not in tools
    assert "auth.status" not in tools
    assert "mcp.serve" not in tools
    # Tool name is the skill id verbatim, and every schema is a JSON object.
    for name, tool in tools.items():
        assert tool.input_schema["type"] == "object"
        assert tool.annotations.open_world_hint is True
        assert "." in name or name in {"doctor"}


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


def test_notifying_tool_without_confirm_is_a_domain_error() -> None:
    with patch("blumkin.mcp_server.load_config", return_value=_CFG):
        result = _drive(lambda c: c.call_tool("chat.send", {"with": "Ada", "text": "hi"}))
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"] == "usage_error"
    assert "--yes is required" in result.structured_content["message"]


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


def test_cli_guard_when_the_mcp_extra_is_absent() -> None:
    import sys

    from click.testing import CliRunner

    import blumkin
    from blumkin.cli import main

    # Force a fresh ``from blumkin import mcp_server`` that re-runs the module body
    # with ``mcp`` unavailable, so the lazy import in the callback raises.
    saved_attr = getattr(blumkin, "mcp_server", None)
    with patch.dict(sys.modules, {"mcp": None, "blumkin.mcp_server": None}):
        if hasattr(blumkin, "mcp_server"):
            delattr(blumkin, "mcp_server")
        try:
            result = CliRunner().invoke(main, ["mcp", "serve"])
        finally:
            if saved_attr is not None:
                setattr(blumkin, "mcp_server", saved_attr)  # noqa: B010
    assert result.exit_code == EXIT_USAGE
    assert "blumkin[mcp]" in result.output
