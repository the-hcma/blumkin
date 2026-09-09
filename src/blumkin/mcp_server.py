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
import tomllib
from typing import Any

from blumkin.config import list_profiles, load_config
from blumkin.providers.kind import ProviderConfigError
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
# A synthetic read-only tool (not a catalog skill) so an agent can enumerate the
# accounts this server can act as without shell access - issue #227.
_PROFILES_LIST_TOOL = "profiles.list"
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


def build_tools(
    *,
    read_only: bool = False,
    only: tuple[str, ...] = (),
    profiles: list[dict[str, Any]] | None = None,
    pinned: bool = False,
) -> list[types.Tool]:
    profiles = profiles or []
    multi = not pinned and len(profiles) >= 2
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
                input_schema=_input_schema(skill, profiles=profiles, multi=multi),
                annotations=_annotations(skill),
                _meta=meta,
            )
        )
    if _selected(_PROFILES_LIST_TOOL, only):
        tools.append(_profiles_list_tool())
    return tools


def build_server(
    *, profile: str | None = None, read_only: bool = False, only: tuple[str, ...] = ()
) -> Server:
    pinned = profile is not None
    profiles = _safe_list_profiles()
    multi = not pinned and len(profiles) >= 2
    profile_names = {item["name"] for item in profiles}
    tools = build_tools(read_only=read_only, only=only, profiles=profiles, pinned=pinned)
    names = {tool.name for tool in tools}
    bool_props = {
        tool.name: {
            key
            for key, spec in tool.input_schema["properties"].items()
            if spec.get("type") == "boolean"
        }
        for tool in tools
    }

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(_ctx: Any, params: Any) -> types.CallToolResult:
        if params.name == _PROFILES_LIST_TOOL and _PROFILES_LIST_TOOL in names:
            return _profiles_list_result(profiles, pinned_name=profile)
        if params.name not in names:
            return _error_result(LookupError(f"unknown tool: {params.name}"))
        args: dict[str, Any] = dict(params.arguments or {})
        # Per-call account selection (issue #227): a pinned server ignores it, a
        # multi-account server requires it, a single-account server may omit it.
        try:
            selected_profile = _effective_profile(
                args.pop("profile", None),
                pinned_name=profile,
                multi=multi,
                names=profile_names,
            )
        except ValueError as exc:
            return _error_result(exc)
        # `yes` is the internal consent token; a client consents via `confirm`.
        # An inbound `yes` (e.g. `"yes": "false"`, truthy) would smuggle past the gate.
        if "yes" in args:
            return _error_result(ValueError("pass `confirm`, not `yes`, to consent"))
        # The low-level Server does not type-check arguments against the schema, so
        # a model can send `"confirm": "false"` (truthy) or `"enable": "false"`.
        # Every property advertised as boolean must arrive as a real JSON bool -
        # `bool("false")` must never become a silent yes or a flipped flag.
        for key, value in args.items():
            if key in bool_props[params.name] and not isinstance(value, bool):
                return _error_result(ValueError(f"`{key}` must be a JSON boolean (true or false)"))
        if "confirm" in args:
            args["yes"] = args.pop("confirm")
        if "on" in args:
            on = args.pop("on")
            args["on"], args["off"] = on, not on
        try:
            payload = await run_skill(
                params.name, args, config=load_config(profile=selected_profile)
            )
        except Exception as exc:  # noqa: BLE001 - classify_exception owns the taxonomy
            return _error_result(exc)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

    return Server(
        "blumkin",
        version=build_version(),
        instructions=_server_instructions(profiles, pinned_name=profile),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
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


def _effective_profile(
    requested: Any, *, pinned_name: str | None, multi: bool, names: set[str]
) -> str | None:
    """Resolve the account for one call. Raises :class:`ValueError` (-> usage_error)
    when the client must choose and did not, or sent one to a pinned server."""
    if pinned_name is not None:
        if requested is not None:
            raise ValueError(
                f"this server is pinned to profile {pinned_name!r}; remove the `profile` argument"
            )
        return pinned_name
    if requested is not None:
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("`profile` must be a non-empty string")
        return requested.strip()
    if multi:
        raise ValueError(
            "this blumkin server serves multiple accounts - pass `profile` (one of: "
            f"{', '.join(sorted(names))}); call `profiles.list` for provider and email"
        )
    return None


def _input_schema(
    skill: dict[str, Any],
    *,
    profiles: list[dict[str, Any]] | None = None,
    multi: bool = False,
) -> dict[str, Any]:
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
    if multi and profiles:
        props["profile"] = {
            "type": "string",
            "enum": [item["name"] for item in profiles],
            "description": _profile_arg_description(profiles),
        }
        required.append("profile")
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


def _profile_arg_description(profiles: list[dict[str, Any]]) -> str:
    listed = "; ".join(_profile_one_line(item) for item in profiles)
    return (
        f"Which configured account to act as - one of: {listed}. Required. If the "
        "user's request does not make the account obvious, ask them; do not assume "
        "the default."
    )


def _profile_one_line(profile: dict[str, Any]) -> str:
    bits = [f"{profile['name']} ({profile['provider']}, {profile['email'] or 'no email recorded'})"]
    if profile.get("tags"):
        bits.append(f"tags: {', '.join(profile['tags'])}")
    if profile.get("is_default"):
        bits.append("default")
    return " - ".join(bits)


def _profiles_list_result(
    profiles: list[dict[str, Any]], *, pinned_name: str | None
) -> types.CallToolResult:
    body: dict[str, Any] = {
        "ok": True,
        "pinned_profile": pinned_name,
        "profiles": [
            {
                "name": item["name"],
                "provider": item["provider"],
                "email": item["email"],
                "tags": item["tags"],
                "is_default": item["is_default"],
            }
            for item in profiles
        ],
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body))],
        structured_content=body,
    )


def _profiles_list_tool() -> types.Tool:
    return types.Tool(
        name=_PROFILES_LIST_TOOL,
        description=(
            "List the blumkin accounts this server can act as (name, provider, email, "
            "tags, default). Read-only; touches no account."
        ),
        input_schema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(
            read_only_hint=True, destructive_hint=False, open_world_hint=True
        ),
        _meta=None,
    )


def _safe_list_profiles() -> list[dict[str, Any]]:
    """Configured profiles for schema generation, or ``[]`` if the config cannot be
    read. A malformed / unreadable ``config.toml`` must not stop the server from
    starting - the per-call ``load_config`` still surfaces the error in-band."""
    try:
        return list_profiles()
    except OSError, ProviderConfigError, tomllib.TOMLDecodeError:
        return []


def _selected(tool_id: str, only: tuple[str, ...]) -> bool:
    return not only or any(tool_id == p or tool_id.startswith(f"{p}.") for p in only)


async def _serve_async(*, profile: str | None, read_only: bool, only: tuple[str, ...]) -> None:
    server = build_server(profile=profile, read_only=read_only, only=only)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _server_instructions(profiles: list[dict[str, Any]], *, pinned_name: str | None) -> str | None:
    if pinned_name is not None:
        match = next((item for item in profiles if item["name"] == pinned_name), None)
        who = f" ({_profile_one_line(match)})" if match else ""
        return (
            f"This blumkin server is pinned to profile {pinned_name!r}{who}. Every tool "
            "acts as that account; the `profile` argument is not accepted."
        )
    if not profiles:
        return None
    listed = "\n".join(f"  - {_profile_one_line(item)}" for item in profiles)
    if len(profiles) == 1:
        return (
            f"This blumkin server acts as a single account:\n{listed}\n"
            "The `profile` argument is optional."
        )
    return (
        "blumkin serves more than one Microsoft 365 / Google account. Every tool call "
        f"must set `profile` to one of:\n{listed}\n"
        "If the user's request does not clearly indicate which account (e.g. \"email my "
        'sister" with both a work and a personal profile), ask them which account to use '
        "- do not guess or fall back to the default. Call `profiles.list` to re-read this set."
    )


def _tool_arg_key(name: str) -> str:
    return name.lstrip("-").replace("-", "_")
