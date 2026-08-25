"""Click CLI entrypoint for blumkin."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import click

from blumkin import __version__
from blumkin.auth import create_credential, logout, save_token_cache, status_dict
from blumkin.config import load_config
from blumkin.exit_codes import EXIT_AUTH, EXIT_NOT_FOUND, EXIT_OTHER, EXIT_SUCCESS
from blumkin.output import emit_error, emit_json, emit_lines
from blumkin.skills import describe_skill, skills_catalog
from blumkin.skills.calendar import calendar_today, format_today_human


def _as_json(ctx: click.Context, as_json_flag: bool) -> bool:
    return bool(ctx.obj.get("as_json") or as_json_flag)


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
        raise SystemExit(EXIT_AUTH if "client_id" in problems[0] else EXIT_OTHER)


@main.group()
def calendar() -> None:
    """Calendar skills."""


@calendar.command("today")
@click.option("--date", "day", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_today_cmd(ctx: click.Context, day: Any, as_json_flag: bool) -> None:
    """List events for today (or --date YYYY-MM-DD)."""
    as_json = _as_json(ctx, as_json_flag)
    tz_name = ctx.obj["tz_name"]
    day_value: date | None = day.date() if day is not None else None
    try:
        payload = asyncio.run(calendar_today(day=day_value, tz_name=tz_name))
    except ValueError as exc:
        emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    except Exception as exc:
        emit_error(error="graph_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    if as_json:
        emit_json(payload)
    else:
        emit_lines(format_today_human(payload))
    raise SystemExit(EXIT_SUCCESS)


if __name__ == "__main__":
    main(obj={})
