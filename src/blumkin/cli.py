"""Click CLI entrypoint for blumkin."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
import httpx

from blumkin import help_text
from blumkin.auth import AuthRequiredError, AuthTransientError, MissingScopeError, SecretWriteError
from blumkin.config import BlumkinConfig, list_profiles, load_config, set_profile_email
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_SUCCESS,
    EXIT_USAGE,
)
from blumkin.output import emit_error, emit_json, emit_lines
from blumkin.pipx_install import PIPX_UPGRADE_TIMEOUT_S, pipx_blumkin_path
from blumkin.providers import get_provider
from blumkin.providers.kind import ProviderConfigError, ProviderKind
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
    MAIL_IMPORTANCE_VALUES,
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
    render_mail_signature,
)
from blumkin.skills.mail import (
    format_get_human as format_mail_get_human,
)
from blumkin.skills.meeting import (
    format_get_human as format_meeting_get_human,
)
from blumkin.skills.meeting import format_transcription_human
from blumkin.skills.people import format_resolve_human
from blumkin.version import (
    build_info,
    build_status_fields,
    build_version,
    is_source_checkout,
    running_command_path,
)

# Fallback next-step guidance per error slug, used when a call site does not pass
# its own more specific `hint=`. Keep every non-zero exit actionable (issue #97).
_DEFAULT_HINTS: dict[str, str] = {
    "auth_required": (
        "Run `blumkin auth login` on this machine (or `blumkin auth refresh` for an "
        "expired access token), then retry. `blumkin auth status` shows the current state."
    ),
    "graph_error": (
        "Retry once. If it persists, check `blumkin auth status` and Microsoft 365 "
        "service health, and re-run with --json for the raw Graph error."
    ),
    "missing_scope": (
        "The signed-in account is missing a Graph scope for this command. Run "
        "`blumkin doctor`; if the flow needs an add-on scope, set wo1162425_scopes "
        "(or files_scopes) = true in config.toml, delete the token cache and auth "
        "record, then `blumkin auth login`."
    ),
    "not_found": (
        "Re-check the id or name. List first to get a valid one: `blumkin mail list "
        "--json`, `blumkin calendar today --json`, or `blumkin chat find --with NAME --json`."
    ),
    "secret_write_failed": (
        "The token cache or auth record could not be written. Remove any symlink at "
        "~/.config/blumkin/ (or the cache files), fix the directory permissions, then retry."
    ),
    "timeout": (
        "Raise graph_timeout_seconds in config.toml, kill any stuck blumkin processes "
        "(`pkill -f blumkin`), then run `blumkin auth refresh` if the access token expired."
    ),
    "transient_error": (
        "The auth provider hit a transient network or server error - this is not a bad "
        "grant. Wait a moment and retry the same command."
    ),
    "upgrade_failed": (
        "Run `pipx upgrade blumkin` directly for the full output. If blumkin was not "
        "installed with pipx, use `pipx install blumkin` (see docs/RELEASING.md)."
    ),
    "usage_error": "See `blumkin COMMAND --help` for the accepted arguments and examples.",
}

# Overrides _DEFAULT_HINTS["missing_scope"] (Microsoft/Graph-only wording) for
# MissingScopeError specifically: it is provider-neutral, unlike the tenant-grant /
# wo1162425_scopes / files_scopes hint that only makes sense for a Microsoft 403
# (issue #133 - a Google profile has neither of those knobs or an MSAL auth record).
_MISSING_SCOPE_HINT = (
    "Run `blumkin auth login` on a TTY and tick every scope box (or click "
    '"Select all") on the consent screen - the message above lists exactly '
    "which scopes are missing."
)


def _as_json(ctx: click.Context, as_json_flag: bool) -> bool:
    value = bool(ctx.obj.get("as_json") or as_json_flag)
    ctx.obj["as_json"] = value
    return value


def _auth_status_payload(config: BlumkinConfig | None = None) -> dict[str, Any]:
    """Auth-status fields plus the resolved build (version, commit, binary path)."""
    payload = dict(_workspace(config).auth_status())
    payload.update(build_status_fields())
    return payload


def _cli_as_json() -> bool:
    ctx = click.get_current_context(silent=True)
    if ctx is None or not isinstance(ctx.obj, dict):
        return False
    return bool(ctx.obj.get("as_json"))


def _emit_error(
    *,
    error: str,
    message: str,
    as_json: bool,
    hint: str | None = None,
) -> None:
    """`emit_error` that falls back to `_DEFAULT_HINTS[error]` when no hint is given."""
    emit_error(
        error=error,
        message=message,
        as_json=as_json,
        hint=hint or _DEFAULT_HINTS.get(error),
    )


def _graph_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status from kiota/msgraph or googleapiclient exceptions.

    ``status_code`` covers both kiota ``APIError`` and
    ``googleapiclient.errors.HttpError`` (an int property since v2.40); the
    ``response`` fallbacks catch older/other shapes.
    """
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
        _emit_error(error="usage_error", message=str(exc), as_json=_cli_as_json())
        raise SystemExit(EXIT_USAGE) from exc


def _populate_profile_email_once() -> str | None:
    """Record the signed-in address in config.toml, once, at onboarding.

    Only writes when the profile has no ``email`` key yet: after that the value is
    a stable operator-set label, not a live mirror (``blumkin doctor`` reports
    drift instead of silently rewriting it). Entirely best-effort - resolving or
    writing must never fail an otherwise successful login.
    """
    try:
        cfg = _load_config()
        if cfg.email:
            return None
        address = _workspace(cfg).account_email()
        if not address:
            return None
        written = set_profile_email(
            cfg.config_path,
            profile=cfg.profile,
            email=address,
            legacy_flat=cfg.legacy_flat,
        )
    except Exception:
        return None
    return address if written else None


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


def _print_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """``--version`` callback: package version, short commit, resolved binary path."""
    if not value or ctx.resilient_parsing:
        return
    click.echo(build_info())
    click.echo(f"running from {running_command_path()}")
    ctx.exit()


def _provider_config_hint(message: str) -> str | None:
    """The client_id hint only applies when the failure is actually about client_id.

    Google's ``ProviderConfigError`` is about ``google_oauth_client_file`` (a
    Desktop-client JSON path), not ``client_id`` - showing the Microsoft-only
    remediation there reproduces the exact misleading-hint failure issue #133
    reports, on the Google config path this time (issue #133 review, round 2).
    """
    if "client_id" in message and "google_oauth_client_file" not in message:
        return "Set client_id in ~/.config/blumkin/config.toml then retry."
    return None


def _raise_auth_value_error(exc: ValueError, *, as_json: bool) -> NoReturn:
    """Classify a ``ValueError`` from the auth layer by type, not by sniffing its message.

    ``AuthRequiredError`` / ``AuthTransientError`` / ``MissingScopeError`` /
    ``ProviderConfigError`` are the typed subclasses raised by
    ``blumkin.auth`` / ``blumkin.providers.google_auth`` (issue #133); the
    message-substring fallback below only still applies to a plain
    ``ValueError`` from elsewhere in the codebase.
    """
    if isinstance(exc, ProviderConfigError):
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    if isinstance(exc, MissingScopeError):
        _emit_error(
            error="missing_scope", message=str(exc), as_json=as_json, hint=_MISSING_SCOPE_HINT
        )
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    if isinstance(exc, AuthTransientError):
        _emit_error(error="transient_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    if isinstance(exc, AuthRequiredError):
        _emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    msg = str(exc)
    if (
        "client_id" in msg
        or "Missing" in msg
        or msg.startswith("Authentication required")
        or msg.startswith("Silent token refresh failed")
    ):
        _emit_error(error="auth_required", message=msg, as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    _emit_error(error="usage_error", message=msg, as_json=as_json)
    raise SystemExit(EXIT_USAGE) from exc


def _raise_chat_attachment_error(exc: BaseException, *, as_json: bool) -> NoReturn:
    if isinstance(exc, ChatAttachmentScopeError):
        _emit_error(error="missing_scope", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    if isinstance(exc, ChatAttachmentNotFoundError | ChatMessageNotFoundError | LookupError):
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    if isinstance(exc, ChatAttachmentSkippedError):
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    if isinstance(exc, ProviderConfigError):
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    if isinstance(exc, ValueError):
        _raise_auth_value_error(exc, as_json=as_json)
    _raise_graph_http_error(exc, as_json=as_json)


def _raise_graph_http_error(exc: BaseException, *, as_json: bool) -> NoReturn:
    if isinstance(exc, SecretWriteError):
        _emit_error(error="secret_write_failed", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        _emit_error(
            error="timeout",
            message=str(exc) or "Graph or token HTTP call timed out",
            as_json=as_json,
        )
        raise SystemExit(EXIT_OTHER) from exc
    status = _graph_http_status(exc)
    if status == 401:
        _emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    if status == 403:
        _emit_error(error="missing_scope", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    if status == 404:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    _emit_error(error="graph_error", message=str(exc), as_json=as_json)
    raise SystemExit(EXIT_OTHER) from exc


def _raise_mail_value_error(exc: ValueError, *, as_json: bool) -> NoReturn:
    _raise_auth_value_error(exc, as_json=as_json)


def _read_pipx_version(executable: Path) -> str | None:
    """Return ``<executable> --version`` as ``<version> (<commit>)``, or None.

    The first line of ``blumkin --version`` is ``blumkin <version> (<commit>)``;
    the ``blumkin `` prefix is stripped so the value compares directly against
    :func:`blumkin.version.build_version` (the from/to pair in ``upgrade``).
    """
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    lines = (completed.stdout or "").splitlines()
    first = lines[0].strip() if lines else ""
    if not first:
        return None
    return first.removeprefix("blumkin ").strip() or first


def _require_wo1162425_scopes(*, as_json: bool) -> None:
    """Gate the skills that need the Microsoft add-on scopes from Remedy WO1162425.

    Microsoft-only by construction: WO1162425 is an Entra tenant request for
    Chat.ReadWrite / OnlineMeetings.ReadWrite / People.Read. Google grants the
    equivalent access through its own consent screen, so applying this gate there
    would make those verbs unreachable no matter what the operator consented to.
    """
    cfg = _load_config()
    if cfg.provider is not ProviderKind.MICROSOFT:
        return
    if cfg.wo1162425_scopes:
        return
    _emit_error(
        error="usage_error",
        message=(
            "WO1162425 add-on scopes are disabled. Calendar, mail, and chat read "
            "skills work without them; chat write, meeting skills, and people resolve "
            "do not."
        ),
        as_json=as_json,
        hint=(
            "Set wo1162425_scopes = true in config.toml once Remedy WO1162425 has "
            "granted its add-ons (at least Chat.ReadWrite, OnlineMeetings.ReadWrite, "
            "People.Read; see HANDOFF.md, some asks may still be pending), then delete "
            "the token cache and auth record and run `blumkin auth login`."
        ),
    )
    raise SystemExit(EXIT_USAGE)


def _require_yes(
    *,
    yes: bool,
    as_json: bool,
    reason: str = "This action notifies other people.",
) -> None:
    if not yes:
        _emit_error(
            error="usage_error",
            message="--yes is required for this command",
            as_json=as_json,
            hint=f"{reason} Re-run the command with --yes to confirm.",
        )
        raise SystemExit(EXIT_USAGE)


def _tz_name(ctx: click.Context, tz_flag: str | None) -> str | None:
    return tz_flag if tz_flag is not None else ctx.obj.get("tz_name")


def _workspace(config: BlumkinConfig | None = None) -> WorkspaceProvider:
    try:
        return get_provider(config if config is not None else _load_config())
    except ProviderConfigError as exc:
        _emit_error(error="usage_error", message=str(exc), as_json=_cli_as_json())
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
@click.option(
    "--version",
    is_flag=True,
    callback=_print_version,
    expose_value=False,
    is_eager=True,
    help="Show the version, commit, and binary path, then exit.",
)
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
        _emit_error(
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
        # The client_id hint only fires when the failure is actually about
        # client_id - not a revoked grant, a transient network error, or a
        # Google google_oauth_client_file problem (issue #133).
        _emit_error(
            error="usage_error",
            message=str(exc),
            as_json=as_json,
            hint=_provider_config_hint(str(exc)),
        )
        raise SystemExit(EXIT_USAGE) from exc
    except MissingScopeError as exc:
        _emit_error(
            error="missing_scope", message=str(exc), as_json=as_json, hint=_MISSING_SCOPE_HINT
        )
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    except AuthTransientError as exc:
        _emit_error(error="transient_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    except AuthRequiredError as exc:
        _emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    except Exception as exc:
        # Truly unclassified failure (should be rare now that the auth layer
        # types its errors) - no client_id hint here, that would usually be wrong.
        _emit_error(error="auth_required", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_AUTH) from exc
    populated = _populate_profile_email_once()
    if as_json:
        emit_json({"ok": True, "email_written": populated, "status": _auth_status_payload()})
    else:
        emit_lines(["Signed in. Token cache written under ~/.config/blumkin/."])
        if populated:
            emit_lines([f"Recorded account email in config.toml: {populated}"])


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
        _emit_error(error="secret_write_failed", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    except ProviderConfigError as exc:
        _emit_error(
            error="usage_error",
            message=str(exc),
            as_json=as_json,
            hint=_provider_config_hint(str(exc)),
        )
        raise SystemExit(EXIT_USAGE) from exc
    except MissingScopeError as exc:
        _emit_error(
            error="missing_scope", message=str(exc), as_json=as_json, hint=_MISSING_SCOPE_HINT
        )
        raise SystemExit(EXIT_MISSING_SCOPE) from exc
    except AuthTransientError as exc:
        _emit_error(error="transient_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    except Exception as exc:
        # Covers AuthRequiredError and any unclassified failure alike: the fix is
        # always `auth login` on a TTY (issue #133).
        _emit_error(
            error="auth_required",
            message=str(exc),
            as_json=as_json,
            hint="Run `blumkin auth login` on a TTY, then retry.",
        )
        raise SystemExit(EXIT_AUTH) from exc
    populated = _populate_profile_email_once()
    if as_json:
        emit_json({"ok": True, "email_written": populated, "status": payload})
    else:
        expires = payload.get("access_token_expires_at") or "(none)"
        emit_lines([f"Silent refresh ok. access_token_expires_at: {expires}"])
        if populated:
            emit_lines([f"Recorded account email in config.toml: {populated}"])


@auth.command("status", epilog=help_text.AUTH_STATUS_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_status(ctx: click.Context, as_json_flag: bool) -> None:
    """Show the config path, client-id state, and token-cache expiry.

    Read this before assuming a hang is a login problem.
    """
    as_json = _as_json(ctx, as_json_flag)
    payload = _auth_status_payload()
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
        f"build: {payload['build_version']} ({payload['build_commit']})",
        f"running_from: {payload['running_from']}",
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


@profiles.command("set-email", epilog=help_text.PROFILES_SET_EMAIL_EPILOG)
@click.option(
    "--email",
    "email",
    default=None,
    help="Address to record. Omit to resolve it from the signed-in account.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def profiles_set_email(ctx: click.Context, email: str | None, as_json_flag: bool) -> None:
    """Record (or correct) the account email on the active profile.

    Unlike the automatic fill on `auth login` / `auth refresh`, this overwrites an
    existing value - it is the explicit way to fix the drift `blumkin doctor`
    reports, and to backfill a profile that was authenticated before the field
    existed. Use `--profile` to pick a profile other than the default.
    """
    as_json = _as_json(ctx, as_json_flag)
    cfg = _load_config()
    address = (email or "").strip()
    if not address:
        address = _workspace(cfg).account_email()
    if not address:
        _emit_error(
            error="not_found",
            message="could not resolve the signed-in account email",
            as_json=as_json,
            hint="Pass --email explicitly, or run `blumkin auth login` for this profile first.",
        )
        raise SystemExit(EXIT_NOT_FOUND)
    try:
        written = set_profile_email(
            cfg.config_path,
            profile=cfg.profile,
            email=address,
            legacy_flat=cfg.legacy_flat,
            overwrite=True,
        )
    except ValueError as exc:
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    if not written:
        _emit_error(
            error="usage_error",
            message=f"could not write email into {cfg.config_path}",
            as_json=as_json,
            hint=f'Add `email = "{address}"` under [profiles.{cfg.profile}] by hand.',
        )
        raise SystemExit(EXIT_USAGE)
    if as_json:
        emit_json({"ok": True, "email": address, "profile": cfg.profile})
    else:
        emit_lines([f"Recorded {address} for profile {cfg.profile!r}."])
    raise SystemExit(EXIT_SUCCESS)


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
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
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
                f"email={item['email'] or '(unset)'} "
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
        _emit_error(
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


@main.command(epilog=help_text.COMPLETION_EPILOG)
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Print a tab-completion script for bash, zsh, or fish.

    Source the output to enable `<TAB>` completion of blumkin commands, options,
    and Choice values. See the epilog for one-liners per shell.
    """
    from click.shell_completion import get_completion_class

    comp_cls = get_completion_class(shell)
    if comp_cls is None:  # pragma: no cover - Choice already constrains shell
        _emit_error(
            error="usage_error",
            message=f"no completion support for shell: {shell}",
            as_json=False,
            hint="Supported shells: bash, zsh, fish.",
        )
        raise SystemExit(EXIT_USAGE)
    completer = comp_cls(main, {}, "blumkin", "_BLUMKIN_COMPLETE")
    click.echo(completer.source())


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
    missing_scopes = status.get("missing_scopes") or []
    if missing_scopes:
        problems.append(
            "missing scopes: " + ", ".join(missing_scopes) + " — run: blumkin auth login"
        )
    # Non-fatal: config.toml's email is a label written once at onboarding, so a
    # mismatch means the profile was re-authenticated as somebody else. Report it;
    # rewriting the operator's config on their behalf is not doctor's call.
    warnings: list[str] = []
    if cfg.email:
        live = ""
        try:
            live = _workspace(cfg).account_email()
        except Exception:
            live = ""
        if live and live.casefold() != cfg.email.casefold():
            warnings.append(
                f"config.toml email is {cfg.email!r} but this profile is signed in as "
                f"{live!r}; update config.toml if the account really changed"
            )
    build = build_status_fields()
    payload = {
        "ok": not problems,
        "build": build,
        "wo1162425_scopes": cfg.wo1162425_scopes,
        "problems": problems,
        "warnings": warnings,
        "status": status,
        "skills": [s["id"] for s in skills_catalog()["skills"]],
    }
    if as_json:
        emit_json(payload)
    else:
        emit_lines([f"ok: {payload['ok']}"])
        emit_lines([f"build: {build['build_version']} ({build['build_commit']})"])
        emit_lines([f"running_from: {build['running_from']}"])
        emit_lines([f"wo1162425_scopes: {cfg.wo1162425_scopes}"])
        emit_lines([f"requested_scopes: {', '.join(status.get('requested_scopes') or [])}"])
        for problem in problems:
            emit_lines([f"problem: {problem}"])
        for warning in warnings:
            emit_lines([f"warning: {warning}"])
        emit_lines([f"skills: {', '.join(payload['skills'])}"])
    if problems:
        raise SystemExit(EXIT_AUTH)


@main.command(epilog=help_text.UPGRADE_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def upgrade(ctx: click.Context, as_json_flag: bool) -> None:
    """Upgrade the pipx install of blumkin, reporting the pipx app's build before and after.

    Wraps `pipx upgrade blumkin`. `from:` / `to:` are always the pipx app's own
    version and commit - bare `pipx upgrade` cannot tell you that, and cannot
    tell you PATH still resolves to a dev checkout, so an upgrade can look like a
    no-op. When you run this from a checkout, the checkout is reported separately.
    """
    as_json = _as_json(ctx, as_json_flag)
    running_build = build_version()
    running_path = running_command_path()
    source_checkout = is_source_checkout()

    pipx = shutil.which("pipx")
    if pipx is None:
        _emit_error(
            error="upgrade_failed",
            message="pipx is not on PATH",
            as_json=as_json,
            hint="Install pipx and `pipx install blumkin` (see docs/RELEASING.md), then retry.",
        )
        raise SystemExit(EXIT_OTHER)

    pipx_app = pipx_blumkin_path(pipx_bin=pipx)
    before = _read_pipx_version(pipx_app) if pipx_app is not None else None

    try:
        completed = subprocess.run(
            [pipx, "upgrade", "blumkin"],
            capture_output=True,
            check=False,
            text=True,
            timeout=PIPX_UPGRADE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        _emit_error(
            error="timeout",
            message="`pipx upgrade blumkin` timed out",
            as_json=as_json,
            hint="Run `pipx upgrade blumkin` directly to see where it hangs.",
        )
        raise SystemExit(EXIT_OTHER) from exc
    except OSError as exc:
        _emit_error(error="upgrade_failed", message=f"could not run pipx: {exc}", as_json=as_json)
        raise SystemExit(EXIT_OTHER) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        _emit_error(
            error="upgrade_failed",
            message="`pipx upgrade blumkin` failed",
            as_json=as_json,
            hint=detail or "Run `pipx upgrade blumkin` directly for the full output.",
        )
        raise SystemExit(EXIT_OTHER)

    pipx_app = pipx_blumkin_path(pipx_bin=pipx) or pipx_app
    after = _read_pipx_version(pipx_app) if pipx_app is not None else None

    if as_json:
        emit_json(
            {
                "ok": True,
                "pipx_app": {
                    "path": str(pipx_app) if pipx_app is not None else None,
                    "before": before,
                    "after": after,
                },
                "running_from": {"build": running_build, "path": str(running_path)},
                "source_checkout": source_checkout,
            }
        )
        return

    # `from:` is the pipx app's own pre-upgrade build - the same value as
    # pipx_app.before in --json, never a stand-in from the running process
    # (which may be a different install entirely).
    if before is not None:
        lines = [f"from: {before}"]
    elif pipx_app is not None:
        lines = ["from: (could not read the pipx app before upgrading)"]
    else:
        lines = ["from: (no pipx install of blumkin found)"]
    if after is not None:
        lines.append(f"to:   {after}")
    elif pipx_app is not None:
        lines.append(f"to:   run `{pipx_app} --version` to confirm")
    else:
        lines.append("to:   run `blumkin --version` to confirm")
    if pipx_app is not None:
        lines.append(f"      {pipx_app}")
    if source_checkout:
        lines.append(
            f"note: you ran the source checkout ({running_build}) at {running_path}; "
            "pipx upgrade changed the pipx app above, not this tree"
        )
    emit_lines(lines)


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
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
            _emit_error(error="graph_error", message=msg, as_json=as_json)
            raise SystemExit(EXIT_OTHER) from exc
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
    help="Attendee email; repeat once per attendee. Omit for a solo hold.",
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
    help="Length as a short duration, e.g. 30m, 45m, 1h, 1d, 1w.",
)
@click.option(
    "--remind-email",
    "remind_email",
    default=None,
    help=(
        "Add a reminder this long before start, e.g. 30m, 1h, 1d, 1w. Google: an "
        "email reminder. Microsoft: an Outlook popup reminder (Outlook events have "
        "no per-event email reminder)."
    ),
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
    remind_email: str | None,
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
                remind_email=remind_email,
                teams=teams,
                tz_name=_tz_name(ctx, tz_flag),
            )
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except ZoneInfoNotFoundError as exc:
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
@click.option(
    "--with",
    "with_name",
    default=None,
    help="Display-name substring to match a chat (exclusive with --chat-id).",
)
@click.option(
    "--chat-id",
    "chat_id",
    default=None,
    help="Explicit chat id from `chat find` (exclusive with --with).",
)
@click.option(
    "--contains",
    "contains",
    default=None,
    help="Case-insensitive substring filter over message bodies (local scan, max 500).",
)
@click.option("--n", "n", default=3, show_default=True, type=int, help="How many messages to show.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_last_cmd(
    ctx: click.Context,
    with_name: str | None,
    chat_id: str | None,
    contains: str | None,
    n: int,
    as_json_flag: bool,
) -> None:
    """Show the last N messages from one chat (by --with name or --chat-id).

    Exit 5 (not_found) means no chat matched --with. An ambiguous --with is exit
    2 (usage_error) listing the candidate ids - pass one back as --chat-id.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            _workspace().chat_last(with_name=with_name, chat_id=chat_id, contains=contains, n=n)
        )
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    no_match = payload.get("chat") is None
    if as_json:
        # Deliberate: no-match keeps the payload on stdout with empty stderr
        # (chat == null / ok == false is the signal), pinned by
        # test_diagnostic_commands_report_failure_on_stdout and the agent guide.
        emit_json({**payload, "ok": not no_match})
    else:
        emit_lines(format_last_human(payload))
    if no_match:
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
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
    help=(
        "Graph $search term (whole mailbox); excludes --from / --subject / --since / "
        "--importance / --has-attachments."
    ),
)
@click.option("--since", default=None, help="Only messages at or after this local date/time.")
@click.option("--until", default=None, help="Only messages strictly before this local date/time.")
@click.option("--unread", is_flag=True, help="Only unread messages.")
@click.option(
    "--importance",
    default=None,
    type=click.Choice(MAIL_IMPORTANCE_VALUES, case_sensitive=False),
    help="Only messages at this importance (server-side).",
)
@click.option(
    "--has-attachments",
    "has_attachments",
    is_flag=True,
    help="Only messages with a file attachment (server-side).",
)
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
    importance: str | None,
    has_attachments: bool,
    top: int,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """List recent inbox messages, with optional filters or full-text search.

    `--from` / `--subject` match locally over a newest-first scan capped at 500
    (the payload then reports `complete: false`). `--importance` /
    `--has-attachments` filter server-side and compose with the sort.
    `--search` runs on Graph over the whole mailbox and cannot combine with the
    substring, date, importance, or attachment filters.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        since_dt, until_dt = _mail_time_bounds(ctx, tz_flag, since=since, until=until)
        payload = asyncio.run(
            _workspace().mail_inbox(
                top=top,
                has_attachments=has_attachments,
                importance=importance,
                search=search,
                sender=sender,
                subject=subject,
                since=since_dt,
                unread=unread,
                until=until_dt,
            )
        )
    except ZoneInfoNotFoundError as exc:
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
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
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
    help=(
        "Graph $search term (whole mailbox); excludes --from / --subject / --since / "
        "--importance / --has-attachments."
    ),
)
@click.option("--since", default=None, help="Only messages at or after this local date/time.")
@click.option("--until", default=None, help="Only messages strictly before this local date/time.")
@click.option("--unread", is_flag=True, help="Only unread messages.")
@click.option(
    "--importance",
    default=None,
    type=click.Choice(MAIL_IMPORTANCE_VALUES, case_sensitive=False),
    help="Only messages at this importance (server-side).",
)
@click.option(
    "--has-attachments",
    "has_attachments",
    is_flag=True,
    help="Only messages with a file attachment (server-side).",
)
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
    importance: str | None,
    has_attachments: bool,
    top: int,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """List recent messages from any mail folder (well-known name, id, or name).

    Sort order defaults per folder (sent for Sent Items, created for
    Drafts/Outbox, received otherwise); override with `--orderby`. Same filter
    rules as `mail inbox` (`--importance` / `--has-attachments` are server-side).
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        since_dt, until_dt = _mail_time_bounds(ctx, tz_flag, since=since, until=until)
        payload = asyncio.run(
            _workspace().mail_list(
                top=top,
                folder=folder,
                has_attachments=has_attachments,
                importance=importance,
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
        _emit_error(
            error="usage_error",
            message=f"invalid timezone: {exc}",
            as_json=as_json,
            hint="Use an IANA name like America/New_York or UTC (not an abbreviation).",
        )
        raise SystemExit(EXIT_USAGE) from exc
    except MailFolderNotFoundError as exc:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
        _emit_error(
            error="usage_error",
            message="--id is required",
            as_json=as_json,
            hint="Pass --id <message-id>; get one from `blumkin mail list --json`.",
        )
        raise SystemExit(EXIT_USAGE)
    try:
        payload = asyncio.run(_workspace().mail_attachments_list(message_id=message_id))
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except MailMessageNotFoundError as exc:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except MailAttachmentNotFoundError as exc:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except MailAttachmentSkippedError as exc:
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
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
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailBodyFileError as exc:
        _emit_error(
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
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailMessageNotFoundError as exc:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailMessageNotFoundError as exc:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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


@mail.command("signature", epilog=help_text.MAIL_SIGNATURE_EPILOG)
@click.option(
    "--body-type",
    "body_type",
    default="html",
    show_default=True,
    type=click.Choice(["html", "text"], case_sensitive=False),
    help="Render the signature as HTML or plain text.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_signature_cmd(ctx: click.Context, body_type: str, as_json_flag: bool) -> None:
    """Print the rendered [mail.signature] for the active profile.

    Read-only. Use it to append the exact configured sign-off to a body you are
    composing yourself, instead of hand-reconstructing the markup. Empty output
    means the profile has no signature configured (or it is disabled).
    """
    as_json = _as_json(ctx, as_json_flag)
    cfg = _load_config()
    try:
        rendered = render_mail_signature(cfg.mail_signature, body_type=body_type)
    except ValueError as exc:
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    if as_json:
        emit_json(
            {
                "ok": True,
                "body_type": body_type.lower(),
                "enabled": cfg.mail_signature.enabled,
                "signature": rendered,
            }
        )
    else:
        emit_lines([rendered] if rendered else ["(no signature configured)"])
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
@click.option(
    "--keep-quoted",
    "keep_quoted",
    is_flag=True,
    help="Re-append the quoted original from the existing draft after the new body.",
)
@click.option(
    "--no-signature",
    "no_signature",
    is_flag=True,
    help="Do not reapply [mail.signature] when replacing the body.",
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
    keep_quoted: bool,
    no_signature: bool,
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
                keep_quoted=keep_quoted,
                no_signature=no_signature,
            )
        )
    except MailAttachError as exc:
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    except MailBodyFileError as exc:
        _emit_error(
            error="usage_error",
            message=str(exc),
            as_json=as_json,
        )
        raise SystemExit(EXIT_USAGE) from exc
    except MailDraftNotFoundError as exc:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
        _require_yes(
            yes=yes,
            as_json=as_json,
            reason="This changes a meeting setting (allowTranscription).",
        )
    try:
        payload = asyncio.run(_workspace().meeting_transcription(event_id=event_id, enable=enable))
    except LookupError as exc:
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
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
        _emit_error(error="not_found", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        _raise_auth_value_error(exc, as_json=as_json)
    except Exception as exc:
        _raise_graph_http_error(exc, as_json=as_json)
    ambiguous = bool(payload.get("ambiguous"))
    if as_json:
        emit_json({**payload, "ok": not ambiguous})
    else:
        emit_lines(format_resolve_human(payload))
    if ambiguous:
        raise SystemExit(EXIT_USAGE)
    raise SystemExit(EXIT_SUCCESS)


if __name__ == "__main__":
    main(obj={})
