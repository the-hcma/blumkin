"""Click CLI entrypoint for blumkin."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
import httpx

from blumkin import __version__, help_text
from blumkin.auth import SecretWriteError
from blumkin.config import BlumkinConfig, list_profiles, load_config
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_SUCCESS,
    EXIT_USAGE,
)
from blumkin.output import emit_error, emit_json, emit_lines
from blumkin.providers import get_provider
from blumkin.providers.kind import ProviderConfigError
from blumkin.providers.protocol import WorkspaceProvider
from blumkin.skills import describe_skill, skills_catalog
from blumkin.skills.calendar import (
    format_freebusy_human,
    format_suggest_human,
    format_today_human,
    format_view_human,
    parse_local_datetime,
)
from blumkin.skills.calendar_writes import (
    format_accept_human,
    format_cancel_human,
    format_create_human,
    format_update_human,
    parse_duration,
)
from blumkin.skills.chat import (
    ChatAttachmentNotFoundError,
    ChatAttachmentScopeError,
    ChatAttachmentSkippedError,
    ChatMessageNotFoundError,
    format_edit_human,
    format_find_human,
    format_last_human,
    format_send_human,
)
from blumkin.skills.chat import (
    format_attachments_download_human as format_chat_attachments_download_human,
)
from blumkin.skills.chat import (
    format_attachments_human as format_chat_attachments_human,
)
from blumkin.skills.chat import (
    format_delete_human as format_chat_delete_human,
)
from blumkin.skills.mail import (
    WELL_KNOWN_MAIL_FOLDERS,
    MailAttachError,
    MailAttachmentNotFoundError,
    MailAttachmentSkippedError,
    MailBodyFileError,
    MailDraftNotFoundError,
    MailFolderNotFoundError,
    MailMessageNotFoundError,
    format_attachments_download_human,
    format_attachments_human,
    format_delete_draft_human,
    format_draft_human,
    format_folders_human,
    format_inbox_human,
    format_list_human,
    format_reply_human,
    format_send_draft_human,
)
from blumkin.skills.mail import (
    format_get_human as format_mail_get_human,
)
from blumkin.skills.meeting import (
    format_get_human as format_meeting_get_human,
)
from blumkin.skills.meeting import format_transcription_human
from blumkin.skills.people import format_resolve_human


def _as_json(ctx: click.Context, as_json_flag: bool) -> bool:
    value = bool(ctx.obj.get("as_json") or as_json_flag)
    ctx.obj["as_json"] = value
    return value


def _cli_as_json() -> bool:
    ctx = click.get_current_context(silent=True)
    if ctx is None or not isinstance(ctx.obj, dict):
        return False
    return bool(ctx.obj.get("as_json"))


def _graph_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status from kiota/msgraph exceptions."""
    for attr in ("response_status_code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            value = getattr(response, attr, None)
            if isinstance(value, int):
                return value
    return None


def _load_config() -> BlumkinConfig:
    ctx = click.get_current_context(silent=True)
    profile: str | None = None
    if ctx is not None and isinstance(ctx.obj, dict):
        raw = ctx.obj.get("profile")
        if isinstance(raw, str) and raw.strip():
            profile = raw.strip()
    try:
        return load_config(profile=profile)
    except ProviderConfigError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=_cli_as_json())
        raise SystemExit(EXIT_USAGE) from exc


def _mail_time_bounds(
    ctx: click.Context,
    tz_flag: str | None,
    *,
    since: str | None,
    until: str | None,
) -> tuple[datetime | None, datetime | None]:
    """Parse --since/--until in the operator's timezone, as `calendar view` does."""
    if since is None and until is None:
        # Resolving a zone nobody asked about would reject a plain listing over --tz.
        return (None, None)
    tz = ZoneInfo(_tz_name(ctx, tz_flag) or _load_config().default_tz)
    return (
        None if since is None else parse_local_datetime(since, tz),
        None if until is None else parse_local_datetime(until, tz),
    )


def _raise_auth_value_error(exc: ValueError, *, as_json: bool) -> NoReturn:
    if isinstance(exc, ProviderConfigError):
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    msg = str(exc)
    if (
        "client_id" in msg
        or "Missing" in msg
        or msg.startswith("Authentication required")
        or msg.startswith("Silent token refresh failed")
    ):
        emit_error(error="auth_required", message=msg, as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    emit_error(error="usage_error", message=msg, as_json=as_json)
    raise SystemExit(EXIT_USAGE) from exc


def _raise_chat_attachment_error(exc: BaseException, *, as_json: bool) -> NoReturn:
    if isinstance(exc, ChatAttachmentScopeError):
        emit_error(error="missing_scope", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    if isinstance(exc, ChatAttachmentNotFoundError | ChatMessageNotFoundError | LookupError):
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    if isinstance(exc, ChatAttachmentSkippedError):
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    if isinstance(exc, ProviderConfigError):
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    if isinstance(exc, ValueError):
        _raise_auth_value_error(exc, as_json=as_json)
    _raise_graph_http_error(exc, as_json=as_json)


def _raise_graph_http_error(exc: BaseException, *, as_json: bool) -> NoReturn:
    if isinstance(exc, SecretWriteError):
        emit_error(error="secret_write_failed", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        emit_error(
            error="timeout",
            message=str(exc) or "Graph or token HTTP call timed out",
            as_json=as_json,
            hint=(
                "Raise graph_timeout_seconds in config.toml if needed; "
                "kill stuck blumkin PIDs; run `blumkin auth refresh` if the access "
                "token is expired."
            ),
        )
        raise SystemExit(EXIT_OTHER) from exc
    status = _graph_http_status(exc)
    if status == 401:
        emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    if status == 403:
        emit_error(error="missing_scope", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    if status == 404:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    emit_error(error="graph_error", message=str(exc), as_json=as_json)
    raise SystemExit(EXIT_OTHER) from exc


def _raise_mail_value_error(exc: ValueError, *, as_json: bool) -> NoReturn:
    _raise_auth_value_error(exc, as_json=as_json)


def _require_wo1162425_scopes(*, as_json: bool) -> None:
    cfg = _load_config()
    if cfg.wo1162425_scopes:
        return
    emit_error(
        error="usage_error",
        message=(
            "WO1162425 add-on scopes are disabled. Calendar/mail/chat read skills work "
            "without them; chat write, meeting skills, and "
            "people resolve need wo1162425_scopes = true "
            "in config.toml after Remedy WO1162425 "
            "grants its add-ons (at least Chat.ReadWrite, OnlineMeetings.ReadWrite, "
            "People.Read — see HANDOFF.md; augmented asks may still be pending) — "
            "then wipe token cache, auth record, and re-login."
        ),
        as_json=as_json,
    )
    raise SystemExit(EXIT_USAGE)


def _require_yes(*, yes: bool, as_json: bool) -> None:
    if not yes:
        emit_error(
            error="usage_error",
            message="--yes is required for this command",
            as_json=as_json,
        )
        raise SystemExit(EXIT_USAGE)


def _tz_name(ctx: click.Context, tz_flag: str | None) -> str | None:
    return tz_flag if tz_flag is not None else ctx.obj.get("tz_name")


def _workspace(config: BlumkinConfig | None = None) -> WorkspaceProvider:
    try:
        return get_provider(config if config is not None else _load_config())
    except ProviderConfigError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=_cli_as_json())
        raise SystemExit(EXIT_USAGE) from exc


@click.group(epilog=help_text.MAIN_EPILOG)
@click.option(
    "--profile",
    default=None,
    help="Profile name or unique tag (@work, google, …) to act as.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON on stdout (recommended for agents).",
)
@click.option(
    "--tz",
    "tz_name",
    default=None,
    help="IANA timezone for date/time in and out (e.g. America/New_York); default from config.",
)
@click.version_option(version=__version__, prog_name="blumkin")
@click.pass_context
def main(
    ctx: click.Context,
    as_json: bool,
    profile: str | None,
    tz_name: str | None,
) -> None:
    """Personal Microsoft 365 / Google Workspace skills CLI, acting as you.

    blumkin turns calendar, mail, Teams chat, and free/busy flows into small
    commands a coding agent (or a human) can run over the shell, using delegated
    OAuth - it acts as the signed-in user, never as an app.

    Reads work with the base scope set. Writes that notify someone (calendar
    invites, mail sends, chat messages) always require --yes. Run
    `blumkin auth login` once per machine, then `blumkin doctor` to check setup.
    """
    ctx.ensure_object(dict)
    ctx.obj["as_json"] = as_json
    ctx.obj["profile"] = profile
    ctx.obj["tz_name"] = tz_name


@main.group(epilog=help_text.AUTH_EPILOG)
def auth() -> None:
    """Sign in, check token status, refresh, and sign out.

    Delegated public-client OAuth only. The token cache and auth record are
    written under the active config dir and must never be committed.
    """


@auth.command("login", epilog=help_text.AUTH_LOGIN_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_login(ctx: click.Context, as_json_flag: bool) -> None:
    """Sign in via the system browser and cache the tokens on this machine.

    Run once per machine, or again after `auth logout` or a scope change. Writes
    the token cache and auth record under the active config dir. Use
    `auth refresh` in non-interactive shells.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        _workspace().auth_login()
    except SecretWriteError as exc:
        emit_error(
            error="secret_write_failed",
            message=str(exc),
            as_json=as_json,
            hint=(
                "Remove symlinks at the config dir or token cache/auth record "
                "paths under ~/.config/blumkin/, then retry."
            ),
        )
        raise SystemExit(EXIT_OTHER) from exc
    except ProviderConfigError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        emit_error(
            error="auth_required",
            message=str(exc),
            as_json=as_json,
            hint="Set client_id in ~/.config/blumkin/config.toml then retry.",
        )
        raise SystemExit(EXIT_AUTH) from exc
    if as_json:
        emit_json({"ok": True, "status": _workspace().auth_status()})
    else:
        emit_lines(["Signed in. Token cache written under ~/.config/blumkin/."])


@auth.command("logout", epilog=help_text.AUTH_LOGOUT_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_logout(ctx: click.Context, as_json_flag: bool) -> None:
    """Delete this machine's token cache and auth record.

    The next Graph call needs a fresh `auth login`.
    """
    as_json = _as_json(ctx, as_json_flag)
    _workspace().auth_logout()
    if as_json:
        emit_json({"ok": True})
    else:
        emit_lines(["Logged out (cache files removed)."])


@auth.command("refresh", epilog=help_text.AUTH_REFRESH_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_refresh(ctx: click.Context, as_json_flag: bool) -> None:
    """Mint a new access token from the cached refresh token (no browser).

    The agent-safe way to recover from an expired access token. Exit 3
    (auth_required) means the refresh token is gone - run `auth login` on a TTY.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = _workspace().auth_refresh()
    except SecretWriteError as exc:
        emit_error(error="secret_write_failed", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    except ProviderConfigError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        emit_error(
            error="auth_required",
            message=str(exc),
            as_json=as_json,
            hint="Run `blumkin auth login` on a TTY, then retry.",
        )
        raise SystemExit(EXIT_AUTH) from exc
    if as_json:
        emit_json({"ok": True, "status": payload})
    else:
        expires = payload.get("access_token_expires_at") or "(none)"
        emit_lines([f"Silent refresh ok. access_token_expires_at: {expires}"])


@auth.command("status", epilog=help_text.AUTH_STATUS_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_status(ctx: click.Context, as_json_flag: bool) -> None:
    """Show the config path, client-id state, and token-cache expiry.

    Read this before assuming a hang is a login problem.
    """
    as_json = _as_json(ctx, as_json_flag)
    payload = _workspace().auth_status()
    if as_json:
        emit_json(payload)
        return
    lines = [
        f"config_dir: {payload['config_dir']}",
        f"config_path: {payload['config_path']}",
        f"client_id_configured: {payload['client_id_configured']}",
        f"tenant_id: {payload['tenant_id']}",
        f"token_cache: {payload['token_cache']}",
        f"auth_record: {payload['auth_record']}",
        f"refresh_token_present: {payload['refresh_token_present']}",
    ]
    expires_at = payload.get("access_token_expires_at")
    if expires_at is None:
        lines.append("access_token_expires_at: (none)")
    else:
        remaining = payload.get("access_token_expires_in_seconds")
        expired = payload.get("access_token_expired")
        if expired:
            rel = "expired"
        elif remaining is not None:
            hours = remaining / 3600
            if hours < 1:
                rel = f"{max(remaining, 0) // 60}m left"
            else:
                rel = f"{hours:.1f}h left"
        else:
            rel = "?"
        lines.append(f"access_token_expires_at: {expires_at} ({rel})")
        lines.append(
            "note: access tokens are short-lived; a refresh token renews them without a browser"
        )
    emit_lines(lines)


@main.group(epilog=help_text.PROFILES_EPILOG)
def profiles() -> None:
    """List and inspect the account profiles in config.toml.

    Each profile is one account (Microsoft or Google). Select one on any command
    with `--profile <name-or-tag>` or the BLUMKIN_PROFILE env var.
    """


@profiles.command("list", epilog=help_text.PROFILES_LIST_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def profiles_list(ctx: click.Context, as_json_flag: bool) -> None:
    """List configured profiles: name, provider, timezone, tags, and default.

    Prefer --json in agent sessions. `count: 0` means config.toml has no
    profiles. With more than one profile and no --profile / BLUMKIN_PROFILE /
    default_profile to pick one, mail/calendar/chat commands error out.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        profiles_payload = list_profiles()
    except ProviderConfigError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    default_profile = next(
        (item["name"] for item in profiles_payload if item.get("is_default")),
        None,
    )
    payload = {
        "count": len(profiles_payload),
        "default_profile": default_profile,
        "profiles": profiles_payload,
    }
    if as_json:
        emit_json(payload)
        return
    if not profiles_payload:
        emit_lines(["(no profiles)"])
        return
    for item in profiles_payload:
        tags = ", ".join(item["tags"]) if item["tags"] else "(none)"
        marker = " (default)" if item["is_default"] else ""
        emit_lines(
            [
                f"{item['name']}{marker}: provider={item['provider']} "
                f"tz={item['default_tz'] or '(unset)'} tags={tags}"
            ]
        )


@main.group(epilog=help_text.SKILLS_EPILOG)
def skills() -> None:
    """Discover what blumkin can do, as a machine-readable catalog.

    Each skill entry carries a `notifies_others` flag - treat `true` as
    off-limits for verification runs.
    """


@skills.command("list", epilog=help_text.SKILLS_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def skills_list(ctx: click.Context, as_json_flag: bool) -> None:
    """List every skill id and one-line summary (prefer --json for agents)."""
    catalog = skills_catalog()
    if _as_json(ctx, as_json_flag):
        emit_json(catalog)
        return
    for skill in catalog["skills"]:
        emit_lines([f"{skill['id']}: {skill['summary']}"])


@skills.command("describe", epilog=help_text.SKILLS_DESCRIBE_EPILOG)
@click.argument("skill_id")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def skills_describe(ctx: click.Context, skill_id: str, as_json_flag: bool) -> None:
    """Describe one skill: CLI form, args, scopes, and mutate/notify flags.

    SKILL_ID is an id from `blumkin skills list` (e.g. calendar.create).
    """
    as_json = _as_json(ctx, as_json_flag)
    skill = describe_skill(skill_id)
    if skill is None:
        emit_error(
            error="not_found",
            message=f"Unknown skill: {skill_id}",
            as_json=as_json,
        )
        raise SystemExit(EXIT_NOT_FOUND)
    payload: dict[str, Any] = {
        "args": list(skill.args),
        "cli": list(skill.cli),
        "id": skill.id,
        "mutates": skill.mutates,
        "notifies_others": skill.notifies_others,
        "scopes": list(skill.scopes),
        "summary": skill.summary,
    }
    if as_json:
        emit_json(payload)
    else:
        emit_lines(
            [
                f"id: {skill.id}",
                f"cli: {' '.join(skill.cli)}",
                f"summary: {skill.summary}",
                f"mutates: {skill.mutates}",
                f"notifies_others: {skill.notifies_others}",
                f"scopes: {', '.join(skill.scopes) or '(none)'}",
            ]
        )


@main.command(epilog=help_text.DOCTOR_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def doctor(ctx: click.Context, as_json_flag: bool) -> None:
    """Check config, token cache, and which scope set is active.

    Exit 3 (auth_required) lists the problems to fix - usually run
    `blumkin auth login`.
    """
    as_json = _as_json(ctx, as_json_flag)
    cfg = _load_config()
    status = _workspace(cfg).auth_status()
    problems: list[str] = []
    if not status["client_id_configured"]:
        problems.append("client_id missing in config.toml")
    if not status["token_cache"] or not status["auth_record"]:
        problems.append("auth cache incomplete — run: blumkin auth login")
    payload = {
        "ok": not problems,
        "wo1162425_scopes": cfg.wo1162425_scopes,
        "problems": problems,
        "status": status,
        "skills": [s["id"] for s in skills_catalog()["skills"]],
    }
    if as_json:
        emit_json(payload)
    else:
        emit_lines([f"ok: {payload['ok']}"])
        emit_lines([f"wo1162425_scopes: {cfg.wo1162425_scopes}"])
        emit_lines([f"requested_scopes: {', '.join(status.get('requested_scopes') or [])}"])
        for problem in problems:
            emit_lines([f"problem: {problem}"])
        emit_lines([f"skills: {', '.join(payload['skills'])}"])
    if problems:
        raise SystemExit(EXIT_AUTH)


@main.group(epilog=help_text.CALENDAR_EPILOG)
def calendar() -> None:
    """Read your calendar and schedule, and create or respond to events.

    Times are local to the organizer (profile `default_tz`, or `--tz AREA`).
    Date ranges are half-open: `--to` is the first day NOT included. Anything
    that emails attendees requires --yes.
    """


@calendar.command("today", epilog=help_text.CALENDAR_TODAY_EPILOG)
@click.option(
    "--date",
    "day",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Local day to list as YYYY-MM-DD (default: today).",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def calendar_today_cmd(
    ctx: click.Context, day: Any, as_json_flag: bool, tz_flag: str | None
) -> None:
    """List events for the local day (today, or --date YYYY-MM-DD).

    Graph returns UTC; blumkin converts to --tz or the config default. Use
    --json to get event ids for accept / cancel / update.
    """
    as_json = _as_json(ctx, as_json_flag)
    tz_name = _tz_name(ctx, tz_flag)
    day_value: date | None = day.date() if day is not None else None
    try:
        payload = asyncio.run(_workspace().calendar_today(day=day_value, tz_name=tz_name))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_today_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("view", epilog=help_text.CALENDAR_VIEW_EPILOG)
@click.option(
    "--from",
    "from_day",
    required=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="First local day to include (YYYY-MM-DD).",
)
@click.option(
    "--to",
    "to_day",
    required=True,
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="First local day to EXCLUDE (YYYY-MM-DD); range is half-open.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def calendar_view_cmd(
    ctx: click.Context,
    from_day: Any,
    to_day: Any,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """List events across a local date range [--from, --to).

    The range is half-open: `--to` is the first day NOT shown, so
    `--from 2026-09-01 --to 2026-09-08` covers exactly that week.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        cfg = _load_config()
        tz = ZoneInfo(_tz_name(ctx, tz_flag) or cfg.default_tz)
        start = datetime(from_day.year, from_day.month, from_day.day, tzinfo=tz)
        end = datetime(to_day.year, to_day.month, to_day.day, tzinfo=tz)
        payload = asyncio.run(_workspace().calendar_view(start=start, end=end))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_view_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("freebusy", epilog=help_text.CALENDAR_FREEBUSY_EPILOG)
@click.option(
    "--with",
    "with_emails",
    multiple=True,
    required=True,
    help="Email to query; repeat for several people.",
)
@click.option(
    "--start",
    "start_raw",
    required=True,
    help="Local window start, YYYY-MM-DDTHH:MM.",
)
@click.option(
    "--end",
    "end_raw",
    required=True,
    help="Local window end, YYYY-MM-DDTHH:MM.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def calendar_freebusy_cmd(
    ctx: click.Context,
    with_emails: tuple[str, ...],
    start_raw: str,
    end_raw: str,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """Show busy intervals for one or more people over a window.

    Returns busy blocks (plus each person's timezone / working hours when Graph
    exposes them), not free slots. For ranked mutual-free start times, use
    `calendar suggest`. Do not use this to guess someone's address.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        cfg = _load_config()
        tz = ZoneInfo(_tz_name(ctx, tz_flag) or cfg.default_tz)
        start = parse_local_datetime(start_raw, tz)
        end = parse_local_datetime(end_raw, tz)
        payload = asyncio.run(
            _workspace().calendar_freebusy(with_emails=list(with_emails), start=start, end=end)
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_freebusy_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("suggest", epilog=help_text.CALENDAR_SUGGEST_EPILOG)
@click.option(
    "--with",
    "with_emails",
    multiple=True,
    required=True,
    help="People who must be free; repeat once per person (include yourself if needed).",
)
@click.option(
    "--start",
    "start_raw",
    required=True,
    help="Earliest local start to consider, YYYY-MM-DDTHH:MM.",
)
@click.option(
    "--end",
    "end_raw",
    required=True,
    help="Latest local end to consider, YYYY-MM-DDTHH:MM.",
)
@click.option(
    "--duration",
    default="30m",
    show_default=True,
    help="Meeting length (e.g. 45m, 1h).",
)
@click.option(
    "--window",
    default=None,
    help="Optional local day clip HH:MM-HH:MM (e.g. 09:00-18:00).",
)
@click.option(
    "--treat-tentative",
    "treat_tentative",
    default="busy",
    show_default=True,
    type=click.Choice(["busy", "free"], case_sensitive=False),
    help="Whether tentative blocks count as busy.",
)
@click.option(
    "--limit",
    default=10,
    show_default=True,
    type=int,
    help="Max number of suggested starts.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def calendar_suggest_cmd(
    ctx: click.Context,
    with_emails: tuple[str, ...],
    start_raw: str,
    end_raw: str,
    duration: str,
    window: str | None,
    treat_tentative: str,
    limit: int,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """Rank mutual-free start times from everyone's free/busy.

    Suggests starts only - it never creates an event. Feed a chosen start into
    `calendar create`. Clip to a working-day window with `--window HH:MM-HH:MM`.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        cfg = _load_config()
        tz = ZoneInfo(_tz_name(ctx, tz_flag) or cfg.default_tz)
        start = parse_local_datetime(start_raw, tz)
        end = parse_local_datetime(end_raw, tz)
        payload = asyncio.run(
            _workspace().calendar_suggest(
                with_emails=list(with_emails),
                start=start,
                end=end,
                duration=parse_duration(duration),
                window=window,
                treat_tentative=treat_tentative,
                limit=limit,
            )
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("freebusy lookup failed"):
            emit_error(error="graph_error", message=msg, as_json=as_json)
            raise SystemExit(EXIT_OTHER) from exc
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_suggest_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("accept", epilog=help_text.CALENDAR_ACCEPT_EPILOG)
@click.option("--event-id", "event_id", default=None, help="Single event id to accept.")
@click.option(
    "--today-pending",
    "today_pending",
    is_flag=True,
    help="Accept all not-yet-responded events for today.",
)
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_accept_cmd(
    ctx: click.Context,
    event_id: str | None,
    today_pending: bool,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Accept one invitation (--event-id) or all pending ones for today.

    Sends a response to each organizer, so --yes is required. Event ids come
    from `blumkin calendar today --json`.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        tz_name = _tz_name(ctx, tz_flag)
        if today_pending:
            cfg = _load_config()
            ZoneInfo(tz_name or cfg.default_tz)
        payload = asyncio.run(
            _workspace().calendar_accept(
                event_id=event_id,
                today_pending=today_pending,
                tz_name=tz_name,
            )
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_accept_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("cancel", epilog=help_text.CALENDAR_CANCEL_EPILOG)
@click.option("--event-id", "event_id", required=True, help="Event id to cancel (organizer only).")
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_cancel_cmd(ctx: click.Context, event_id: str, yes: bool, as_json_flag: bool) -> None:
    """Cancel an event you organize and notify every attendee. Requires --yes."""
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(_workspace().calendar_cancel(event_id=event_id))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_cancel_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("create", epilog=help_text.CALENDAR_CREATE_EPILOG)
@click.option("--subject", required=True, help="Event title.")
@click.option(
    "--with",
    "with_emails",
    multiple=True,
    required=True,
    help="Attendee email; repeat once per attendee.",
)
@click.option(
    "--start",
    "start_raw",
    required=True,
    help="Local start, YYYY-MM-DDTHH:MM in the organizer timezone.",
)
@click.option(
    "--duration",
    default="30m",
    show_default=True,
    help="Length as a short duration, e.g. 30m, 45m, 1h, 1h30m.",
)
@click.option(
    "--teams/--no-teams",
    default=True,
    show_default=True,
    help=(
        "Teams online meeting via Calendars.ReadWrite isOnlineMeeting; "
        "--no-teams for an offline hold."
    ),
)
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_create_cmd(
    ctx: click.Context,
    subject: str,
    with_emails: tuple[str, ...],
    start_raw: str,
    duration: str,
    teams: bool,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Create an event and invite the --with attendees. Requires --yes.

    A Teams online meeting is added by default; pass --no-teams for an offline
    hold. --start stays in the organizer timezone. For a cross-zone or external
    attendee, check `calendar freebusy` / `calendar suggest` first.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(
            _workspace().calendar_create(
                subject=subject,
                with_emails=list(with_emails),
                start_raw=start_raw,
                duration=duration,
                teams=teams,
                tz_name=_tz_name(ctx, tz_flag),
            )
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_create_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("update", epilog=help_text.CALENDAR_UPDATE_EPILOG)
@click.option("--event-id", required=True, help="Event id to attach a Teams meeting to.")
@click.option(
    "--teams/--no-teams",
    default=True,
    show_default=True,
    help="Attach Teams online meeting (v1 only supports enabling Teams).",
)
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_update_cmd(
    ctx: click.Context,
    event_id: str,
    teams: bool,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Attach a Teams online meeting to an existing event. Requires --yes.

    v1 only adds Teams; it cannot remove it. Uses Calendars.ReadWrite (not
    OnlineMeetings.ReadWrite). Attendees are notified.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(
            _workspace().calendar_update(
                event_id=event_id,
                teams=teams,
                tz_name=_tz_name(ctx, tz_flag),
            )
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_update_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@main.group(epilog=help_text.CHAT_EPILOG)
def chat() -> None:
    """Read Teams 1:1 chats, and send, edit, or delete your messages.

    Reads (find / last / attachments) work with the base scope set. Writes
    (send / edit / delete) need `wo1162425_scopes = true` and always require
    --yes. When a display name is ambiguous, pass --chat-id from `chat find`.
    """


@chat.group("attachments", invoke_without_command=True, epilog=help_text.CHAT_ATTACHMENTS_EPILOG)
@click.option("--chat-id", default=None, help="Chat id (exactly one of --chat-id or --with).")
@click.option("--with", "with_name", default=None, help="Display-name substring.")
@click.option("--message-id", default=None, help="Message id (exactly one of this or --latest).")
@click.option("--latest", is_flag=True, help="Use the newest message that has attachments.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_attachments_cmd(
    ctx: click.Context,
    chat_id: str | None,
    with_name: str | None,
    message_id: str | None,
    latest: bool,
    as_json_flag: bool,
) -> None:
    """List file attachments on a chat message (the default action here).

    Pass exactly one of --chat-id / --with, and one of --message-id / --latest.
    Use the `download` subcommand to fetch bytes.
    """
    if ctx.invoked_subcommand is not None:
        return
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().chat_attachments_list(
                chat_id=chat_id,
                latest=latest,
                message_id=message_id,
                with_name=with_name,
            )
        )
    except Exception as exc:
        _raise_chat_attachment_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_chat_attachments_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat_attachments_cmd.command("download", epilog=help_text.CHAT_ATTACHMENTS_DOWNLOAD_EPILOG)
@click.option("--chat-id", default=None, help="Chat id (exactly one of --chat-id or --with).")
@click.option("--with", "with_name", default=None, help="Display-name substring.")
@click.option("--message-id", default=None, help="Message id (exactly one of this or --latest).")
@click.option("--latest", is_flag=True, help="Use the newest message that has attachments.")
@click.option("--attachment-id", default=None, help="Attachment id (omit with --all).")
@click.option("--all", "download_all", is_flag=True, help="Download every downloadable file.")
@click.option("--out", required=True, type=click.Path(), help="Output file or directory.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_attachments_download_cmd(
    ctx: click.Context,
    chat_id: str | None,
    with_name: str | None,
    message_id: str | None,
    latest: bool,
    attachment_id: str | None,
    download_all: bool,
    out: str,
    as_json_flag: bool,
) -> None:
    """Download one (--attachment-id) or all (--all) files from a chat message.

    Needs the `files_scopes` opt-in. Without it, download exits 4
    (missing_scope) with a share URL to open in Teams. --out is a file path for
    one attachment, or a directory with --all.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().chat_attachments_download(
                attachment_id=attachment_id,
                chat_id=chat_id,
                download_all=download_all,
                latest=latest,
                message_id=message_id,
                out=out,
                with_name=with_name,
            )
        )
    except Exception as exc:
        _raise_chat_attachment_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_chat_attachments_download_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat.command("delete", epilog=help_text.CHAT_DELETE_EPILOG)
@click.option("--chat-id", required=True, help="Teams chat id (from `chat find`).")
@click.option("--message-id", required=True, help="Chat message id to delete (yours).")
@click.option("--yes", is_flag=True, help="Confirm soft-delete (required).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_delete_cmd(
    ctx: click.Context,
    chat_id: str,
    message_id: str,
    yes: bool,
    as_json_flag: bool,
) -> None:
    """Soft-delete one of your chat messages. Requires --yes.

    Every participant sees the message disappear. Needs
    `wo1162425_scopes = true` (Chat.ReadWrite).
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(_workspace().chat_delete(chat_id=chat_id, message_id=message_id))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_chat_delete_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat.command("edit", epilog=help_text.CHAT_EDIT_EPILOG)
@click.option("--chat-id", required=True, help="Teams chat id (from `chat find`).")
@click.option("--message-id", required=True, help="Chat message id to edit (yours).")
@click.option("--text", required=True, help="Replacement body; use ASCII hyphens, not em dashes.")
@click.option("--yes", is_flag=True, help="Confirm edit (required).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_edit_cmd(
    ctx: click.Context,
    chat_id: str,
    message_id: str,
    text: str,
    yes: bool,
    as_json_flag: bool,
) -> None:
    """Replace one of your chat message bodies in place. Requires --yes.

    Other people have already read the message. Needs
    `wo1162425_scopes = true` (Chat.ReadWrite).
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(
            _workspace().chat_edit(chat_id=chat_id, message_id=message_id, text=text)
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_edit_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat.command("find", epilog=help_text.CHAT_FIND_EPILOG)
@click.option("--with", "with_name", required=True, help="Display-name substring to match members.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_find_cmd(ctx: click.Context, with_name: str, as_json_flag: bool) -> None:
    """List chats whose members match a display-name substring.

    Use it to get a --chat-id when a name matches more than one chat.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(_workspace().chat_find(with_name=with_name))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_find_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat.command("last", epilog=help_text.CHAT_LAST_EPILOG)
@click.option("--with", "with_name", required=True, help="Display-name substring to match a chat.")
@click.option("--n", "n", default=3, show_default=True, type=int, help="How many messages to show.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_last_cmd(ctx: click.Context, with_name: str, n: int, as_json_flag: bool) -> None:
    """Show the last N messages from the chat matching --with.

    Exit 5 (not_found) means no chat matched.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(_workspace().chat_last(with_name=with_name, n=n))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_last_human(payload))
    if payload.get("chat") is None:
        raise SystemExit(EXIT_NOT_FOUND)
    raise SystemExit(EXIT_SUCCESS)


@chat.command("send", epilog=help_text.CHAT_SEND_EPILOG)
@click.option(
    "--with",
    "with_name",
    default=None,
    help="Display-name match for the recipient (exclusive with --chat-id).",
)
@click.option(
    "--chat-id",
    "chat_id",
    default=None,
    help="Explicit chat id from `chat find` (exclusive with --with).",
)
@click.option("--text", required=True, help="Message body; use ASCII hyphens, not em dashes.")
@click.option("--yes", is_flag=True, help="Confirm send (required).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_send_cmd(
    ctx: click.Context,
    with_name: str | None,
    chat_id: str | None,
    text: str,
    yes: bool,
    as_json_flag: bool,
) -> None:
    """Send a text message to a chat (by --with name or --chat-id). Requires --yes.

    This messages a real person. Needs `wo1162425_scopes = true`
    (Chat.ReadWrite). If --with is ambiguous, use --chat-id from `chat find`.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(
            _workspace().chat_send(with_name=with_name, chat_id=chat_id, text=text)
        )
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_send_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@main.group(epilog=help_text.MAIL_EPILOG)
def mail() -> None:
    """Read mail, and draft replies, forwards, and new messages.

    Every drafting verb stays in your mailbox until `mail send-draft --yes` -
    that is the only step that delivers mail. `--from` / `--subject` filter
    locally over a newest-first scan (max 500); `--search` is Graph server-side
    and cannot be combined with those filters.
    """


@mail.command("inbox", epilog=help_text.MAIL_INBOX_EPILOG)
@click.option(
    "--from", "sender", default=None, help="Sender name or address substring (local filter)."
)
@click.option("--subject", default=None, help="Subject substring (local filter).")
@click.option(
    "--search",
    default=None,
    help="Graph $search term (whole mailbox); cannot combine with --from/--subject/--since.",
)
@click.option("--since", default=None, help="Only messages at or after this local date/time.")
@click.option("--until", default=None, help="Only messages strictly before this local date/time.")
@click.option("--unread", is_flag=True, help="Only unread messages.")
@click.option("--top", default=10, show_default=True, type=int, help="Max messages to return.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def mail_inbox_cmd(
    ctx: click.Context,
    sender: str | None,
    subject: str | None,
    search: str | None,
    since: str | None,
    until: str | None,
    unread: bool,
    top: int,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """List recent inbox messages, with optional filters or full-text search.

    `--from` / `--subject` match locally over a newest-first scan capped at 500
    (the payload then reports `complete: false`). `--search` runs on Graph over
    the whole mailbox and cannot combine with the substring or date filters.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        since_dt, until_dt = _mail_time_bounds(ctx, tz_flag, since=since, until=until)
        payload = asyncio.run(
            _workspace().mail_inbox(
                top=top,
                search=search,
                sender=sender,
                subject=subject,
                since=since_dt,
                unread=unread,
                until=until_dt,
            )
        )
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except ValueError as exc:
        _raise_mail_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_inbox_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("folders", epilog=help_text.MAIL_FOLDERS_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_folders_cmd(ctx: click.Context, as_json_flag: bool) -> None:
    """List mail folders with their ids and message counts.

    Graph's totals can lag - do not treat `total: 0` as proof a folder is
    empty; confirm with `mail list --folder <name>`.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(_workspace().mail_folders())
    except ValueError as exc:
        _raise_mail_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_folders_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("get", epilog=help_text.MAIL_GET_EPILOG)
@click.option("--id", "message_id", required=True, help="Message id (from a listing).")
@click.option(
    "--body-type",
    default="text",
    show_default=True,
    type=click.Choice(["html", "text"]),
    help="Body format to request from Graph; html keeps the markup.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_get_cmd(
    ctx: click.Context,
    message_id: str,
    body_type: str,
    as_json_flag: bool,
) -> None:
    """Read one message in full: participants, timestamps, body, attachments.

    Prefer this over listing and filtering client-side once you have the id.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(_workspace().mail_get(message_id=message_id, body_type=body_type))
    except MailMessageNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_mail_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_mail_get_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("list", epilog=help_text.MAIL_LIST_EPILOG)
@click.option(
    "--folder",
    default=None,
    help=(
        f"Well-known name ({', '.join(WELL_KNOWN_MAIL_FOLDERS)}), a folder id, "
        "or a custom folder's display name (default: inbox)."
    ),
)
@click.option(
    "--orderby",
    default=None,
    type=click.Choice(["created", "received", "sent"]),
    help=(
        "Sort field; defaults to sent for Sent Items, created for Drafts/Outbox, "
        "received otherwise."
    ),
)
@click.option(
    "--from", "sender", default=None, help="Sender name or address substring (local filter)."
)
@click.option("--subject", default=None, help="Subject substring (local filter).")
@click.option(
    "--search",
    default=None,
    help="Graph $search term (whole mailbox); cannot combine with --from/--subject/--since.",
)
@click.option("--since", default=None, help="Only messages at or after this local date/time.")
@click.option("--until", default=None, help="Only messages strictly before this local date/time.")
@click.option("--unread", is_flag=True, help="Only unread messages.")
@click.option("--top", default=10, show_default=True, type=int, help="Max messages to return.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def mail_list_cmd(
    ctx: click.Context,
    folder: str | None,
    orderby: str | None,
    sender: str | None,
    subject: str | None,
    search: str | None,
    since: str | None,
    until: str | None,
    unread: bool,
    top: int,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """List recent messages from any mail folder (well-known name, id, or name).

    Sort order defaults per folder (sent for Sent Items, created for
    Drafts/Outbox, received otherwise); override with `--orderby`. Same filter
    rules as `mail inbox`.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        since_dt, until_dt = _mail_time_bounds(ctx, tz_flag, since=since, until=until)
        payload = asyncio.run(
            _workspace().mail_list(
                top=top,
                folder=folder,
                orderby=orderby,
                search=search,
                sender=sender,
                subject=subject,
                since=since_dt,
                unread=unread,
                until=until_dt,
            )
        )
    except ZoneInfoNotFoundError as exc:
        emit_error(error="usage_error", message=f"invalid timezone: {exc}", as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailFolderNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_mail_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_list_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.group("attachments", invoke_without_command=True, epilog=help_text.MAIL_ATTACHMENTS_EPILOG)
@click.option("--id", "message_id", default=None, help="Message id (from a listing).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_attachments_cmd(ctx: click.Context, message_id: str | None, as_json_flag: bool) -> None:
    """List attachments (name, size, id) on a message (the default action here).

    Use the `download` subcommand to save them.
    """
    if ctx.invoked_subcommand is not None:
        return
    as_json = _as_json(ctx, as_json_flag)
    if not message_id or not message_id.strip():
        emit_error(error="usage_error", message="--id is required", as_json=as_json)
        raise SystemExit(EXIT_USAGE)
    try:
        payload = asyncio.run(_workspace().mail_attachments_list(message_id=message_id))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except MailMessageNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_attachments_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail_attachments_cmd.command("download", epilog=help_text.MAIL_ATTACHMENTS_DOWNLOAD_EPILOG)
@click.option("--message-id", required=True, help="Message id (from a listing).")
@click.option(
    "--attachment-id",
    default=None,
    help="Attachment id from `mail attachments --id ... --json` (omit with --all).",
)
@click.option("--all", "download_all", is_flag=True, help="Download every file attachment.")
@click.option(
    "--out",
    required=True,
    type=click.Path(),
    help="Output file (single attachment) or directory (with --all).",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_attachments_download_cmd(
    ctx: click.Context,
    message_id: str,
    attachment_id: str | None,
    download_all: bool,
    out: str,
    as_json_flag: bool,
) -> None:
    """Download one (--attachment-id) or all (--all) file attachments.

    Get attachment ids from `mail attachments --id ... --json`.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().mail_attachments_download(
                message_id=message_id,
                attachment_id=attachment_id,
                download_all=download_all,
                out=out,
            )
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except MailMessageNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except MailAttachmentNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except MailAttachmentSkippedError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_attachments_download_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("delete-draft", epilog=help_text.MAIL_DELETE_DRAFT_EPILOG)
@click.option("--id", "draft_id", required=True, help="Draft message id.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_delete_draft_cmd(ctx: click.Context, draft_id: str, as_json_flag: bool) -> None:
    """Permanently delete a draft. No --yes needed - nobody is notified.

    The safe way to clean up a draft you created only to inspect it.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(_workspace().mail_delete_draft(draft_id=draft_id))
    except MailDraftNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_delete_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("draft", epilog=help_text.MAIL_DRAFT_EPILOG)
@click.option(
    "--to",
    multiple=True,
    required=True,
    help="Recipient email (repeatable or comma-separated).",
)
@click.option(
    "--cc",
    multiple=True,
    help="CC recipient email (repeatable or comma-separated).",
)
@click.option(
    "--bcc",
    multiple=True,
    help="BCC recipient email (repeatable or comma-separated).",
)
@click.option("--subject", required=True, help="Message subject.")
@click.option(
    "--attach",
    multiple=True,
    type=click.Path(dir_okay=False, path_type=str),
    help="Attach a file, under 2 MB (repeat for several).",
)
@click.option("--body", default=None, help="Message body (mutually exclusive with --body-file).")
@click.option(
    "--body-file",
    "body_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="Read body from a UTF-8 file.",
)
@click.option(
    "--body-type",
    "body_type",
    default="text",
    show_default=True,
    type=click.Choice(["text", "html"], case_sensitive=False),
    help="Body content type.",
)
@click.option(
    "--no-signature",
    "no_signature",
    is_flag=True,
    help="Do not append [mail.signature] even when enabled in config.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_draft_cmd(
    ctx: click.Context,
    to: tuple[str, ...],
    cc: tuple[str, ...],
    bcc: tuple[str, ...],
    subject: str,
    attach: tuple[str, ...],
    body: str | None,
    body_file: str | None,
    body_type: str,
    no_signature: bool,
    as_json_flag: bool,
) -> None:
    """Create a new mail draft. Does not send.

    Send it later with `mail send-draft --id ... --yes`. `--to` / `--cc` /
    `--bcc` repeat or take comma-separated lists. Use ASCII hyphens in the body,
    not em dashes.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().mail_draft(
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                attach=attach,
                body=body,
                body_file=body_file,
                body_type=body_type,
                no_signature=no_signature,
            )
        )
    except MailAttachError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailBodyFileError as exc:
        emit_error(
            error="usage_error",
            message=str(exc),
            as_json=as_json,
        )
        raise SystemExit(EXIT_USAGE) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("forward", epilog=help_text.MAIL_FORWARD_EPILOG)
@click.option("--id", "message_id", required=True, help="Message id to forward.")
@click.option("--to", required=True, help="Recipient email.")
@click.option(
    "--cc",
    multiple=True,
    help="Add CC recipients (merged with any Graph-inherited CC; repeatable or comma-separated).",
)
@click.option(
    "--bcc",
    multiple=True,
    help="Add BCC recipients (merged with any Graph-inherited BCC; repeatable or comma-separated).",
)
@click.option("--body", default=None, help="Text to add above the forwarded message.")
@click.option("--body-file", default=None, help="Read the added text from a file.")
@click.option("--body-type", default="text", show_default=True, type=click.Choice(["html", "text"]))
@click.option(
    "--no-signature",
    "no_signature",
    is_flag=True,
    help="Do not append [mail.signature] even when enabled in config.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_forward_cmd(
    ctx: click.Context,
    message_id: str,
    to: str,
    cc: tuple[str, ...],
    bcc: tuple[str, ...],
    body: str | None,
    body_file: str | None,
    body_type: str,
    no_signature: bool,
    as_json_flag: bool,
) -> None:
    """Create a forward draft for a message. Does not send.

    Pass `--body` on create; filling it in later with `mail update-draft --body`
    replaces the quoted original. `--cc` / `--bcc` on create merge with
    inherited recipients. Send with `mail send-draft --yes`.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().mail_forward(
                message_id=message_id,
                to=to,
                body=body,
                body_file=body_file,
                body_type=body_type,
                cc=cc or None,
                bcc=bcc or None,
                no_signature=no_signature,
            )
        )
    except MailBodyFileError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailMessageNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_mail_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_reply_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("reply", epilog=help_text.MAIL_REPLY_EPILOG)
@click.option("--id", "message_id", required=True, help="Message id to reply to.")
@click.option("--all", "reply_all", is_flag=True, help="Reply to every recipient (reply-all).")
@click.option(
    "--cc",
    multiple=True,
    help="Add CC recipients (merged with Graph-inherited CC; repeatable or comma-separated).",
)
@click.option(
    "--bcc",
    multiple=True,
    help="Add BCC recipients (merged with Graph-inherited BCC; repeatable or comma-separated).",
)
@click.option("--body", default=None, help="Reply text; omit for an empty draft.")
@click.option("--body-file", default=None, help="Read the reply text from a file.")
@click.option("--body-type", default="text", show_default=True, type=click.Choice(["html", "text"]))
@click.option(
    "--no-signature",
    "no_signature",
    is_flag=True,
    help="Do not append [mail.signature] even when enabled in config.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_reply_cmd(
    ctx: click.Context,
    message_id: str,
    reply_all: bool,
    cc: tuple[str, ...],
    bcc: tuple[str, ...],
    body: str | None,
    body_file: str | None,
    body_type: str,
    no_signature: bool,
    as_json_flag: bool,
) -> None:
    """Create a reply draft that threads in the recipient's client. Does not send.

    Prefer this over a fresh draft with "RE:" - Graph keeps it in the original
    conversation and inherits recipients. Pass `--body` on create; a later
    `mail update-draft --body` drops the quoted original. Send with
    `mail send-draft --yes`.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().mail_reply(
                message_id=message_id,
                body=body,
                body_file=body_file,
                body_type=body_type,
                reply_all=reply_all,
                cc=cc or None,
                bcc=bcc or None,
                no_signature=no_signature,
            )
        )
    except MailBodyFileError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailMessageNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_mail_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_reply_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("send-draft", epilog=help_text.MAIL_SEND_DRAFT_EPILOG)
@click.option("--id", "draft_id", required=True, help="Draft message id to send.")
@click.option("--yes", "yes", is_flag=True, help="Confirm send.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_send_draft_cmd(ctx: click.Context, draft_id: str, yes: bool, as_json_flag: bool) -> None:
    """Send an existing draft (from draft / reply / forward). Requires --yes.

    This is the step that actually delivers mail.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(_workspace().mail_send_draft(draft_id=draft_id))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_send_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("update-draft", epilog=help_text.MAIL_UPDATE_DRAFT_EPILOG)
@click.option("--id", "draft_id", required=True, help="Draft message id to patch.")
@click.option(
    "--attach",
    multiple=True,
    type=click.Path(dir_okay=False, path_type=str),
    help="Add a file to the draft, under 2 MB (additive; repeat for several).",
)
@click.option("--subject", default=None, help="New subject (omit to leave unchanged).")
@click.option(
    "--to",
    multiple=True,
    help="Replace the entire To list (repeatable or comma-separated; omit to leave unchanged).",
)
@click.option(
    "--cc",
    multiple=True,
    help="Replace the entire CC list (repeatable or comma-separated; omit to leave unchanged).",
)
@click.option(
    "--bcc",
    multiple=True,
    help="Replace the entire BCC list (repeatable or comma-separated; omit to leave unchanged).",
)
@click.option("--body", default=None, help="New body (mutually exclusive with --body-file).")
@click.option(
    "--body-file",
    "body_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="Read new body from a UTF-8 file.",
)
@click.option(
    "--body-type",
    "body_type",
    default="text",
    show_default=True,
    type=click.Choice(["text", "html"], case_sensitive=False),
    help="Body content type when updating body.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_update_draft_cmd(
    ctx: click.Context,
    draft_id: str,
    attach: tuple[str, ...],
    subject: str | None,
    to: tuple[str, ...],
    cc: tuple[str, ...],
    bcc: tuple[str, ...],
    body: str | None,
    body_file: str | None,
    body_type: str,
    as_json_flag: bool,
) -> None:
    """Patch an existing draft in place. Does not send.

    `--to` / `--cc` / `--bcc` and `--body` each REPLACE that field wholesale
    when given - include every value that should remain. `--attach` is additive.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().mail_update_draft(
                draft_id=draft_id,
                attach=attach,
                subject=subject,
                to=to or None,
                cc=cc or None,
                bcc=bcc or None,
                body=body,
                body_file=body_file,
                body_type=body_type,
            )
        )
    except MailAttachError as exc:
        emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailBodyFileError as exc:
        emit_error(
            error="usage_error",
            message=str(exc),
            as_json=as_json,
        )
        raise SystemExit(EXIT_USAGE) from exc
    except MailDraftNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@main.group(epilog=help_text.MEETING_EPILOG)
def meeting() -> None:
    """Inspect and configure the online meeting on an event you organize.

    Organizer-only. Needs `wo1162425_scopes = true`
    (OnlineMeetings.ReadWrite). Event ids come from `blumkin calendar today`.
    """


@meeting.command("get", epilog=help_text.MEETING_GET_EPILOG)
@click.option("--event-id", required=True, help="Calendar event id (from `calendar today`).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def meeting_get_cmd(ctx: click.Context, event_id: str, as_json_flag: bool) -> None:
    """Resolve an event's online meeting: join URL, id, and settings.

    Exit 5 (not_found) means the event has no online meeting or you are not the
    organizer.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    try:
        payload = asyncio.run(_workspace().meeting_get(event_id=event_id))
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_meeting_get_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@meeting.command("transcription", epilog=help_text.MEETING_TRANSCRIPTION_EPILOG)
@click.option("--event-id", required=True, help="Calendar event id (from `calendar today`).")
@click.option("--enable", is_flag=True, help="Set allowTranscription=true (needs --yes).")
@click.option("--yes", is_flag=True, help="Confirm enable (required with --enable).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def meeting_transcription_cmd(
    ctx: click.Context,
    event_id: str,
    enable: bool,
    yes: bool,
    as_json_flag: bool,
) -> None:
    """Show transcription flags, or enable them with --enable --yes.

    Without --enable this is a read. With --enable it sets
    allowTranscription=true and needs --yes.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    if enable:
        _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(_workspace().meeting_transcription(event_id=event_id, enable=enable))
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_transcription_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@main.group(epilog=help_text.PEOPLE_EPILOG)
def people() -> None:
    """Resolve a name to an email address before you invite or message someone.

    Needs `wo1162425_scopes = true` (People.Read). Fail-closed: never guess
    when more than one person matches.
    """


@people.command("resolve", epilog=help_text.PEOPLE_RESOLVE_EPILOG)
@click.option("--name", "name", default=None, help="Display name to search for.")
@click.option(
    "--email", "email", default=None, help="Exact email for a reverse / exact-match lookup."
)
@click.option(
    "--top",
    default=10,
    show_default=True,
    type=int,
    help="Max Graph people results to consider (max 50).",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def people_resolve_cmd(
    ctx: click.Context,
    name: str | None,
    email: str | None,
    top: int,
    as_json_flag: bool,
) -> None:
    """Resolve a person to an SMTP address, failing closed when ambiguous.

    Zero matches exits 5 (not_found); more than one exits 2 with
    `ambiguous: true` and the candidate list - ask which person, never guess.
    Exactly one match: `person.email` is the address to use.
    """
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    try:
        payload = asyncio.run(_workspace().people_resolve(name=name, email=email, top=top))
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_resolve_human(payload))
    if payload.get("ambiguous"):
        raise SystemExit(EXIT_USAGE)
    raise SystemExit(EXIT_SUCCESS)


if __name__ == "__main__":
    main(obj={})
