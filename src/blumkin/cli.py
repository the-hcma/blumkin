"""Click CLI entrypoint for blumkin."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click

from blumkin import __version__
from blumkin.auth import create_credential, logout, save_token_cache, status_dict
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
    calendar_today,
    calendar_view,
    format_freebusy_human,
    format_today_human,
    format_view_human,
    parse_local_datetime,
)
from blumkin.skills.calendar_writes import (
    calendar_accept,
    calendar_cancel,
    calendar_create,
    format_accept_human,
    format_cancel_human,
    format_create_human,
)
from blumkin.skills.chat import chat_find, chat_last, format_find_human, format_last_human
from blumkin.skills.mail import (
    format_draft_human,
    format_inbox_human,
    format_send_draft_human,
    mail_draft,
    mail_inbox,
    mail_send_draft,
)


def _as_json(ctx: click.Context, as_json_flag: bool) -> bool:
    return bool(ctx.obj.get("as_json") or as_json_flag)


def _raise_graph_http_error(exc: BaseException, *, as_json: bool) -> NoReturn:
    status = getattr(exc, "response_status_code", None)
    if status == 401:
        emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    if status == 403:
        emit_error(error="missing_scope", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    emit_error(error="graph_error", message=str(exc), as_json=as_json)
    raise SystemExit(EXIT_OTHER) from exc


def _tz_name(ctx: click.Context, tz_flag: str | None) -> str | None:
    return tz_flag if tz_flag is not None else ctx.obj.get("tz_name")


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
        create_credential(cfg)
        save_token_cache(cfg)
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
        "problems": problems,
        "status": status,
        "skills": [s["id"] for s in skills_catalog()["skills"]],
    }
    if as_json:
        emit_json(payload)
    else:
        emit_lines([f"ok: {payload['ok']}"])
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
        payload = asyncio.run(
            calendar_accept(
                event_id=event_id,
                today_pending=today_pending,
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
@click.option("--teams", is_flag=True, help="Create as Teams online meeting.")
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
    """Create a calendar event. Requires --yes."""
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


@main.group()
def chat() -> None:
    """Teams chat read skills."""


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


@main.group()
def mail() -> None:
    """Mail read skills."""


@mail.command("inbox")
@click.option("--top", default=10, show_default=True, type=int)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_inbox_cmd(ctx: click.Context, top: int, as_json_flag: bool) -> None:
    """List recent inbox messages."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(mail_inbox(top=top))
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
        emit_lines(format_inbox_human(payload))
    raise SystemExit(EXIT_SUCCESS)


@mail.command("draft")
@click.option("--to", required=True, help="Recipient email.")
@click.option("--subject", required=True)
@click.option("--body", required=True)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_draft_cmd(
    ctx: click.Context, to: str, subject: str, body: str, as_json_flag: bool
) -> None:
    """Create a mail draft (does not send)."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(mail_draft(to=to, subject=subject, body=body))
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


if __name__ == "__main__":
    main(obj={})
