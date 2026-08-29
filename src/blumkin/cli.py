"""Click CLI entrypoint for blumkin."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
import httpx

from blumkin import __version__
from blumkin.auth import (
    SecretWriteError,
    create_credential,
    logout,
    refresh_silent,
    save_token_cache,
    status_dict,
)
from blumkin.config import load_config
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_SUCCESS,
    EXIT_USAGE,
)
from blumkin.output import emit_error, emit_json, emit_lines
from blumkin.skills import describe_skill, skills_catalog
from blumkin.skills.calendar import (
    calendar_freebusy,
    calendar_suggest,
    calendar_today,
    calendar_view,
    format_freebusy_human,
    format_suggest_human,
    format_today_human,
    format_view_human,
    parse_local_datetime,
)
from blumkin.skills.calendar_writes import (
    calendar_accept,
    calendar_cancel,
    calendar_create,
    calendar_update,
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
    chat_attachments_download,
    chat_attachments_list,
    chat_delete,
    chat_edit,
    chat_find,
    chat_last,
    chat_send,
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
    mail_attachments_download,
    mail_attachments_list,
    mail_delete_draft,
    mail_draft,
    mail_folders,
    mail_forward,
    mail_get,
    mail_inbox,
    mail_list,
    mail_reply,
    mail_send_draft,
    mail_update_draft,
)
from blumkin.skills.mail import (
    format_get_human as format_mail_get_human,
)
from blumkin.skills.meeting import (
    format_get_human as format_meeting_get_human,
)
from blumkin.skills.meeting import (
    format_transcription_human,
    meeting_get,
    meeting_transcription,
)
from blumkin.skills.people import format_resolve_human, people_resolve


def _as_json(ctx: click.Context, as_json_flag: bool) -> bool:
    return bool(ctx.obj.get("as_json") or as_json_flag)


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
    if isinstance(exc, ValueError):
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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
                "Raise graph_timeout_seconds / BLUMKIN_GRAPH_TIMEOUT if needed; "
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
    tz = ZoneInfo(_tz_name(ctx, tz_flag) or load_config().default_tz)
    return (
        None if since is None else parse_local_datetime(since, tz),
        None if until is None else parse_local_datetime(until, tz),
    )


def _raise_mail_value_error(exc: ValueError, *, as_json: bool) -> NoReturn:
    msg = str(exc)
    if "client_id" in msg or "Missing" in msg:
        emit_error(error="auth_required", message=msg, as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    emit_error(error="usage_error", message=msg, as_json=as_json)
    raise SystemExit(EXIT_USAGE) from exc


def _tz_name(ctx: click.Context, tz_flag: str | None) -> str | None:
    return tz_flag if tz_flag is not None else ctx.obj.get("tz_name")


def _require_wo1162425_scopes(*, as_json: bool) -> None:
    cfg = load_config()
    if cfg.wo1162425_scopes:
        return
    emit_error(
        error="usage_error",
        message=(
            "WO1162425 add-on scopes are disabled. Calendar/mail/chat read skills work "
            "without them; chat write, meeting skills, and "
            "people resolve need wo1162425_scopes = true "
            "in config.toml (or BLUMKIN_WO1162425_SCOPES=1) after Remedy WO1162425 "
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


@click.group()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_name", default=None, help="IANA timezone (default from config).")
@click.version_option(version=__version__, prog_name="blumkin")
@click.pass_context
def main(ctx: click.Context, as_json: bool, tz_name: str | None) -> None:
    """Personal Microsoft 365 / Graph skills CLI (delegated as me)."""
    ctx.ensure_object(dict)
    ctx.obj["as_json"] = as_json
    ctx.obj["tz_name"] = tz_name


@main.group()
def auth() -> None:
    """Sign in, status, and logout."""


@auth.command("login")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_login(ctx: click.Context, as_json_flag: bool) -> None:
    """Interactive browser login; write cache + auth record under ~/.config/blumkin."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        cfg = load_config()
        create_credential(cfg, allow_interactive=True)
        save_token_cache(cfg)
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
    except Exception as exc:
        emit_error(
            error="auth_required",
            message=str(exc),
            as_json=as_json,
            hint="Set client_id in ~/.config/blumkin/config.toml then retry.",
        )
        raise SystemExit(EXIT_AUTH) from exc
    if as_json:
        emit_json({"ok": True, "status": status_dict()})
    else:
        emit_lines(["Signed in. Token cache written under ~/.config/blumkin/."])


@auth.command("logout")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_logout(ctx: click.Context, as_json_flag: bool) -> None:
    """Delete local token cache and auth record."""
    logout()
    if _as_json(ctx, as_json_flag):
        emit_json({"ok": True})
    else:
        emit_lines(["Logged out (cache files removed)."])


@auth.command("refresh")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_refresh(ctx: click.Context, as_json_flag: bool) -> None:
    """Silent token refresh; never opens a browser."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = refresh_silent()
    except SecretWriteError as exc:
        emit_error(error="secret_write_failed", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
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


@auth.command("status")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_status(ctx: click.Context, as_json_flag: bool) -> None:
    """Show config path and whether cache / auth record exist."""
    payload = status_dict()
    if _as_json(ctx, as_json_flag):
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


@main.group()
def skills() -> None:
    """Agent skill discovery."""


@skills.command("list")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def skills_list(ctx: click.Context, as_json_flag: bool) -> None:
    """List skills (prefer --json for agents)."""
    catalog = skills_catalog()
    if _as_json(ctx, as_json_flag):
        emit_json(catalog)
        return
    for skill in catalog["skills"]:
        emit_lines([f"{skill['id']}: {skill['summary']}"])


@skills.command("describe")
@click.argument("skill_id")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def skills_describe(ctx: click.Context, skill_id: str, as_json_flag: bool) -> None:
    """Describe one skill by id."""
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


@main.command()
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def doctor(ctx: click.Context, as_json_flag: bool) -> None:
    """Check config, cache, and skill scope notes."""
    as_json = _as_json(ctx, as_json_flag)
    cfg = load_config()
    status = status_dict(cfg)
    problems: list[str] = []
    if not status["client_id_configured"]:
        problems.append("client_id missing in config.toml / BLUMKIN_CLIENT_ID")
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


@main.group()
def calendar() -> None:
    """Calendar skills."""


@calendar.command("today")
@click.option("--date", "day", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def calendar_today_cmd(
    ctx: click.Context, day: Any, as_json_flag: bool, tz_flag: str | None
) -> None:
    """List events for today (or --date YYYY-MM-DD)."""
    as_json = _as_json(ctx, as_json_flag)
    tz_name = _tz_name(ctx, tz_flag)
    day_value: date | None = day.date() if day is not None else None
    try:
        payload = asyncio.run(calendar_today(day=day_value, tz_name=tz_name))
    except ValueError as exc:
        emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
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


@calendar.command("view")
@click.option("--from", "from_day", required=True, type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--to", "to_day", required=True, type=click.DateTime(formats=["%Y-%m-%d"]))
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
    """List events in half-open local range [--from, --to)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        cfg = load_config()
        tz = ZoneInfo(_tz_name(ctx, tz_flag) or cfg.default_tz)
        start = datetime(from_day.year, from_day.month, from_day.day, tzinfo=tz)
        end = datetime(to_day.year, to_day.month, to_day.day, tzinfo=tz)
        payload = asyncio.run(calendar_view(start=start, end=end))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@calendar.command("freebusy")
@click.option("--with", "with_emails", multiple=True, required=True, help="Email to query.")
@click.option("--start", "start_raw", required=True, help="Local start datetime.")
@click.option("--end", "end_raw", required=True, help="Local end datetime.")
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
    """Get free/busy for one or more people."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        cfg = load_config()
        tz = ZoneInfo(_tz_name(ctx, tz_flag) or cfg.default_tz)
        start = parse_local_datetime(start_raw, tz)
        end = parse_local_datetime(end_raw, tz)
        payload = asyncio.run(
            calendar_freebusy(with_emails=list(with_emails), start=start, end=end)
        )
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@calendar.command("suggest")
@click.option(
    "--with", "with_emails", multiple=True, required=True, help="People who must be free."
)
@click.option("--start", "start_raw", required=True, help="Local search start datetime.")
@click.option("--end", "end_raw", required=True, help="Local search end datetime.")
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
    """Suggest mutual free slots from free/busy (does not create an event)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        cfg = load_config()
        tz = ZoneInfo(_tz_name(ctx, tz_flag) or cfg.default_tz)
        start = parse_local_datetime(start_raw, tz)
        end = parse_local_datetime(end_raw, tz)
        payload = asyncio.run(
            calendar_suggest(
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
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        if msg.startswith("freebusy lookup failed"):
            emit_error(error="graph_error", message=msg, as_json=as_json)
            raise SystemExit(EXIT_OTHER) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@calendar.command("accept")
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
    """Accept calendar invitation(s). Requires --yes."""
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        tz_name = _tz_name(ctx, tz_flag)
        if today_pending:
            cfg = load_config()
            ZoneInfo(tz_name or cfg.default_tz)
        payload = asyncio.run(
            calendar_accept(
                event_id=event_id,
                today_pending=today_pending,
                tz_name=tz_name,
            )
        )
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@calendar.command("cancel")
@click.option("--event-id", "event_id", required=True)
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_cancel_cmd(ctx: click.Context, event_id: str, yes: bool, as_json_flag: bool) -> None:
    """Cancel a calendar event. Requires --yes."""
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(calendar_cancel(event_id=event_id))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_cancel_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@calendar.command("create")
@click.option("--subject", required=True)
@click.option("--with", "with_emails", multiple=True, required=True, help="Attendee email.")
@click.option("--start", "start_raw", required=True, help="Local start datetime.")
@click.option("--duration", default="30m", show_default=True, help="Length (e.g. 30m, 1h).")
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
    """Create a calendar event (Teams by default). Requires --yes."""
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(
            calendar_create(
                subject=subject,
                with_emails=list(with_emails),
                start_raw=start_raw,
                duration=duration,
                teams=teams,
                tz_name=_tz_name(ctx, tz_flag),
            )
        )
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@calendar.command("update")
@click.option("--event-id", required=True, help="Event id to update.")
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
    """Attach Teams to an existing calendar event. Requires --yes."""
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(
            calendar_update(
                event_id=event_id,
                teams=teams,
                tz_name=_tz_name(ctx, tz_flag),
            )
        )
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@main.group()
def chat() -> None:
    """Teams chat skills."""


@chat.group("attachments", invoke_without_command=True)
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
    """List attachments on a chat message (default when no subcommand)."""
    if ctx.invoked_subcommand is not None:
        return
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            chat_attachments_list(
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


@chat_attachments_cmd.command("download")
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
    """Download one or all file attachments from a chat message."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            chat_attachments_download(
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


@chat.command("delete")
@click.option("--chat-id", required=True, help="Teams chat id.")
@click.option("--message-id", required=True, help="Chat message id.")
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
    """Soft-delete a chat message."""
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(chat_delete(chat_id=chat_id, message_id=message_id))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_chat_delete_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat.command("edit")
@click.option("--chat-id", required=True, help="Teams chat id.")
@click.option("--message-id", required=True, help="Chat message id.")
@click.option("--text", required=True, help="Replacement message text.")
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
    """Edit a chat message body in place."""
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(chat_edit(chat_id=chat_id, message_id=message_id, text=text))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_edit_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat.command("find")
@click.option("--with", "with_name", required=True, help="Display-name substring.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_find_cmd(ctx: click.Context, with_name: str, as_json_flag: bool) -> None:
    """Find chats whose members match a display name."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(chat_find(with_name=with_name))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_find_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@chat.command("last")
@click.option("--with", "with_name", required=True, help="Display-name substring.")
@click.option("--n", "n", default=3, show_default=True, type=int)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_last_cmd(ctx: click.Context, with_name: str, n: int, as_json_flag: bool) -> None:
    """Show last N messages from a matched chat."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(chat_last(with_name=with_name, n=n))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_last_human(payload))
    if payload.get("chat") is None:
        raise SystemExit(EXIT_NOT_FOUND)
    raise SystemExit(EXIT_SUCCESS)


@chat.command("send")
@click.option(
    "--with",
    "with_name",
    default=None,
    help="Display-name match (exclusive with --chat-id).",
)
@click.option(
    "--chat-id",
    "chat_id",
    default=None,
    help="Explicit chat id (exclusive with --with).",
)
@click.option("--text", required=True, help="Message text to send.")
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
    """Send a text message to a matched or explicit chat."""
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(chat_send(with_name=with_name, chat_id=chat_id, text=text))
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_send_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@main.group()
def mail() -> None:
    """Mail read skills."""


@mail.command("inbox")
@click.option("--from", "sender", default=None, help="Sender name or address substring.")
@click.option("--subject", default=None, help="Subject substring.")
@click.option("--search", default=None, help="Graph $search term; cannot be combined with filters.")
@click.option("--since", default=None, help="Only messages at or after this date/time.")
@click.option("--until", default=None, help="Only messages strictly before this date/time.")
@click.option("--unread", is_flag=True, help="Only unread messages.")
@click.option("--top", default=10, show_default=True, type=int)
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
    """List recent inbox messages."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        since_dt, until_dt = _mail_time_bounds(ctx, tz_flag, since=since, until=until)
        payload = asyncio.run(
            mail_inbox(
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


@mail.command("folders")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_folders_cmd(ctx: click.Context, as_json_flag: bool) -> None:
    """List mail folders with their ids and message counts."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(mail_folders())
    except ValueError as exc:
        _raise_mail_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_folders_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("get")
@click.option("--id", "message_id", required=True, help="Message id.")
@click.option(
    "--body-type",
    default="text",
    show_default=True,
    type=click.Choice(["html", "text"]),
    help="Body format to request from Graph.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_get_cmd(
    ctx: click.Context,
    message_id: str,
    body_type: str,
    as_json_flag: bool,
) -> None:
    """Read one message, including its body and attachments."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(mail_get(message_id=message_id, body_type=body_type))
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


@mail.command("list")
@click.option(
    "--folder",
    default=None,
    help=f"Well-known name ({', '.join(WELL_KNOWN_MAIL_FOLDERS)}) or a folder id.",
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
@click.option("--from", "sender", default=None, help="Sender name or address substring.")
@click.option("--subject", default=None, help="Subject substring.")
@click.option("--search", default=None, help="Graph $search term; cannot be combined with filters.")
@click.option("--since", default=None, help="Only messages at or after this date/time.")
@click.option("--until", default=None, help="Only messages strictly before this date/time.")
@click.option("--unread", is_flag=True, help="Only unread messages.")
@click.option("--top", default=10, show_default=True, type=int)
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
    """List recent messages from a mail folder."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        since_dt, until_dt = _mail_time_bounds(ctx, tz_flag, since=since, until=until)
        payload = asyncio.run(
            mail_list(
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


@mail.group("attachments", invoke_without_command=True)
@click.option("--id", "message_id", default=None, help="Message id.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_attachments_cmd(ctx: click.Context, message_id: str | None, as_json_flag: bool) -> None:
    """List attachments on a message (default when no subcommand)."""
    if ctx.invoked_subcommand is not None:
        return
    as_json = _as_json(ctx, as_json_flag)
    if not message_id or not message_id.strip():
        emit_error(error="usage_error", message="--id is required", as_json=as_json)
        raise SystemExit(EXIT_USAGE)
    try:
        payload = asyncio.run(mail_attachments_list(message_id=message_id))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@mail_attachments_cmd.command("download")
@click.option("--message-id", required=True, help="Message id.")
@click.option("--attachment-id", default=None, help="Attachment id (omit with --all).")
@click.option("--all", "download_all", is_flag=True, help="Download every file attachment.")
@click.option("--out", required=True, type=click.Path(), help="Output file or directory.")
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
    """Download one or all file attachments from a message."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            mail_attachments_download(
                message_id=message_id,
                attachment_id=attachment_id,
                download_all=download_all,
                out=out,
            )
        )
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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


@mail.command("delete-draft")
@click.option("--id", "draft_id", required=True, help="Draft message id.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_delete_draft_cmd(ctx: click.Context, draft_id: str, as_json_flag: bool) -> None:
    """Delete a draft message (does not notify recipients)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(mail_delete_draft(draft_id=draft_id))
    except MailDraftNotFoundError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_delete_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("draft")
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
@click.option("--subject", required=True)
@click.option(
    "--attach",
    multiple=True,
    type=click.Path(dir_okay=False, path_type=str),
    help="Attach a file (repeat for several).",
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
    """Create a mail draft (does not send)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            mail_draft(
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
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("forward")
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
    """Create a forward draft (does not send)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            mail_forward(
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


@mail.command("reply")
@click.option("--id", "message_id", required=True, help="Message id to reply to.")
@click.option("--all", "reply_all", is_flag=True, help="Reply to every recipient.")
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
    """Create a reply draft that threads correctly (does not send)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            mail_reply(
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


@mail.command("send-draft")
@click.option("--id", "draft_id", required=True, help="Draft message id.")
@click.option("--yes", "yes", is_flag=True, help="Confirm send.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_send_draft_cmd(ctx: click.Context, draft_id: str, yes: bool, as_json_flag: bool) -> None:
    """Send an existing draft. Requires --yes."""
    as_json = _as_json(ctx, as_json_flag)
    _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(mail_send_draft(draft_id=draft_id))
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_send_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("update-draft")
@click.option("--id", "draft_id", required=True, help="Draft message id.")
@click.option(
    "--attach",
    multiple=True,
    type=click.Path(dir_okay=False, path_type=str),
    help="Attach a file to the draft (repeat for several).",
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
    """Patch an existing draft in place (does not send)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            mail_update_draft(
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
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_draft_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@main.group()
def meeting() -> None:
    """Online meeting skills."""


@meeting.command("get")
@click.option("--event-id", required=True, help="Calendar event id.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def meeting_get_cmd(ctx: click.Context, event_id: str, as_json_flag: bool) -> None:
    """Show online-meeting details for a calendar event."""
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    try:
        payload = asyncio.run(meeting_get(event_id=event_id))
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_meeting_get_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@meeting.command("transcription")
@click.option("--event-id", required=True, help="Calendar event id.")
@click.option("--enable", is_flag=True, help="Set allowTranscription=true.")
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
    """Show or enable transcription on an event's online meeting."""
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    if enable:
        _require_yes(yes=yes, as_json=as_json)
    try:
        payload = asyncio.run(meeting_transcription(event_id=event_id, enable=enable))
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_transcription_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@main.group()
def people() -> None:
    """People directory skills."""


@people.command("resolve")
@click.option("--name", "name", default=None, help="Display name to search for.")
@click.option("--email", "email", default=None, help="Exact email / reverse lookup.")
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
    """Resolve a person to an SMTP address (fail-closed when ambiguous)."""
    as_json = _as_json(ctx, as_json_flag)
    _require_wo1162425_scopes(as_json=as_json)
    try:
        payload = asyncio.run(people_resolve(name=name, email=email, top=top))
    except LookupError as exc:
        emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        msg = str(exc)
        if "client_id" in msg or "Missing" in msg:
            emit_error(error="auth_required", message=msg, as_json=as_json)
            raise SystemExit(EXIT_AUTH) from exc
        emit_error(error="usage_error", message=msg, as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
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
