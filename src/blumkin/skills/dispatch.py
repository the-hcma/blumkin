"""One execution path for every provider-backed skill.

:func:`run_skill` takes a skill id and a plain argument dict (the shape the CLI
callbacks and the MCP server both produce), applies the catalog's ``param`` /
``coerce`` metadata plus a small set of per-skill preprocessors, runs the consent
and add-on-scope gates, and calls the matching :class:`WorkspaceProvider` method.
Exceptions propagate unchanged - callers classify them with
:func:`blumkin.skills.errors.classify_exception`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from blumkin.config import BlumkinConfig
from blumkin.providers import get_provider
from blumkin.providers.kind import ProviderKind
from blumkin.skills import (
    DOCS_SKILLS,
    DRIVE_SKILLS,
    SKILL_METHOD_OVERRIDES,
    WO1162425_SKILLS,
    describe_skill,
)
from blumkin.skills.calendar import parse_local_datetime
from blumkin.skills.calendar_writes import parse_duration, parse_recurrence
from blumkin.skills.errors import ConsentRequiredError, ScopeAddonDisabledError

# Either key satisfies the notify gate. The MCP server maps its synthetic
# ``confirm`` boolean onto ``yes`` before calling run_skill.
_CONSENT_KEYS = ("yes", "confirm")

_DOCS_SCOPES_MESSAGE = (
    "docs create needs the Files.ReadWrite Graph scope, which is off. It uploads "
    "a .docx to your OneDrive."
)
_DOCS_SCOPES_HINT = (
    "Set docs_scopes = true in config.toml (once the tenant has granted "
    "Files.ReadWrite), delete the token cache and auth record, then run "
    "`blumkin auth login`. On a Google profile no toggle is needed."
)
_DRIVE_SCOPES_MESSAGE = (
    "drive skills need the Files.ReadWrite Graph scope on Microsoft, which is off "
    "(the same grant docs create uses - there is no separate drive toggle)."
)
_DRIVE_SCOPES_HINT = (
    "Set docs_scopes = true in config.toml (once the tenant has granted "
    "Files.ReadWrite), delete the token cache and auth record, then run "
    "`blumkin auth login`. On a Google profile no toggle is needed."
)
_WO1162425_MESSAGE = (
    "WO1162425 add-on scopes are disabled. Calendar, mail, and chat read "
    "skills work without them; chat write, meeting skills, people resolve, "
    "and mail auto-reply do not."
)
_WO1162425_HINT = (
    "Set wo1162425_scopes = true in config.toml once Remedy WO1162425 has "
    "granted its add-ons (at least Chat.ReadWrite, MailboxSettings.ReadWrite, "
    "OnlineMeetings.ReadWrite, People.Read; see HANDOFF.md, some asks may still "
    "be pending), then delete "
    "the token cache and auth record and run `blumkin auth login`."
)
_YES_HINT_DEFAULT = "This action notifies other people. Re-run the command with --yes to confirm."
_YES_HINT_AUTO_REPLY = (
    "This changes your mailbox auto-reply setting. Re-run the command with --yes to confirm."
)
_YES_HINT_TRANSCRIPTION = (
    "This changes a meeting setting (allowTranscription). Re-run the command with --yes to confirm."
)


def _argkey(name: str) -> str:
    return name.lstrip("-").replace("-", "_")


def _consent_given(arguments: dict[str, Any]) -> bool:
    return any(bool(arguments.get(key)) for key in _CONSENT_KEYS)


def _tristate(arguments: dict[str, Any], on_key: str, off_key: str) -> bool | None:
    if arguments.get(on_key):
        return True
    if arguments.get(off_key):
        return False
    return None


def _zone(tz_name: str | None, config: BlumkinConfig) -> ZoneInfo:
    return ZoneInfo(tz_name or config.default_tz)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part) for part in value]


def _coerce(value: Any, *, arg: dict[str, Any], tz_name: str | None, config: BlumkinConfig) -> Any:
    coerce = arg.get("coerce")
    if coerce == "list":
        return _as_list(value)
    if coerce == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
    if coerce == "duration":
        return parse_duration(str(value))
    if coerce == "local_datetime":
        return parse_local_datetime(str(value), _zone(tz_name, config))
    if coerce == "local_midnight":
        day = value.date() if isinstance(value, datetime) else value
        if not isinstance(day, date):
            day = date.fromisoformat(str(value))
        return datetime(day.year, day.month, day.day, tzinfo=_zone(tz_name, config))
    if coerce == "raw":
        return str(value)
    if coerce == "negate_flag":
        return not bool(value)

    arg_type = arg["type"]
    if arg.get("multiple"):
        return _as_list(value)
    if arg_type == "email" and isinstance(value, str) and "," in value:
        return _as_list(value)
    if arg_type == "flag":
        return bool(value)
    if arg_type == "int":
        return int(value)
    if arg_type == "datetime":
        return str(value)
    return value


# --------------------------------------------------------------------------- preprocessors


def _pp_calendar_create(kwargs: dict[str, Any], raw: dict[str, Any], config: BlumkinConfig) -> None:
    # `--with` is optional in the catalog but `calendar_create(with_emails)` has no
    # default; the CLI always passes `[]` for a solo hold, so match that over MCP.
    kwargs.setdefault("with_emails", [])
    repeat = raw.get("repeat")
    count, days, until = raw.get("count"), raw.get("days"), raw.get("until")
    raw_interval = raw.get("interval")
    interval = 1 if raw_interval is None else int(raw_interval)
    if repeat is not None:
        kwargs["recurrence"] = parse_recurrence(
            repeat=str(repeat),
            count=count,
            days=days,
            interval=interval,
            until=str(until) if until is not None else None,
        )
    elif any(v is not None for v in (until, count, days)) or interval != 1:
        raise ValueError("--interval / --until / --count / --days require --repeat")


def _pp_rsvp(kwargs: dict[str, Any], raw: dict[str, Any], config: BlumkinConfig) -> None:
    if kwargs.get("today_pending") or kwargs.get("propose_start"):
        _zone(kwargs.get("tz_name") or raw.get("tz"), config)  # ZoneInfoNotFoundError -> usage


def _pp_auto_reply(kwargs: dict[str, Any], raw: dict[str, Any], config: BlumkinConfig) -> None:
    enable = _tristate(raw, "on", "off")
    kwargs["enable"] = enable
    _dirty_keys = (
        "message",
        "message_file",
        "external_message",
        "external_audience",
        "start",
        "until",
    )
    dirty = any(kwargs.get(k) is not None for k in _dirty_keys)
    if enable is None and dirty:
        raise ValueError(
            "pass --on to turn the auto-reply on (with --message / --start / ...) "
            "or --off to clear it"
        )


_PREPROCESSORS: dict[str, Callable[[dict[str, Any], dict[str, Any], BlumkinConfig], None]] = {
    "calendar.create": _pp_calendar_create,
    "calendar.accept": _pp_rsvp,
    "calendar.decline": _pp_rsvp,
    "calendar.tentative": _pp_rsvp,
    "mail.auto-reply": _pp_auto_reply,
}


# --------------------------------------------------------------------------- gates


def _consent_mode(skill_id: str, spec_args: list[dict[str, Any]]) -> str:
    if skill_id == "mail.auto-reply":
        return "auto_reply"
    if skill_id == "meeting.transcription":
        return "transcription"
    requires_yes = any(a["name"] == "--yes" and a.get("required") for a in spec_args)
    return "always" if requires_yes else "never"


def _run_gates(
    skill_id: str, spec_args: list[dict[str, Any]], arguments: dict[str, Any], config: BlumkinConfig
) -> None:
    # docs create needs Files.ReadWrite on Microsoft (docs_scopes opt-in); Google
    # carries its own grant and needs no toggle.
    if (
        skill_id in DOCS_SKILLS
        and config.provider is ProviderKind.MICROSOFT
        and not config.docs_scopes
    ):
        raise ScopeAddonDisabledError(_DOCS_SCOPES_MESSAGE, hint=_DOCS_SCOPES_HINT)

    # drive skills reuse the same Microsoft grant (Files.ReadWrite via docs_scopes);
    # Google carries `drive` in its standard scope set (fail-fast in get_credentials).
    if (
        skill_id in DRIVE_SKILLS
        and config.provider is ProviderKind.MICROSOFT
        and not config.docs_scopes
    ):
        raise ScopeAddonDisabledError(_DRIVE_SCOPES_MESSAGE, hint=_DRIVE_SCOPES_HINT)

    # wo1162425 add-on scopes (Microsoft only)
    needs_addon = skill_id in WO1162425_SKILLS
    if skill_id == "mail.auto-reply" and _tristate(arguments, "on", "off") is not None:
        needs_addon = True
    if needs_addon and config.provider is ProviderKind.MICROSOFT and not config.wo1162425_scopes:
        raise ScopeAddonDisabledError(_WO1162425_MESSAGE, hint=_WO1162425_HINT)

    # --yes / confirm
    mode = _consent_mode(skill_id, spec_args)
    if mode == "always" and not _consent_given(arguments):
        raise ConsentRequiredError("--yes is required for this command", hint=_YES_HINT_DEFAULT)
    if mode == "auto_reply":
        if _tristate(arguments, "on", "off") is not None and not _consent_given(arguments):
            raise ConsentRequiredError(
                "--yes is required for this command", hint=_YES_HINT_AUTO_REPLY
            )
    if mode == "transcription":
        if arguments.get("enable") and not _consent_given(arguments):
            raise ConsentRequiredError(
                "--yes is required for this command", hint=_YES_HINT_TRANSCRIPTION
            )


# --------------------------------------------------------------------------- entry point


def skill_method_name(skill_id: str) -> str:
    return SKILL_METHOD_OVERRIDES.get(skill_id) or skill_id.replace(".", "_").replace("-", "_")


async def run_skill(
    skill_id: str,
    arguments: dict[str, Any],
    *,
    config: BlumkinConfig,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Dispatch one skill: coerce args, run the gates + preprocessor, call the provider."""
    spec = describe_skill(skill_id)
    if spec is None:
        raise ValueError(f"unknown skill: {skill_id}")

    _run_gates(skill_id, spec.args, arguments, config)

    tz_name = arguments.get("tz")
    kwargs: dict[str, Any] = {}
    for arg in spec.args:
        param = arg["param"]
        if param is None:
            continue
        key = _argkey(arg["name"])
        if key not in arguments or arguments[key] is None:
            continue
        kwargs[param] = _coerce(arguments[key], arg=arg, tz_name=tz_name, config=config)

    preprocess = _PREPROCESSORS.get(skill_id)
    if preprocess is not None:
        preprocess(kwargs, arguments, config)

    prov = provider if provider is not None else get_provider(config)
    method = getattr(prov, skill_method_name(skill_id))
    return await method(**kwargs)
