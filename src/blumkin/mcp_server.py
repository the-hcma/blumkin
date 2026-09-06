"""A thin stdio MCP adapter: one tool per blumkin skill, dispatched through run_skill.

The MCP host (Claude Code, Cursor CLI, GitHub Copilot CLI) spawns
``blumkin mcp serve`` as a child process, speaks JSON-RPC over stdin/stdout, and
reaps it on session close. There is no daemon and no new auth surface - the same
on-disk token cache and ``load_config`` the CLI uses.

Tools are derived 1:1 from ``skills_catalog()`` so the CLI stays the source of
truth. A tool whose ``run_skill`` consent gate demands ``--yes`` (every notifying
skill, plus ``mail delete`` / ``mark`` / ``move`` and the ``mail.auto-reply`` /
``meeting.transcription`` setting changes) carries a required synthetic
``confirm`` boolean - the MCP mirror of the CLI's ``--yes``.
"""

from __future__ import annotations

import json
from typing import Any

from blumkin.config import load_config
from blumkin.skills import BESPOKE_SKILLS, skills_catalog
from blumkin.skills.dispatch import run_skill
from blumkin.skills.errors import classify_exception
from blumkin.version import build_version

try:
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
except ModuleNotFoundError as exc:  # pragma: no cover - the CLI wrapper re-raises with a hint
    raise ModuleNotFoundError(
        "the blumkin MCP server needs the optional `mcp` dependency; "
        "install it with `pipx install 'blumkin[mcp]'`"
    ) from exc

# BESPOKE_SKILLS (auth verbs, doctor, skills.*, mail.signature, mcp.serve) have no
# WorkspaceProvider method, so they can never be dispatched - never expose them.
_EXCLUDED = BESPOKE_SKILLS
_CONFIRM_SKILLS = frozenset({"mail.auto-reply", "meeting.transcription"})
_JSON_TYPE = {
    "string": "string",
    "path": "string",
    "duration": "string",
    "iana_tz": "string",
    "email": "string",
    "enum": "string",
    "date": "string",
    "datetime": "string",
    "int": "integer",
    "flag": "boolean",
}


def build_tools(*, read_only: bool = False, only: tuple[str, ...] = ()) -> list[types.Tool]:
    tools: list[types.Tool] = []
    for skill in skills_catalog()["skills"]:
        sid = skill["id"]
        if sid in _EXCLUDED:
            continue
        if read_only and skill["mutates"]:
            continue
        if only and not any(sid == p or sid.startswith(f"{p}.") for p in only):
            continue
        meta = {"anthropic/requiresUserInteraction": True} if _needs_confirm(skill) else None
        tools.append(
            types.Tool(
                name=sid,
                description=skill["summary"],
                input_schema=_input_schema(skill),
                annotations=_annotations(skill),
                _meta=meta,
            )
        )
    return tools


def build_server(
    *, profile: str | None = None, read_only: bool = False, only: tuple[str, ...] = ()
) -> Server:
    tools = build_tools(read_only=read_only, only=only)
    names = {tool.name for tool in tools}

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(_ctx: Any, params: Any) -> types.CallToolResult:
        if params.name not in names:
            return _error_result(LookupError(f"unknown tool: {params.name}"))
        args: dict[str, Any] = dict(params.arguments or {})
        # The low-level Server does not validate arguments against the advertised
        # schema, so a model can send `"confirm": "false"`. Only a real JSON bool
        # is consent - anything else is a usage error, never a silent yes.
        for key in ("confirm", "on"):
            if key in args and not isinstance(args[key], bool):
                return _error_result(ValueError(f"`{key}` must be a JSON boolean (true or false)"))
        if "confirm" in args:
            args["yes"] = args.pop("confirm")
        if "on" in args:
            on = args.pop("on")
            args["on"], args["off"] = on, not on
        try:
            payload = await run_skill(params.name, args, config=load_config(profile=profile))
        except Exception as exc:  # noqa: BLE001 - classify_exception owns the taxonomy
            return _error_result(exc)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

    return Server(
        "blumkin", version=build_version(), on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )


def serve(
    *, profile: str | None = None, read_only: bool = False, only: tuple[str, ...] = ()
) -> None:
    """Run the stdio MCP server; blocks until the client disconnects."""
    import anyio

    anyio.run(lambda: _serve_async(profile=profile, read_only=read_only, only=only))


def _annotations(skill: dict[str, Any]) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        read_only_hint=not skill["mutates"],
        destructive_hint=skill["notifies_others"],
        open_world_hint=True,
    )


def _error_result(exc: BaseException) -> types.CallToolResult:
    info = classify_exception(exc)
    body: dict[str, Any] = {"ok": False, "error": info.slug, "message": info.message}
    if info.hint:
        body["hint"] = info.hint
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body))],
        structured_content=body,
        is_error=True,
    )


def _input_schema(skill: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for arg in skill["args"]:
        key = _tool_arg_key(arg["name"])
        if key == "yes":
            continue  # replaced by the synthetic `confirm`
        if arg["name"] in ("--on", "--off"):
            key = "on"  # collapse the tri-state to one boolean
        base: dict[str, Any] = {"type": _JSON_TYPE[arg["type"]]}
        if arg["type"] == "enum":
            base["enum"] = list(arg["values"])
        if arg["type"] == "date":
            base["format"] = "date"
        if arg.get("note"):
            base["description"] = arg["note"]
        if arg.get("multiple"):
            base = {"type": "array", "items": base}
        props.setdefault(key, base)
        if arg.get("required"):
            required.append(key)
    if _needs_confirm(skill):
        props["confirm"] = {
            "type": "boolean",
            "description": "Must be true - this notifies people or changes a shared setting.",
        }
        required.append("confirm")
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = sorted(set(required))
    return schema


def _needs_confirm(skill: dict[str, Any]) -> bool:
    """Does ``run_skill``'s consent gate demand ``--yes`` / ``confirm`` for this skill?

    Mirrors ``dispatch._consent_mode``: notifying skills and the two setting-change
    skills always, plus anything the catalog marks ``--yes`` required (``mail
    delete`` / ``mark`` / ``move``).
    """
    if skill["id"] in _CONFIRM_SKILLS:
        return True
    return any(arg["name"] == "--yes" and arg.get("required") for arg in skill["args"])


async def _serve_async(*, profile: str | None, read_only: bool, only: tuple[str, ...]) -> None:
    server = build_server(profile=profile, read_only=read_only, only=only)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _tool_arg_key(name: str) -> str:
    return name.lstrip("-").replace("-", "_")
