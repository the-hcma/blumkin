"""Click CLI entrypoint for blumkin."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import click
from click.shell_completion import get_completion_class

from blumkin import help_text
from blumkin.auth import AuthRequiredError, AuthTransientError, MissingScopeError, SecretWriteError
from blumkin.config import BlumkinConfig, list_profiles, load_config, set_profile_email
from blumkin.contacts import format_people_context_human
from blumkin.exit_codes import (
    EXIT_AUTH,
    EXIT_MISSING_SCOPE,
    EXIT_NOT_FOUND,
    EXIT_OTHER,
    EXIT_SUCCESS,
    EXIT_USAGE,
)
from blumkin.install_method import (
    METHOD_EDITABLE_PIPX,
    METHOD_EDITABLE_UV,
    METHOD_PIPX,
    METHOD_SOURCE_CHECKOUT,
    METHOD_UNMANAGED,
    METHOD_UV_TOOL,
    Install,
    detect_install,
    metadata_stale,
    suggested_commands,
    upgrade_steps,
)
from blumkin.mail_signature_state import (
    clear_signature_state,
    load_signature_state,
    record_signature_state,
)
from blumkin.output import emit_error, emit_json, emit_lines
from blumkin.providers import get_provider
from blumkin.providers.kind import ProviderConfigError, ProviderKind
from blumkin.providers.protocol import WorkspaceProvider
from blumkin.skills import describe_skill, skills_catalog
from blumkin.skills.calendar import (
    format_calendar_get_human,
    format_calendar_list_human,
    format_freebusy_human,
    format_suggest_human,
    format_today_human,
    format_view_human,
)
from blumkin.skills.calendar_writes import (
    format_cancel_human,
    format_create_human,
    format_rsvp_human,
    format_update_human,
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
from blumkin.skills.chat import (
    format_edit_human,
    format_find_human,
    format_last_human,
    format_send_human,
)
from blumkin.skills.dispatch import run_skill
from blumkin.skills.docs import format_docs_create_human, format_docs_update_human
from blumkin.skills.drive import (
    format_drive_download_human,
    format_drive_export_human,
    format_drive_get_human,
    format_drive_list_human,
    format_drive_mkdir_human,
    format_drive_move_human,
    format_drive_read_human,
    format_drive_rename_human,
)
from blumkin.skills.errors import ErrorInfo, classify_exception
from blumkin.skills.mail import (
    MAIL_IMPORTANCE_VALUES,
    WELL_KNOWN_MAIL_FOLDERS,
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
    format_auto_reply_human as format_mail_auto_reply_human,
)
from blumkin.skills.mail import (
    format_get_human as format_mail_get_human,
)
from blumkin.skills.mail import (
    format_search_human as format_mail_search_human,
)
from blumkin.skills.mail import (
    format_thread_human as format_mail_thread_human,
)
from blumkin.skills.mail import (
    format_triage_human as format_mail_triage_human,
)
from blumkin.skills.meeting import (
    format_get_human as format_meeting_get_human,
)
from blumkin.skills.meeting import format_transcription_human
from blumkin.skills.people import format_resolve_human
from blumkin.tasks import format_tasks_list_human, format_tasks_show_human
from blumkin.version import (
    build_info,
    build_status_fields,
    build_version,
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
    "install_failed": (
        "Completion `--install` could not write the script. Ensure the target path is "
        "a writable file you own (not a directory), then retry with --force."
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
        "Run the failing command directly for the full output. `blumkin upgrade` "
        "picks pipx / uv tool / a checkout reinstall to match how blumkin is "
        "installed; `blumkin doctor` reports the detected method."
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

_INSTALL_METHOD_LABELS: dict[str, str] = {
    METHOD_EDITABLE_PIPX: "editable pipx install (-e <path>)",
    METHOD_EDITABLE_UV: "editable uv tool install (-e <path>)",
    METHOD_PIPX: "pipx",
    METHOD_SOURCE_CHECKOUT: "source checkout (not tool-managed)",
    METHOD_UNMANAGED: "not package-managed (plain venv or system)",
    METHOD_UV_TOOL: "uv tool",
}

# One `git pull` / `pipx|uv` step in `blumkin upgrade`. A cold reinstall builds
# no wheels (blumkin is pure Python) but still resolves and downloads deps.
_UPGRADE_STEP_TIMEOUT_S = 300


def _as_json(ctx: click.Context, as_json_flag: bool) -> bool:
    value = bool(ctx.obj.get("as_json") or as_json_flag)
    ctx.obj["as_json"] = value
    return value


def _auth_status_payload(config: BlumkinConfig | None = None) -> dict[str, Any]:
    """Auth-status fields plus the resolved build (version, commit, binary path)."""
    payload = dict(_workspace(config).auth_status())
    payload.update(build_status_fields())
    return payload


def _completion_install_path(shell: str) -> Path:
    """Conventional per-user path for a shell's blumkin completion script."""
    xdg_data = _xdg_base("XDG_DATA_HOME", Path.home() / ".local" / "share")
    if shell == "bash":
        return xdg_data / "bash-completion" / "completions" / "blumkin.bash"
    if shell == "fish":
        xdg_config = _xdg_base("XDG_CONFIG_HOME", Path.home() / ".config")
        return xdg_config / "fish" / "completions" / "blumkin.fish"
    return xdg_data / "zsh" / "site-functions" / "_blumkin"


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


def _load_config() -> BlumkinConfig:
    """Load config for the active command, cached on the Click context.

    A single command (``_dispatch`` / ``_dispatch_soft``) calls this once directly
    and once more inside the default-argument path of ``_workspace()`` - without
    caching, that means two fresh ``config.toml`` parses per command, and a
    preferences conflict warning (``blumkin.config._preferences_config``) firing
    twice on stderr for one invocation.
    """
    ctx = click.get_current_context(silent=True)
    profile: str | None = None
    if ctx is not None and isinstance(ctx.obj, dict):
        cached = ctx.obj.get("_loaded_config")
        if isinstance(cached, BlumkinConfig):
            return cached
        raw = ctx.obj.get("profile")
        if isinstance(raw, str) and raw.strip():
            profile = raw.strip()
    try:
        cfg = load_config(profile=profile)
    except ProviderConfigError as exc:
        _emit_error(error="usage_error", message=str(exc), as_json=_cli_as_json())
        raise SystemExit(EXIT_USAGE) from exc
    if ctx is not None and isinstance(ctx.obj, dict):
        ctx.obj["_loaded_config"] = cfg
    return cfg


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
        )
    except Exception:
        return None
    return address if written else None


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


def _checkout_lines(checkout: Any) -> list[str]:
    if checkout.behind_origin is None:
        origin = "origin comparison unavailable"
    elif checkout.behind_origin == 0:
        origin = "up to date with origin"
    else:
        count = checkout.behind_origin
        origin = f"{count} commit{'' if count == 1 else 's'} behind origin"
    if checkout.dirty is None:
        dirty = "status unknown"
    else:
        dirty = "uncommitted changes" if checkout.dirty else "clean"
    return [
        f"checkout: {checkout.path}",
        f"          branch {checkout.branch or '(detached)'}, {origin}, {dirty}",
    ]


def _editable_upgrade_steps(install: Install, *, as_json: bool) -> list[list[str]]:
    """`upgrade_steps` with each leading command resolved to an absolute path.

    Same source list as `suggested_commands` (both from `upgrade_steps`), so the
    printed / `action_taken` text and the argv actually run cannot diverge.
    """
    return [
        [_require_manager(step[0], as_json=as_json), *step[1:]] for step in upgrade_steps(install)
    ]


def _emit_upgrade_result(
    install: Install,
    *,
    action_taken: str | None,
    after: str | None,
    as_json: bool,
    before: str | None,
    running_build: str,
    running_path: Path,
    stale: tuple[str, str] | None,
) -> None:
    if as_json:
        emit_json(
            {
                "ok": True,
                "action_taken": action_taken,
                "checkout": install.checkout.as_dict() if install.checkout is not None else None,
                "from": before,
                "install_method": install.method,
                "managed_path": (
                    str(install.managed_path) if install.managed_path is not None else None
                ),
                "manager": install.manager,
                "metadata_stale": stale is not None,
                "running_from": {"build": running_build, "path": str(running_path)},
                "suggested_commands": suggested_commands(install),
                "to": after,
            }
        )
        return
    emit_lines(
        _upgrade_human_lines(
            install, action_taken=action_taken, after=after, before=before, stale=stale
        )
    )


def _raise_chat_attachment_error(exc: BaseException, *, as_json: bool) -> NoReturn:
    """Kept for tests that call it directly; delegates to the shared classifier."""
    _fail(exc, as_json=as_json)


def _read_app_version(executable: Path) -> str | None:
    """Return ``<executable> --version`` as ``<version> (<commit>)``, or None.

    The first line of ``blumkin --version`` is ``blumkin <version> (<commit>)``;
    the ``blumkin `` prefix is stripped so the value compares directly against
    :func:`blumkin.version.build_version` (the from/to pair in ``upgrade``). The
    executable is re-run on disk, so it reflects a just-applied upgrade even
    though the calling process is still the old build.
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


def _require_manager(name: str, *, as_json: bool) -> str:
    """Resolve `name` on PATH or exit `upgrade_failed` - the install needs it."""
    found = shutil.which(name)
    if found is None:
        _emit_error(
            error="upgrade_failed",
            message=f"`{name}` is not on PATH",
            as_json=as_json,
            hint=f"`blumkin upgrade` needs `{name}` for this install - install it and retry.",
        )
        raise SystemExit(EXIT_OTHER)
    return found


def _run_upgrade_command(cmd: list[str], *, as_json: bool, timeout: int) -> None:
    """Run one upgrade step; emit an error and exit non-zero on any failure."""
    printable = " ".join(cmd)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, check=False, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        _emit_error(
            error="timeout",
            message=f"`{printable}` timed out",
            as_json=as_json,
            hint=f"Run `{printable}` directly to see where it hangs.",
        )
        raise SystemExit(EXIT_OTHER) from exc
    except OSError as exc:
        _emit_error(
            error="upgrade_failed",
            message=f"could not run `{printable}`: {exc}",
            as_json=as_json,
        )
        raise SystemExit(EXIT_OTHER) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        _emit_error(
            error="upgrade_failed",
            message=f"`{printable}` failed",
            as_json=as_json,
            hint=detail or f"Run `{printable}` directly for the full output.",
        )
        raise SystemExit(EXIT_OTHER)


def _upgrade_human_lines(
    install: Install,
    *,
    action_taken: str | None,
    after: str | None,
    before: str | None,
    stale: tuple[str, str] | None,
) -> list[str]:
    lines = [f"install: {_INSTALL_METHOD_LABELS.get(install.method, install.method)}"]
    if install.managed_path is not None:
        lines.append(f"         {install.managed_path}")
    if install.checkout is not None:
        lines.extend(_checkout_lines(install.checkout))
    if stale is not None:
        lines.append(
            f"note: installed metadata ({stale[0]}) is stale vs the checkout ({stale[1]}) - "
            "reinstall to re-bake it"
        )

    if install.method == METHOD_UNMANAGED:
        lines.append("blumkin upgrade: not package-managed - nothing to upgrade here.")
        lines.append(
            "  install a managed copy:  uv tool install blumkin  (or  pipx install blumkin)"
        )
        return lines

    if action_taken is None and install.checkout is not None:
        lines.append(
            "blumkin upgrade: an editable / source install is not advanced by a package "
            "upgrade. Run:"
        )
        lines.extend(f"  {command}" for command in suggested_commands(install))
        lines.append("  (or re-run `blumkin upgrade --yes` to run them now)")
        return lines

    if action_taken is not None:
        lines.append(f"ran:  {action_taken}")
    lines.append(f"from: {before or '(unknown)'}")
    lines.append(f"to:   {after or '(run `blumkin --version` to confirm)'}")
    return lines


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
            "skills work without them; chat write, meeting skills, people resolve, "
            "and mail auto-reply do not."
        ),
        as_json=as_json,
        hint=(
            "Set wo1162425_scopes = true in config.toml once Remedy WO1162425 has "
            "granted its add-ons (at least Chat.ReadWrite, MailboxSettings.ReadWrite, "
            "OnlineMeetings.ReadWrite, People.Read; see HANDOFF.md, some asks may still "
            "be pending), then delete "
            "the token cache and auth record and run `blumkin auth login`."
        ),
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


def _refresh_signature_probe(config: BlumkinConfig) -> None:
    """Best-effort: detect whether the mail client auto-signs, and cache the answer.

    Run from ``auth login`` / ``doctor``. Any failure - the probe (no Mail scope,
    offline, Graph error) or the state write (read-only / full config dir) -
    leaves the cached state untouched and must never break an otherwise
    successful login, so the whole thing is swallowed.
    """
    try:
        detected = asyncio.run(_workspace(config).probe_mail_signature())
        record_signature_state(config, detected=detected)
    except Exception:
        return


def _fail(exc: BaseException, *, as_json: bool) -> NoReturn:
    """Classify any exception and turn it into the documented envelope + exit code."""
    info: ErrorInfo = classify_exception(exc)
    _emit_error(error=info.slug, message=info.message, as_json=as_json, hint=info.hint)
    raise SystemExit(info.exit_code) from exc


def _dispatch(
    ctx: click.Context,
    skill_id: str,
    arguments: dict[str, Any],
    *,
    human: Any,
    as_json_flag: bool,
) -> NoReturn:
    """Run a skill through :func:`run_skill` and emit its payload the standard way."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            run_skill(skill_id, arguments, config=_load_config(), provider=_workspace())
        )
    except Exception as exc:  # noqa: BLE001 - classify_exception owns the taxonomy
        _fail(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(human(payload))
    raise SystemExit(EXIT_SUCCESS)


def _dispatch_local(
    ctx: click.Context,
    skill_id: str,
    arguments: dict[str, Any],
    *,
    human: Any,
    as_json_flag: bool,
) -> NoReturn:
    """Like :func:`_dispatch` for a `CONFIG_SKILLS` id - reads an operator file,
    needs no provider, so no auth setup is required to run it."""
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(run_skill(skill_id, arguments, config=_load_config()))
    except Exception as exc:  # noqa: BLE001 - classify_exception owns the taxonomy
        _fail(exc, as_json=as_json)
    if as_json:
        emit_json(payload)
    else:
        emit_lines(human(payload))
    raise SystemExit(EXIT_SUCCESS)


def _dispatch_soft(
    ctx: click.Context,
    skill_id: str,
    arguments: dict[str, Any],
    *,
    human: Any,
    as_json_flag: bool,
    is_failure: Any,
    fail_exit: int,
) -> NoReturn:
    """Like :func:`_dispatch` but for skills that report a soft miss on stdout.

    ``chat last`` (no chat matched) / ``people resolve`` (ambiguous) keep their
    payload on stdout with ``ok: false`` and a non-zero exit - the agent guide
    and ``test_diagnostic_commands_report_failure_on_stdout`` pin this.
    """
    as_json = _as_json(ctx, as_json_flag)
    try:
        payload = asyncio.run(
            run_skill(skill_id, arguments, config=_load_config(), provider=_workspace())
        )
    except Exception as exc:  # noqa: BLE001
        _fail(exc, as_json=as_json)
    failed = bool(is_failure(payload))
    if as_json:
        emit_json({**payload, "ok": not failed})
    else:
        emit_lines(human(payload))
    raise SystemExit(fail_exit if failed else EXIT_SUCCESS)


def _xdg_base(var: str, default: Path) -> Path:
    """XDG base dir from ``$var``, honoring the spec: a relative value is ignored."""
    value = os.environ.get(var, "")
    candidate = Path(value) if value else default
    return candidate if candidate.is_absolute() else default


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
    cfg = _load_config()
    _refresh_signature_probe(cfg)
    signature_state = load_signature_state(cfg)
    if as_json:
        emit_json(
            {
                "ok": True,
                "email_written": populated,
                "outlook_signature_detected": signature_state.detected,
                "status": _auth_status_payload(),
            }
        )
    else:
        emit_lines(["Signed in. Token cache written under ~/.config/blumkin/."])
        if populated:
            emit_lines([f"Recorded account email in config.toml: {populated}"])
        if signature_state.suppresses_signature:
            emit_lines(
                [
                    "Outlook adds its own signature for this account - blumkin will "
                    "not append [mail.signature] to drafts (avoids a double signature)."
                ]
            )


@auth.command("logout", epilog=help_text.AUTH_LOGOUT_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def auth_logout(ctx: click.Context, as_json_flag: bool) -> None:
    """Delete this machine's token cache and auth record.

    The next Graph call needs a fresh `auth login`.
    """
    as_json = _as_json(ctx, as_json_flag)
    cfg = _load_config()
    _workspace(cfg).auth_logout()
    clear_signature_state(cfg)
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
@click.option(
    "--install",
    is_flag=True,
    help="Write the script to the conventional per-user completion dir instead of printing it.",
)
@click.option(
    "--force",
    is_flag=True,
    help="With --install, overwrite an existing file whose contents differ.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def completion(
    ctx: click.Context, shell: str, install: bool, force: bool, as_json_flag: bool
) -> None:
    """Print a tab-completion script for bash, zsh, or fish (or --install it).

    Source the output to enable `<TAB>` completion of blumkin commands, options,
    and Choice values. `--install` writes it to the per-user completion directory
    for the shell (idempotent; `--force` overwrites a differing file). See the
    epilog for one-liners per shell.
    """
    as_json = _as_json(ctx, as_json_flag)
    if force and not install:
        _emit_error(
            error="usage_error",
            message="--force only applies with --install",
            as_json=as_json,
        )
        raise SystemExit(EXIT_USAGE)
    comp_cls = get_completion_class(shell)
    if comp_cls is None:  # pragma: no cover - Choice already constrains shell
        _emit_error(
            error="usage_error",
            message=f"no completion support for shell: {shell}",
            as_json=as_json,
            hint="Supported shells: bash, zsh, fish.",
        )
        raise SystemExit(EXIT_USAGE)
    completer = comp_cls(main, {}, "blumkin", "_BLUMKIN_COMPLETE")
    script = completer.source()
    if not script.endswith("\n"):
        script += "\n"

    if not install:
        if as_json:
            emit_json({"shell": shell, "script": script})
        else:
            click.echo(script, nl=False)
        raise SystemExit(EXIT_SUCCESS)

    path = _completion_install_path(shell)
    want = script.encode()

    def _refuse_clobber() -> NoReturn:
        _emit_error(
            error="usage_error",
            message=f"{path} already exists with different contents",
            as_json=as_json,
            hint="Re-run with --force to overwrite it.",
        )
        raise SystemExit(EXIT_USAGE)

    try:
        if path.exists() and not path.is_file():
            # A directory, FIFO, socket, or device at the target: is_file() is
            # False so the paths below would misread it as "nothing there".
            _emit_error(
                error="install_failed",
                message=f"{path} exists but is not a regular file",
                as_json=as_json,
                hint="Remove it (or point XDG_DATA_HOME/XDG_CONFIG_HOME elsewhere), then retry.",
            )
            raise SystemExit(EXIT_OTHER)
        # Compare bytes so a non-UTF-8 file at the target (hand-placed, another
        # tool) is "different", not a decode crash.
        current = path.read_bytes() if path.is_file() else None
        if current == want:
            action = "unchanged"
        elif current is not None and not force:
            _refuse_clobber()
        elif force:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(want)
            action = "written"
        else:
            # Nothing there a moment ago: create exclusively so a racing
            # --install cannot be clobbered without --force.
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as handle:
                    handle.write(want)
                action = "written"
            except FileExistsError:
                if path.read_bytes() != want:
                    _refuse_clobber()
                action = "unchanged"
    except OSError as exc:
        # A directory at the target, an unwritable XDG dir, or a file owned by
        # another account: report it the way every other I/O site does, not as a
        # raw traceback (and keep the --json contract).
        _emit_error(
            error="install_failed",
            message=f"could not install the completion script to {path}: {exc}",
            as_json=as_json,
            hint="Ensure the path is a writable file (not a directory) that you own.",
        )
        raise SystemExit(EXIT_OTHER) from exc

    if as_json:
        emit_json({"shell": shell, "path": str(path), "action": action})
    else:
        lines = [f"{action}: {path}"]
        if shell == "zsh":
            lines.append(
                f"  ensure {path.parent} is on $fpath before `compinit` "
                "(e.g. in ~/.zshrc), then open a new shell"
            )
        else:
            lines.append("  open a new shell to pick it up")
        emit_lines(lines)
    raise SystemExit(EXIT_SUCCESS)


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
    # Re-probe the mail-client auto-signature (the Outlook setting Graph does not
    # expose) so a change since login is picked up. Best-effort; needs a working
    # auth cache, so skip it when the checks above already found auth problems.
    if not problems:
        _refresh_signature_probe(cfg)
    signature_state = load_signature_state(cfg)
    if signature_state.suppresses_signature:
        warnings.append(
            "this account's mail client auto-inserts its own signature - blumkin is "
            "not appending [mail.signature] to drafts to avoid a double signature"
        )
    build = build_status_fields()
    install = detect_install()
    stale = metadata_stale(install.checkout.path) if install.checkout is not None else None
    if stale is not None:
        reinstall = next(iter(suggested_commands(install)[1:]), "reinstall from the checkout")
        warnings.append(
            f"installed metadata ({stale[0]}) is stale vs the checkout ({stale[1]}) - a "
            f"`git pull` did not re-bake it; run: {reinstall}"
        )
    payload = {
        "ok": not problems,
        "build": build,
        "wo1162425_scopes": cfg.wo1162425_scopes,
        "problems": problems,
        "warnings": warnings,
        "install": {
            "checkout": install.checkout.as_dict() if install.checkout is not None else None,
            "managed_path": (
                str(install.managed_path) if install.managed_path is not None else None
            ),
            "metadata_stale": stale is not None,
            "method": install.method,
        },
        "mail_signature": {
            "configured": cfg.mail_signature.enabled,
            "outlook_signature_detected": signature_state.detected,
            "suppressed": signature_state.suppresses_signature and cfg.mail_signature.enabled,
        },
        "status": status,
        "skills": [s["id"] for s in skills_catalog()["skills"]],
    }
    if as_json:
        emit_json(payload)
    else:
        emit_lines([f"ok: {payload['ok']}"])
        emit_lines([f"build: {build['build_version']} ({build['build_commit']})"])
        emit_lines([f"install: {install.method}"])
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
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    help="Run the upgrade commands (an editable / source checkout needs this).",
)
@click.pass_context
def upgrade(ctx: click.Context, as_json_flag: bool, yes: bool) -> None:
    """Upgrade blumkin the way it was installed - pipx, uv tool, or an editable checkout.

    Detects the install method rather than assuming pipx: `pipx upgrade blumkin`
    for a pipx app, `uv tool upgrade blumkin` for a uv tool, and for an editable
    `-e <path>` install (or a bare source checkout) the git pull + `--force`
    reinstall that a package "upgrade" cannot do - printed to run yourself, or
    run for you with `--yes`. An unmanaged install is reported, not touched.

    `--json` carries `install_method`, `managed_path`, `checkout`,
    `metadata_stale`, `action_taken`, and `suggested_commands`.
    """
    as_json = _as_json(ctx, as_json_flag)
    running_build = build_version()
    running_path = running_command_path()
    install = detect_install()
    before = _read_app_version(install.managed_path) if install.managed_path is not None else None
    stale = metadata_stale(install.checkout.path) if install.checkout is not None else None

    action_taken: str | None = None
    if install.method == METHOD_UNMANAGED:
        _emit_upgrade_result(
            install,
            as_json=as_json,
            action_taken=None,
            after=before,
            before=before,
            running_build=running_build,
            running_path=running_path,
            stale=stale,
        )
        return

    if install.checkout is not None:
        if not yes:
            _emit_upgrade_result(
                install,
                as_json=as_json,
                action_taken=None,
                after=before,
                before=before,
                running_build=running_build,
                running_path=running_path,
                stale=stale,
            )
            return
        for step in _editable_upgrade_steps(install, as_json=as_json):
            _run_upgrade_command(step, as_json=as_json, timeout=_UPGRADE_STEP_TIMEOUT_S)
        action_taken = " && ".join(suggested_commands(install))
    else:
        manager_bin = _require_manager(install.manager or "", as_json=as_json)
        step = (
            [manager_bin, "upgrade", "blumkin"]
            if install.manager == "pipx"
            else [manager_bin, "tool", "upgrade", "blumkin"]
        )
        _run_upgrade_command(step, as_json=as_json, timeout=_UPGRADE_STEP_TIMEOUT_S)
        action_taken = f"{install.manager} {' '.join(step[1:])}"

    after = _read_app_version(install.managed_path) if install.managed_path is not None else None
    stale = metadata_stale(install.checkout.path) if install.checkout is not None else None
    _emit_upgrade_result(
        install,
        as_json=as_json,
        action_taken=action_taken,
        after=after,
        before=before,
        running_build=running_build,
        running_path=running_path,
        stale=stale,
    )


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
@click.option(
    "--calendar", "calendar", default=None, help="Calendar name or id (default: primary)."
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def calendar_today_cmd(
    ctx: click.Context, day: Any, calendar: str | None, as_json_flag: bool, tz_flag: str | None
) -> None:
    """List events for the local day (today, or --date YYYY-MM-DD).

    Graph returns UTC; blumkin converts to --tz or the config default. Use
    --json to get event ids for accept / cancel / update. `--calendar` targets a
    non-default calendar (name or id from `blumkin calendar list`).
    """
    _dispatch(
        ctx,
        "calendar.today",
        {"date": day, "calendar": calendar, "tz": _tz_name(ctx, tz_flag)},
        human=format_today_human,
        as_json_flag=as_json_flag,
    )


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
@click.option(
    "--calendar", "calendar", default=None, help="Calendar name or id (default: primary)."
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def calendar_view_cmd(
    ctx: click.Context,
    from_day: Any,
    to_day: Any,
    calendar: str | None,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """List events across a local date range [--from, --to).

    The range is half-open: `--to` is the first day NOT shown, so
    `--from 2026-09-01 --to 2026-09-08` covers exactly that week.
    """
    _dispatch(
        ctx,
        "calendar.view",
        {"from": from_day, "to": to_day, "calendar": calendar, "tz": _tz_name(ctx, tz_flag)},
        human=format_view_human,
        as_json_flag=as_json_flag,
    )


@calendar.command("get", epilog=help_text.CALENDAR_GET_EPILOG)
@click.option("--event-id", required=True, help="Event id (from a listing).")
@click.option(
    "--body-type",
    default="text",
    show_default=True,
    type=click.Choice(["html", "text"]),
    help="Body format to request; html keeps the markup.",
)
@click.option(
    "--calendar", "calendar", default=None, help="Calendar name or id (default: primary)."
)
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_get_cmd(
    ctx: click.Context,
    event_id: str,
    body_type: str,
    calendar: str | None,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Read one event in full: body, per-attendee responses, recurrence, join URL.

    Prefer this over scraping a `calendar view` listing once you have the id.
    """
    _dispatch(
        ctx,
        "calendar.get",
        {
            "event_id": event_id,
            "body_type": body_type,
            "calendar": calendar,
            "tz": _tz_name(ctx, tz_flag),
        },
        human=format_calendar_get_human,
        as_json_flag=as_json_flag,
    )


@calendar.command("list", epilog=help_text.CALENDAR_LIST_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_list_cmd(ctx: click.Context, as_json_flag: bool) -> None:
    """List the calendars this account can see (id, name, default, editability).

    Pass an `id` or `name` from here to `--calendar` on the other calendar verbs.
    """
    _dispatch(ctx, "calendar.list", {}, human=format_calendar_list_human, as_json_flag=as_json_flag)


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
    _dispatch(
        ctx,
        "calendar.freebusy",
        {
            "with": list(with_emails),
            "start": start_raw,
            "end": end_raw,
            "tz": _tz_name(ctx, tz_flag),
        },
        human=format_freebusy_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "calendar.suggest",
        {
            "with": list(with_emails),
            "start": start_raw,
            "end": end_raw,
            "duration": duration,
            "window": window,
            "treat_tentative": treat_tentative,
            "limit": limit,
            "tz": _tz_name(ctx, tz_flag),
        },
        human=format_suggest_human,
        as_json_flag=as_json_flag,
    )


def _run_calendar_rsvp(
    ctx: click.Context,
    *,
    verb: str,
    event_id: str | None,
    today_pending: bool,
    yes: bool,
    comment: str | None,
    propose_start: str | None,
    propose_duration: str | None,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    arguments: dict[str, Any] = {
        "event_id": event_id,
        "today_pending": today_pending,
        "comment": comment,
        "tz": _tz_name(ctx, tz_flag),
        "yes": yes,
    }
    if verb != "accept":
        arguments["propose_time"] = propose_start
        arguments["propose_duration"] = propose_duration
    _dispatch(
        ctx,
        f"calendar.{verb}",
        arguments,
        human=format_rsvp_human,
        as_json_flag=as_json_flag,
    )


@calendar.command("accept", epilog=help_text.CALENDAR_ACCEPT_EPILOG)
@click.option("--event-id", "event_id", default=None, help="Single event id to accept.")
@click.option(
    "--today-pending",
    "today_pending",
    is_flag=True,
    help="Accept all not-yet-responded events for today.",
)
@click.option("--comment", "comment", default=None, help="Note to the organizer.")
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_accept_cmd(
    ctx: click.Context,
    event_id: str | None,
    today_pending: bool,
    comment: str | None,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Accept one invitation (--event-id) or all pending ones for today.

    Sends a response to each organizer, so --yes is required. Event ids come
    from `blumkin calendar today --json`.
    """
    _run_calendar_rsvp(
        ctx,
        verb="accept",
        event_id=event_id,
        today_pending=today_pending,
        yes=yes,
        comment=comment,
        propose_start=None,
        propose_duration=None,
        tz_flag=tz_flag,
        as_json_flag=as_json_flag,
    )


_RSVP_NO_MAYBE_OPTIONS = (
    click.option("--event-id", "event_id", default=None, help="Single event id."),
    click.option(
        "--today-pending",
        "today_pending",
        is_flag=True,
        help="Respond to all not-yet-responded events for today.",
    ),
    click.option("--comment", "comment", default=None, help="Note to the organizer."),
    click.option(
        "--propose-time",
        "propose_start",
        default=None,
        help="Suggest a new start (Microsoft only; e.g. 2026-09-02T15:00).",
    ),
    click.option(
        "--propose-duration",
        "propose_duration",
        default=None,
        help="Length of the proposed slot (with --propose-time; default 30m).",
    ),
    click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action."),
    click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config)."),
    click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout."),
)


def _rsvp_no_maybe_options(fn):  # noqa: ANN001, ANN202 - Click decorator stack
    for decorator in reversed(_RSVP_NO_MAYBE_OPTIONS):
        fn = decorator(fn)
    return fn


@calendar.command("decline", epilog=help_text.CALENDAR_DECLINE_EPILOG)
@_rsvp_no_maybe_options
@click.pass_context
def calendar_decline_cmd(
    ctx: click.Context,
    event_id: str | None,
    today_pending: bool,
    comment: str | None,
    propose_start: str | None,
    propose_duration: str | None,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Decline one invitation (--event-id) or all pending ones for today. Requires --yes."""
    _run_calendar_rsvp(
        ctx,
        verb="decline",
        event_id=event_id,
        today_pending=today_pending,
        yes=yes,
        comment=comment,
        propose_start=propose_start,
        propose_duration=propose_duration,
        tz_flag=tz_flag,
        as_json_flag=as_json_flag,
    )


@calendar.command("tentative", epilog=help_text.CALENDAR_DECLINE_EPILOG)
@_rsvp_no_maybe_options
@click.pass_context
def calendar_tentative_cmd(
    ctx: click.Context,
    event_id: str | None,
    today_pending: bool,
    comment: str | None,
    propose_start: str | None,
    propose_duration: str | None,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Respond "tentative" to one invitation or today's pending ones. Requires --yes."""
    _run_calendar_rsvp(
        ctx,
        verb="tentative",
        event_id=event_id,
        today_pending=today_pending,
        yes=yes,
        comment=comment,
        propose_start=propose_start,
        propose_duration=propose_duration,
        tz_flag=tz_flag,
        as_json_flag=as_json_flag,
    )


@calendar.command("cancel", epilog=help_text.CALENDAR_CANCEL_EPILOG)
@click.option("--event-id", "event_id", required=True, help="Event id to cancel (organizer only).")
@click.option(
    "--calendar", "calendar", default=None, help="Calendar name or id (default: primary)."
)
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_cancel_cmd(
    ctx: click.Context, event_id: str, calendar: str | None, yes: bool, as_json_flag: bool
) -> None:
    """Cancel an event you organize and notify every attendee. Requires --yes."""
    _dispatch(
        ctx,
        "calendar.cancel",
        {"event_id": event_id, "calendar": calendar, "yes": yes},
        human=format_cancel_human,
        as_json_flag=as_json_flag,
    )


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
    help="Local start, YYYY-MM-DDTHH:MM (or YYYY-MM-DD with --all-day).",
)
@click.option(
    "--duration",
    default=None,
    help="Length, e.g. 30m, 1h (default 30m); with --all-day, whole days like 2d (default 1d).",
)
@click.option(
    "--all-day",
    "all_day",
    is_flag=True,
    help="All-day event; --start is a date, --duration is in whole days.",
)
@click.option("--location", default=None, help="Free-text location (a room, 'Zoom', a phone line).")
@click.option(
    "--calendar",
    "calendar",
    default=None,
    help="Create on this calendar (name or id; default: primary).",
)
@click.option(
    "--optional",
    "optional_emails",
    multiple=True,
    help="Optional attendee email; repeat once per attendee (vs required --with).",
)
@click.option("--body", default=None, help="Event body / agenda text.")
@click.option("--body-file", "body_file", default=None, help="Read the event body from this file.")
@click.option(
    "--body-type",
    "body_type",
    default="text",
    show_default=True,
    type=click.Choice(["html", "text"]),
    help="Body format (Microsoft only).",
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
@click.option(
    "--repeat",
    "repeat",
    type=click.Choice(["daily", "weekly", "monthly"]),
    default=None,
    help="Make this a recurring series instead of a single event.",
)
@click.option(
    "--interval",
    "interval",
    type=int,
    default=1,
    show_default=True,
    help="Repeat every N days/weeks/months (with --repeat).",
)
@click.option(
    "--until",
    "until",
    default=None,
    help="End the series on this date, YYYY-MM-DD (mutually exclusive with --count).",
)
@click.option(
    "--count",
    "count",
    type=int,
    default=None,
    help="Stop the series after N occurrences (mutually exclusive with --until).",
)
@click.option(
    "--days",
    "days",
    default=None,
    help="Weekly only: comma list of weekdays, e.g. mon,tue,wed,thu,fri.",
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
    duration: str | None,
    all_day: bool,
    location: str | None,
    calendar: str | None,
    optional_emails: tuple[str, ...],
    body: str | None,
    body_file: str | None,
    body_type: str,
    remind_email: str | None,
    teams: bool,
    repeat: str | None,
    interval: int,
    until: str | None,
    count: int | None,
    days: str | None,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Create an event and invite the --with attendees. Requires --yes.

    A Teams online meeting is added by default; pass --no-teams for an offline
    hold. --start stays in the organizer timezone. For a cross-zone or external
    attendee, check `calendar freebusy` / `calendar suggest` first. Pass --repeat
    for a recurring series.
    """
    _dispatch(
        ctx,
        "calendar.create",
        {
            "subject": subject,
            "with": list(with_emails),
            "start": start_raw,
            "duration": duration,
            "all_day": all_day,
            "location": location,
            "calendar": calendar,
            "optional": list(optional_emails),
            "body": body,
            "body_file": body_file,
            "body_type": body_type,
            "remind_email": remind_email,
            "no_teams": not teams,
            "repeat": repeat,
            "interval": interval,
            "until": until,
            "count": count,
            "days": days,
            "tz": _tz_name(ctx, tz_flag),
            "yes": yes,
        },
        human=format_create_human,
        as_json_flag=as_json_flag,
    )


@calendar.command("update", epilog=help_text.CALENDAR_UPDATE_EPILOG)
@click.option("--event-id", required=True, help="Event id to edit.")
@click.option("--subject", default=None, help="New event title.")
@click.option(
    "--start", "start_raw", default=None, help="New local start (or date with --all-day)."
)
@click.option("--end", "end_raw", default=None, help="New local end (exclusive with --duration).")
@click.option("--duration", default=None, help="New length, e.g. 45m, 1h (exclusive with --end).")
@click.option(
    "--all-day/--no-all-day",
    "all_day",
    default=None,
    help="Convert to / from an all-day event.",
)
@click.option("--location", default=None, help="New free-text location.")
@click.option(
    "--calendar",
    "calendar",
    default=None,
    help="Calendar the event is on (name or id; default: primary).",
)
@click.option("--body", default=None, help="New event body / agenda.")
@click.option("--body-file", "body_file", default=None, help="Read the new body from this file.")
@click.option(
    "--body-type",
    "body_type",
    default="text",
    type=click.Choice(["html", "text"]),
    help="Body format (Microsoft only).",
)
@click.option(
    "--with",
    "with_emails",
    multiple=True,
    help="Replace the attendee list with these emails (repeat per attendee).",
)
@click.option(
    "--teams/--no-teams",
    "teams",
    default=None,
    help="Attach (--teams) or remove (--no-teams) the online meeting; omit to leave it.",
)
@click.option("--yes", "yes", is_flag=True, help="Confirm notify-others action.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def calendar_update_cmd(
    ctx: click.Context,
    event_id: str,
    subject: str | None,
    start_raw: str | None,
    end_raw: str | None,
    duration: str | None,
    all_day: bool | None,
    location: str | None,
    calendar: str | None,
    body: str | None,
    body_file: str | None,
    body_type: str,
    with_emails: tuple[str, ...],
    teams: bool | None,
    yes: bool,
    tz_flag: str | None,
    as_json_flag: bool,
) -> None:
    """Edit an existing event's fields. Only the flags you pass are changed. Requires --yes.

    Uses Calendars.ReadWrite. Editing a recurring series edits the whole series.
    """
    _dispatch(
        ctx,
        "calendar.update",
        {
            "event_id": event_id,
            "subject": subject,
            "start": start_raw,
            "end": end_raw,
            "duration": duration,
            "all_day": all_day,
            "location": location,
            "calendar": calendar,
            "body": body,
            "body_file": body_file,
            "body_type": body_type,
            "with": list(with_emails) if with_emails else None,
            "no_teams": None if teams is None else not teams,
            "tz": _tz_name(ctx, tz_flag),
            "yes": yes,
        },
        human=format_update_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "chat.attachments",
        {"chat_id": chat_id, "latest": latest, "message_id": message_id, "with": with_name},
        human=format_chat_attachments_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "chat.attachments.download",
        {
            "attachment_id": attachment_id,
            "chat_id": chat_id,
            "all": download_all,
            "latest": latest,
            "message_id": message_id,
            "out": out,
            "with": with_name,
        },
        human=format_chat_attachments_download_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "chat.delete",
        {"chat_id": chat_id, "message_id": message_id, "yes": yes},
        human=format_chat_delete_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "chat.edit",
        {"chat_id": chat_id, "message_id": message_id, "text": text, "yes": yes},
        human=format_edit_human,
        as_json_flag=as_json_flag,
    )


@chat.command("find", epilog=help_text.CHAT_FIND_EPILOG)
@click.option("--with", "with_name", required=True, help="Display-name substring to match members.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def chat_find_cmd(ctx: click.Context, with_name: str, as_json_flag: bool) -> None:
    """List chats whose members match a display-name substring.

    Use it to get a --chat-id when a name matches more than one chat.
    """
    _dispatch(
        ctx, "chat.find", {"with": with_name}, human=format_find_human, as_json_flag=as_json_flag
    )


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
    _dispatch_soft(
        ctx,
        "chat.last",
        {"with": with_name, "chat_id": chat_id, "contains": contains, "n": n},
        human=format_last_human,
        as_json_flag=as_json_flag,
        is_failure=lambda payload: payload.get("chat") is None,
        fail_exit=EXIT_NOT_FOUND,
    )


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
    _dispatch(
        ctx,
        "chat.send",
        {"with": with_name, "chat_id": chat_id, "text": text, "yes": yes},
        human=format_send_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "mail.inbox",
        {
            "from": sender,
            "subject": subject,
            "search": search,
            "since": since,
            "until": until,
            "unread": unread,
            "importance": importance,
            "has_attachments": has_attachments,
            "top": top,
            "tz": _tz_name(ctx, tz_flag),
        },
        human=format_inbox_human,
        as_json_flag=as_json_flag,
    )


@mail.command("folders", epilog=help_text.MAIL_FOLDERS_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_folders_cmd(ctx: click.Context, as_json_flag: bool) -> None:
    """List mail folders with their ids and message counts.

    Graph's totals can lag - do not treat `total: 0` as proof a folder is
    empty; confirm with `mail list --folder <name>`.
    """
    _dispatch(ctx, "mail.folders", {}, human=format_folders_human, as_json_flag=as_json_flag)


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
    _dispatch(
        ctx,
        "mail.get",
        {"id": message_id, "body_type": body_type},
        human=format_mail_get_human,
        as_json_flag=as_json_flag,
    )


@mail.command("search", epilog=help_text.MAIL_SEARCH_EPILOG)
@click.option("--query", "query", required=True, help="Search term (Graph $search / Gmail q=).")
@click.option("--since", default=None, help="Only messages at or after this local date/time.")
@click.option("--until", default=None, help="Only messages strictly before this local date/time.")
@click.option("--top", default=25, show_default=True, type=int, help="Max messages to return.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.option("--tz", "tz_flag", default=None, help="IANA timezone (default from config).")
@click.pass_context
def mail_search_cmd(
    ctx: click.Context,
    query: str,
    since: str | None,
    until: str | None,
    top: int,
    as_json_flag: bool,
    tz_flag: str | None,
) -> None:
    """Search the whole mailbox (every folder), relevance-ranked.

    `mail list --search` only covers one folder; this covers all of them and
    tags each hit with its `folder`. `--since` / `--until` filter the returned
    page locally ($search cannot combine with a server-side date filter).
    """
    _dispatch(
        ctx,
        "mail.search",
        {"query": query, "since": since, "until": until, "top": top, "tz": _tz_name(ctx, tz_flag)},
        human=format_mail_search_human,
        as_json_flag=as_json_flag,
    )


@mail.command("thread", epilog=help_text.MAIL_THREAD_EPILOG)
@click.option("--id", "message_id", required=True, help="Any message id in the conversation.")
@click.option("--full", "full", is_flag=True, help="Include each message's body.")
@click.option(
    "--body-type",
    "body_type",
    default="text",
    show_default=True,
    type=click.Choice(["html", "text"]),
    help="Body format when --full (Microsoft converts server-side).",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_thread_cmd(
    ctx: click.Context,
    message_id: str,
    full: bool,
    body_type: str,
    as_json_flag: bool,
) -> None:
    """List every message in the conversation a message belongs to, oldest first."""
    _dispatch(
        ctx,
        "mail.thread",
        {"id": message_id, "full": full, "body_type": body_type},
        human=format_mail_thread_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "mail.list",
        {
            "folder": folder,
            "orderby": orderby,
            "from": sender,
            "subject": subject,
            "search": search,
            "since": since,
            "until": until,
            "unread": unread,
            "importance": importance,
            "has_attachments": has_attachments,
            "top": top,
            "tz": _tz_name(ctx, tz_flag),
        },
        human=format_list_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "mail.attachments",
        {"id": message_id},
        human=format_attachments_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "mail.attachments.download",
        {
            "message_id": message_id,
            "attachment_id": attachment_id,
            "all": download_all,
            "out": out,
        },
        human=format_attachments_download_human,
        as_json_flag=as_json_flag,
    )


def _run_mail_triage(
    ctx: click.Context, *, verb: str, yes: bool, as_json_flag: bool, arguments: dict[str, Any]
) -> None:
    _dispatch(
        ctx,
        f"mail.{verb}",
        {**arguments, "yes": yes},
        human=format_mail_triage_human,
        as_json_flag=as_json_flag,
    )


@mail.command("delete", epilog=help_text.MAIL_TRIAGE_EPILOG)
@click.option("--id", "message_ids", multiple=True, required=True, help="Message id (repeatable).")
@click.option("--yes", "yes", is_flag=True, help="Confirm this mailbox change.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_delete_cmd(
    ctx: click.Context, message_ids: tuple[str, ...], yes: bool, as_json_flag: bool
) -> None:
    """Move one or more messages to Deleted Items / Trash (recoverable). Requires --yes."""
    _run_mail_triage(
        ctx, verb="delete", yes=yes, as_json_flag=as_json_flag, arguments={"id": list(message_ids)}
    )


@mail.command("mark", epilog=help_text.MAIL_TRIAGE_EPILOG)
@click.option("--id", "message_ids", multiple=True, required=True, help="Message id (repeatable).")
@click.option("--read/--unread", "read", default=None, help="Mark read or unread.")
@click.option("--flag/--unflag", "flagged", default=None, help="Set or clear the follow-up flag.")
@click.option(
    "--importance",
    default=None,
    type=click.Choice(MAIL_IMPORTANCE_VALUES, case_sensitive=False),
    help="Set importance.",
)
@click.option("--yes", "yes", is_flag=True, help="Confirm this mailbox change.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_mark_cmd(
    ctx: click.Context,
    message_ids: tuple[str, ...],
    read: bool | None,
    flagged: bool | None,
    importance: str | None,
    yes: bool,
    as_json_flag: bool,
) -> None:
    """Set read/unread, the follow-up flag, and/or importance. Requires --yes."""
    _run_mail_triage(
        ctx,
        verb="mark",
        yes=yes,
        as_json_flag=as_json_flag,
        arguments={
            "id": list(message_ids),
            "read": read,
            "flag": flagged,
            "importance": importance,
        },
    )


@mail.command("move", epilog=help_text.MAIL_TRIAGE_EPILOG)
@click.option("--id", "message_ids", multiple=True, required=True, help="Message id (repeatable).")
@click.option(
    "--to",
    "to",
    required=True,
    help="Destination: a well-known name (archive, deleteditems, …), folder id, or Gmail label.",
)
@click.option("--yes", "yes", is_flag=True, help="Confirm this mailbox change.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_move_cmd(
    ctx: click.Context, message_ids: tuple[str, ...], to: str, yes: bool, as_json_flag: bool
) -> None:
    """Move one or more messages to a folder (or `archive`). Requires --yes."""
    _run_mail_triage(
        ctx,
        verb="move",
        yes=yes,
        as_json_flag=as_json_flag,
        arguments={"id": list(message_ids), "to": to},
    )


@mail.command("auto-reply", epilog=help_text.MAIL_AUTO_REPLY_EPILOG)
@click.option(
    "--on/--off",
    "enable",
    default=None,
    help="Turn the auto-reply on or off. Omit both to read the current setting.",
)
@click.option("--message", "message", default=None, help="Reply body text.")
@click.option(
    "--message-file",
    "message_file",
    default=None,
    help="Read the reply body from a file (mutually exclusive with --message).",
)
@click.option(
    "--external-message",
    "external_message",
    default=None,
    help="Separate body for senders outside your org (Microsoft only).",
)
@click.option(
    "--external",
    "external_audience",
    default=None,
    type=click.Choice(["none", "contacts", "all"], case_sensitive=False),
    help="Who outside your org gets a reply (default: all).",
)
@click.option(
    "--start",
    "start",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Schedule start date (YYYY-MM-DD).",
)
@click.option(
    "--until",
    "until",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Schedule end date (YYYY-MM-DD).",
)
@click.option("--yes", "yes", is_flag=True, help="Confirm the change (required with --on/--off).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_auto_reply_cmd(
    ctx: click.Context,
    enable: bool | None,
    message: str | None,
    message_file: str | None,
    external_message: str | None,
    external_audience: str | None,
    start: Any,
    until: Any,
    yes: bool,
    as_json_flag: bool,
) -> None:
    """Read, set, or clear the automatic-reply / vacation responder (out-of-office).

    With no flags this is a read. `--on` needs `--message` or `--message-file`
    and `--yes`; `--off` clears it and needs `--yes`. `--start` / `--until`
    schedule a window. Microsoft needs `wo1162425_scopes` (MailboxSettings.ReadWrite);
    Google needs the `gmail.settings.basic` scope.
    """
    _dispatch(
        ctx,
        "mail.auto-reply",
        {
            "on": enable is True,
            "off": enable is False,
            "message": message,
            "message_file": message_file,
            "external_message": external_message,
            "external": external_audience,
            "start": start,
            "until": until,
            "yes": yes,
        },
        human=format_mail_auto_reply_human,
        as_json_flag=as_json_flag,
    )


mail.add_command(mail_auto_reply_cmd, "oof")


@mail.command("delete-draft", epilog=help_text.MAIL_DELETE_DRAFT_EPILOG)
@click.option("--id", "draft_id", required=True, help="Draft message id.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mail_delete_draft_cmd(ctx: click.Context, draft_id: str, as_json_flag: bool) -> None:
    """Permanently delete a draft. No --yes needed - nobody is notified.

    The safe way to clean up a draft you created only to inspect it.
    """
    _dispatch(
        ctx,
        "mail.delete-draft",
        {"id": draft_id},
        human=format_delete_draft_human,
        as_json_flag=as_json_flag,
    )


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
    default=None,
    type=click.Choice(["markdown", "text", "html"], case_sensitive=False),
    help=(
        "How --body is authored; markdown renders to HTML on the wire. "
        "Default: markdown, or text when config sets preferences.html_email = false."
    ),
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
    body_type: str | None,
    no_signature: bool,
    as_json_flag: bool,
) -> None:
    """Create a new mail draft. Does not send.

    Send it later with `mail send-draft --id ... --yes`. `--to` / `--cc` /
    `--bcc` repeat or take comma-separated lists. Use ASCII hyphens in the body,
    not em dashes.
    """
    _dispatch(
        ctx,
        "mail.draft",
        {
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "attach": attach,
            "body": body,
            "body_file": body_file,
            "body_type": body_type,
            "no_signature": no_signature,
        },
        human=format_draft_human,
        as_json_flag=as_json_flag,
    )


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
@click.option(
    "--body-type",
    default=None,
    type=click.Choice(["markdown", "text", "html"], case_sensitive=False),
    help=(
        "How --body is authored; markdown renders to HTML on the wire. "
        "Default: markdown, or text when config sets preferences.html_email = false."
    ),
)
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
    body_type: str | None,
    no_signature: bool,
    as_json_flag: bool,
) -> None:
    """Create a forward draft for a message. Does not send.

    Pass `--body` on create; filling it in later with `mail update-draft --body`
    replaces the quoted original. `--cc` / `--bcc` on create merge with
    inherited recipients. Send with `mail send-draft --yes`.
    """
    _dispatch(
        ctx,
        "mail.forward",
        {
            "id": message_id,
            "to": to,
            "cc": cc or None,
            "bcc": bcc or None,
            "body": body,
            "body_file": body_file,
            "body_type": body_type,
            "no_signature": no_signature,
        },
        human=format_reply_human,
        as_json_flag=as_json_flag,
    )


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
@click.option(
    "--body-type",
    default=None,
    type=click.Choice(["markdown", "text", "html"], case_sensitive=False),
    help=(
        "How --body is authored; markdown renders to HTML on the wire. "
        "Default: markdown, or text when config sets preferences.html_email = false."
    ),
)
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
    body_type: str | None,
    no_signature: bool,
    as_json_flag: bool,
) -> None:
    """Create a reply draft that threads in the recipient's client. Does not send.

    Prefer this over a fresh draft with "RE:" - Graph keeps it in the original
    conversation and inherits recipients. Pass `--body` on create; a later
    `mail update-draft --body` drops the quoted original. Send with
    `mail send-draft --yes`.
    """
    _dispatch(
        ctx,
        "mail.reply",
        {
            "id": message_id,
            "all": reply_all,
            "cc": cc or None,
            "bcc": bcc or None,
            "body": body,
            "body_file": body_file,
            "body_type": body_type,
            "no_signature": no_signature,
        },
        human=format_reply_human,
        as_json_flag=as_json_flag,
    )


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

    If `auth login` / `doctor` detected that your mail client (Outlook) already
    auto-inserts a signature, blumkin stops appending [mail.signature] to drafts
    to avoid a double signature; this command reports that as `suppressed`.
    """
    as_json = _as_json(ctx, as_json_flag)
    cfg = _load_config()
    try:
        rendered = render_mail_signature(cfg.mail_signature, body_type=body_type)
    except ValueError as exc:
        _emit_error(error="usage_error", message=str(exc), as_json=as_json)
        raise SystemExit(EXIT_USAGE) from exc
    state = load_signature_state(cfg)
    suppressed = state.suppresses_signature and cfg.mail_signature.enabled
    if as_json:
        emit_json(
            {
                "ok": True,
                "body_type": body_type.lower(),
                "enabled": cfg.mail_signature.enabled,
                "outlook_signature_detected": state.detected,
                "signature": rendered,
                "suppressed": suppressed,
            }
        )
    else:
        emit_lines([rendered] if rendered else ["(no signature configured)"])
        if suppressed:
            emit_lines(
                [
                    "note: not appended to drafts - your mail client auto-inserts "
                    "its own signature (run `blumkin doctor` to re-check)"
                ]
            )
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
    _dispatch(
        ctx,
        "mail.send-draft",
        {"id": draft_id, "yes": yes},
        human=format_send_draft_human,
        as_json_flag=as_json_flag,
    )


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
    default=None,
    type=click.Choice(["markdown", "text", "html"], case_sensitive=False),
    help=(
        "How --body is authored; markdown renders to HTML on the wire. "
        "Default: markdown, or text when config sets preferences.html_email = false."
    ),
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
    body_type: str | None,
    keep_quoted: bool,
    no_signature: bool,
    as_json_flag: bool,
) -> None:
    """Patch an existing draft in place. Does not send.

    `--to` / `--cc` / `--bcc` and `--body` each REPLACE that field wholesale
    when given - include every value that should remain. `--attach` is additive.
    """
    _dispatch(
        ctx,
        "mail.update-draft",
        {
            "id": draft_id,
            "attach": attach,
            "subject": subject,
            "to": to or None,
            "cc": cc or None,
            "bcc": bcc or None,
            "body": body,
            "body_file": body_file,
            "body_type": body_type,
            "keep_quoted": keep_quoted,
            "no_signature": no_signature,
        },
        human=format_draft_human,
        as_json_flag=as_json_flag,
    )


@main.group("mcp", epilog=help_text.MCP_EPILOG)
def mcp_group() -> None:
    """Run and register blumkin as a Model Context Protocol server.

    `install` registers `blumkin mcp serve` with your agent CLIs (Claude Code,
    Cursor, GitHub Copilot CLI), `status` shows where it is registered, and
    `serve` is the stdio server itself. Every skill becomes a typed MCP tool
    dispatched through the same `run_skill` path the CLI uses. Running the server
    needs the optional `mcp` extra: `pipx install 'blumkin[mcp]'`.
    """


@mcp_group.command("serve", epilog=help_text.MCP_SERVE_EPILOG)
@click.option("--profile", "profile", default=None, help="Profile name or tag to act as.")
@click.option("--read-only", "read_only", is_flag=True, help="Expose only non-mutating skills.")
@click.option(
    "--only",
    "only",
    multiple=True,
    help="Expose only skills under this id prefix (repeatable, e.g. --only calendar).",
)
@click.pass_context
def mcp_serve_cmd(
    ctx: click.Context, profile: str | None, read_only: bool, only: tuple[str, ...]
) -> None:
    """Start the stdio MCP server. Blocks until the client disconnects."""
    try:
        from blumkin import mcp_server
    except ModuleNotFoundError as exc:
        # The wrapper in blumkin.mcp_server re-raises with name=None; a genuinely
        # missing non-`mcp` module is a real bug, not a missing optional extra.
        if exc.name is not None and exc.name != "mcp" and not exc.name.startswith("mcp."):
            raise
        _emit_error(
            error="usage_error",
            message="the MCP server needs the optional `mcp` dependency",
            as_json=_cli_as_json(),
            hint="Install it with `pipx install 'blumkin[mcp]'` "
            "(or `uv tool install 'blumkin[mcp]'`), then retry.",
        )
        raise SystemExit(EXIT_USAGE) from exc
    # A command-level --profile wins; otherwise honour the global `blumkin --profile`.
    if profile is None and isinstance(ctx.obj, dict):
        raw = ctx.obj.get("profile")
        if isinstance(raw, str) and raw.strip():
            profile = raw.strip()
    mcp_server.serve(profile=profile, read_only=read_only, only=tuple(only))


def _stdio_is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@mcp_group.command("install", epilog=help_text.MCP_INSTALL_EPILOG)
@click.option(
    "--client",
    "clients",
    multiple=True,
    type=click.Choice(["claude", "cursor", "copilot"]),
    help="Limit to these clients (repeatable). Default: every one detected.",
)
@click.option(
    "--scope",
    type=click.Choice(["user", "project"]),
    default=None,
    help="user = every repo (~/.claude.json, ~/.copilot, ~/.cursor); "
    "project = this directory (.mcp.json, .cursor/mcp.json). Prompted if omitted.",
)
@click.option("--profile", "profile", default=None, help="Bake `--profile NAME` into the server.")
@click.option(
    "--read-only", "read_only", is_flag=True, help="Register a server with no mutating tools."
)
@click.option("--only", "only", multiple=True, help="Register `--only PREFIX` (repeatable).")
@click.option(
    "--yes", "yes", is_flag=True, help="Apply without the per-client prompt (needs --scope)."
)
@click.option(
    "--force", "force", is_flag=True, help="Re-write an entry even if it already matches."
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mcp_install_cmd(
    ctx: click.Context,
    clients: tuple[str, ...],
    scope: str | None,
    profile: str | None,
    read_only: bool,
    only: tuple[str, ...],
    yes: bool,
    force: bool,
    as_json_flag: bool,
) -> None:
    """Register `blumkin mcp serve` with your agent CLIs, confirming each one.

    Detects Claude Code, Cursor, and GitHub Copilot CLI and adds blumkin to each
    - via its own `mcp add` where it has one, else a direct config merge. Safe to
    re-run: an entry that already matches is left alone, a stale one is updated.
    """
    from blumkin import mcp_install as mi

    as_json = _as_json(ctx, as_json_flag)
    tty = _stdio_is_tty()
    interactive = tty and not yes

    # Without a TTY there is no per-client prompt, so writing to every client's
    # config must be an explicit opt-in - not the default of a redirected run.
    if not tty and not yes:
        _emit_error(
            error="usage_error",
            message="`mcp install` needs a TTY for the per-client prompt; pass --yes to skip it",
            as_json=as_json,
            hint="Non-interactive: `blumkin mcp install --yes --scope user` "
            "(also --client / --read-only / ...).",
        )
        raise SystemExit(EXIT_USAGE)

    targets = list(clients) or sorted(mi.detect())
    if not targets:
        _emit_error(
            error="usage_error",
            message="no supported agent CLI found (Claude Code, Cursor, GitHub Copilot CLI)",
            as_json=as_json,
            hint="Install one, or pass --client NAME to target it once its config dir exists.",
        )
        raise SystemExit(EXIT_USAGE)

    if scope is None:
        if not interactive:
            _emit_error(
                error="usage_error",
                message="--scope is required with --yes",
                as_json=as_json,
                hint="Pass --scope user or --scope project.",
            )
            raise SystemExit(EXIT_USAGE)
        scope = click.prompt(
            "Scope (user = every repo, project = this directory)",
            type=click.Choice(["user", "project"]),
            default="user",
        )

    if profile is None and isinstance(ctx.obj, dict):
        raw = ctx.obj.get("profile")
        if isinstance(raw, str) and raw.strip():
            profile = raw.strip()

    if interactive and not (read_only or only or profile):
        read_only = click.confirm("Restrict to read-only (non-mutating) tools?", default=False)
        raw_only = click.prompt(
            "Limit to skill families (space-separated prefixes, blank = all)",
            default="",
            show_default=False,
        ).strip()
        only = tuple(raw_only.split()) if raw_only else ()

    serve = mi.ServeSpec(profile=profile, read_only=read_only, only=tuple(only))
    binary, on_path = mi.resolve_binary()
    if not on_path and not as_json:
        click.echo(f"note: `blumkin` is not on PATH - registering {binary}", err=True)
    scope_val: mi.Scope = "project" if scope == "project" else "user"
    plans = mi.build_plan(
        clients=targets, scope=scope_val, binary=binary, serve=serve, cwd=Path.cwd()
    )

    results: list[dict[str, Any]] = []
    any_failed = False
    for plan in plans:
        should = plan.action != "unchanged" or force
        if should and interactive:
            verb = {"add": "Add", "update": "Update"}.get(plan.action, "Re-write")
            suffix = "" if plan.detected else " [not detected on this machine]"
            should = click.confirm(
                f"{verb} blumkin for {plan.label} -> {plan.target}{suffix}",
                default=plan.detected,
            )
        if not should:
            outcome = "unchanged" if plan.action == "unchanged" else "skipped"
            detail = ""
        else:
            try:
                outcome = mi.apply_plan(plan, binary=binary, force=force)
                detail = ""
            except mi.McpInstallError as exc:
                any_failed = True
                outcome = "failed"
                detail = str(exc)
                if not as_json:
                    click.echo(f"  {plan.label}: {exc}", err=True)
                    if exc.hint:
                        click.echo(f"    hint: {exc.hint}", err=True)
        results.append(
            {
                "client": plan.client,
                "label": plan.label,
                "scope": plan.scope,
                "detected": plan.detected,
                "target": plan.target,
                "action": outcome,
                "detail": detail,
            }
        )

    payload = {
        "ok": not any_failed,
        "binary": binary,
        "on_path": on_path,
        "scope": scope_val,
        "serve": ["blumkin", *serve.args()],
        "clients": results,
    }
    if as_json:
        emit_json(payload)
    else:
        emit_lines(_format_mcp_install_human(payload))
    raise SystemExit(EXIT_OTHER if any_failed else EXIT_SUCCESS)


@mcp_group.command("status", epilog=help_text.MCP_STATUS_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def mcp_status_cmd(ctx: click.Context, as_json_flag: bool) -> None:
    """Show which agent CLIs have `blumkin mcp serve` registered, and with what command."""
    from blumkin import mcp_install as mi

    as_json = _as_json(ctx, as_json_flag)
    binary, on_path = mi.resolve_binary()
    detected = mi.detect()
    registrations: list[dict[str, Any]] = []
    scopes: tuple[mi.Scope, ...] = ("user", "project")
    for client in mi.CLIENTS:
        for scope in scopes:
            current = mi.current_entry(client, scope, Path.cwd())
            if current is None:
                continue
            command = str(current.get("command", ""))
            registrations.append(
                {
                    "client": client,
                    "label": mi._LABELS[client],
                    "scope": scope,
                    "config": str(mi.config_path(client, scope, Path.cwd())),
                    "command": command,
                    "args": list(current.get("args") or []),
                    "resolves_to_blumkin": mi.command_is_current(command, binary),
                }
            )
    payload = {
        "ok": True,
        "binary": binary,
        "on_path": on_path,
        "detected": sorted(detected),
        "registrations": registrations,
    }
    if as_json:
        emit_json(payload)
    else:
        emit_lines(_format_mcp_status_human(payload))
    raise SystemExit(EXIT_SUCCESS)


def _format_mcp_install_human(payload: dict[str, Any]) -> list[str]:
    lines = [f"server: {' '.join(payload['serve'])}  (scope: {payload['scope']})"]
    for row in payload["clients"]:
        mark = {
            "added": "added",
            "updated": "updated",
            "unchanged": "already current",
            "skipped": "skipped",
            "failed": "FAILED",
        }.get(row["action"], row["action"])
        lines.append(f"  {row['label']}: {mark} -> {row['target']}")
    if not payload["ok"]:
        lines.append("one or more clients failed - see the errors above")
    return lines


def _format_mcp_status_human(payload: dict[str, Any]) -> list[str]:
    detected = ", ".join(payload["detected"]) or "none"
    lines = [f"detected clients: {detected}", f"blumkin binary: {payload['binary']}"]
    if not payload["registrations"]:
        lines.append("no client has blumkin registered - run `blumkin mcp install`")
        return lines
    for row in payload["registrations"]:
        cmd = " ".join([row["command"], *row["args"]])
        stale = "" if row["resolves_to_blumkin"] else "  (command does not resolve to this blumkin)"
        lines.append(f"  {row['label']} ({row['scope']}): {cmd}{stale}")
    return lines


@main.group(epilog=help_text.DOCS_EPILOG)
def docs() -> None:
    """Author a document and store it in your drive.

    One authoring format across providers - a Markdown subset. The backend is a
    native Google Doc (`provider = "google"`) or a `.docx` uploaded to OneDrive
    (`provider = "microsoft"`).
    """


@docs.command("create", epilog=help_text.DOCS_CREATE_EPILOG)
@click.option("--title", required=True, help="Document title / file name.")
@click.option(
    "--body", default=None, help="Authored content (mutually exclusive with --body-file)."
)
@click.option(
    "--body-file",
    "body_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="Read the content from a UTF-8 file (under 1 MB).",
)
@click.option(
    "--format",
    "body_format",
    default="markdown",
    show_default=True,
    type=click.Choice(["markdown", "text"], case_sensitive=False),
    help="How --body is parsed.",
)
@click.option(
    "--folder",
    default=None,
    help="Destination folder path - existing or created (mkdir -p); drive root if omitted.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def docs_create_cmd(
    ctx: click.Context,
    title: str,
    body: str | None,
    body_file: str | None,
    body_format: str,
    folder: str | None,
    as_json_flag: bool,
) -> None:
    """Create a document from a Markdown (or plain-text) body.

    Does not notify anyone, so no `--yes`. Pass exactly one of `--body` or
    `--body-file`. Use ASCII hyphens in the body, not em dashes.
    """
    _dispatch(
        ctx,
        "docs.create",
        {
            "title": title,
            "body": body,
            "body_file": body_file,
            "format": body_format,
            "folder": folder,
        },
        human=format_docs_create_human,
        as_json_flag=as_json_flag,
    )


@docs.command("update", epilog=help_text.DOCS_UPDATE_EPILOG)
@click.option("--id", "document_id", required=True, help="Id of a document blumkin created.")
@click.option("--title", default=None, help="New file name (leave off to keep the current one).")
@click.option(
    "--body",
    default=None,
    help="New content; replaces the whole body (mutually exclusive with --body-file).",
)
@click.option(
    "--body-file",
    "body_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="Read the new content from a UTF-8 file (under 1 MB).",
)
@click.option(
    "--format",
    "body_format",
    default="markdown",
    show_default=True,
    type=click.Choice(["markdown", "text"], case_sensitive=False),
    help="How --body is parsed.",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def docs_update_cmd(
    ctx: click.Context,
    document_id: str,
    title: str | None,
    body: str | None,
    body_file: str | None,
    body_format: str,
    as_json_flag: bool,
) -> None:
    """Re-render an existing blumkin-created document in place.

    Pass at least one of `--title`, `--body`, or `--body-file`. `--body` replaces
    the entire body - any manual edits made in the document since it was created
    are overwritten. Keeps the id, URL, and sharing. Does not notify anyone, so no
    `--yes`. Use ASCII hyphens in the body, not em dashes.
    """
    _dispatch(
        ctx,
        "docs.update",
        {
            "id": document_id,
            "title": title,
            "body": body,
            "body_file": body_file,
            "format": body_format,
        },
        human=format_docs_update_human,
        as_json_flag=as_json_flag,
    )


@main.group(epilog=help_text.DRIVE_EPILOG)
def drive() -> None:
    """Read and organize your drive (Google Drive / OneDrive).

    Read: `list`, `get`, `download`, `export`, `read`. Organize: `mkdir`,
    `move`, `rename` (each needs `--yes`). On `provider = "microsoft"` these
    need `docs_scopes = true` (Files.ReadWrite - the same grant `docs create`
    uses). On `provider = "google"` the `drive` scope is requested at `blumkin
    auth login`; re-consent once after upgrading.
    """


@drive.command("list", epilog=help_text.DRIVE_LIST_EPILOG)
@click.option("--folder-id", "folder_id", default=None, help="Folder id to list (from a listing).")
@click.option(
    "--folder",
    default=None,
    help="Folder path. Native on Microsoft; best-effort on Google (prefer --folder-id).",
)
@click.option("--query", default=None, help="Name / full-text substring to search for.")
@click.option(
    "--order",
    default="modified",
    show_default=True,
    type=click.Choice(["modified", "name"], case_sensitive=False),
    help="Sort order.",
)
@click.option("--top", default=None, type=int, help="Max items (default 50; 0 = no cap).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_list_cmd(
    ctx: click.Context,
    folder_id: str | None,
    folder: str | None,
    query: str | None,
    order: str,
    top: int | None,
    as_json_flag: bool,
) -> None:
    """List a drive folder, or search it with --query."""
    _dispatch(
        ctx,
        "drive.list",
        {
            "folder_id": folder_id,
            "folder": folder,
            "query": query,
            "order": order,
            "top": top,
        },
        human=format_drive_list_human,
        as_json_flag=as_json_flag,
    )


@drive.command("get", epilog=help_text.DRIVE_GET_EPILOG)
@click.option("--id", "item_id", required=True, help="Drive item id (from a listing).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_get_cmd(ctx: click.Context, item_id: str, as_json_flag: bool) -> None:
    """Read one drive item's metadata."""
    _dispatch(
        ctx,
        "drive.get",
        {"id": item_id},
        human=format_drive_get_human,
        as_json_flag=as_json_flag,
    )


@drive.command("download", epilog=help_text.DRIVE_DOWNLOAD_EPILOG)
@click.option("--id", "item_id", required=True, help="Drive item id (from a listing).")
@click.option(
    "--out",
    required=True,
    type=click.Path(path_type=str),
    help="Destination file or directory (on the host running the skill).",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_download_cmd(ctx: click.Context, item_id: str, out: str, as_json_flag: bool) -> None:
    """Download a drive file's raw bytes. Google-native docs -> use `drive export`."""
    _dispatch(
        ctx,
        "drive.download",
        {"id": item_id, "out": out},
        human=format_drive_download_human,
        as_json_flag=as_json_flag,
    )


@drive.command("export", epilog=help_text.DRIVE_EXPORT_EPILOG)
@click.option("--id", "item_id", required=True, help="Drive item id (from a listing).")
@click.option(
    "--to",
    required=True,
    type=click.Path(path_type=str),
    help="Target file; the extension selects the format (pdf/txt/html/csv/docx/xlsx/pptx).",
)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_export_cmd(ctx: click.Context, item_id: str, to: str, as_json_flag: bool) -> None:
    """Export a native doc (Google Doc / Sheet / Slides, or an Office file) to a file.

    Microsoft honours `pdf` only.
    """
    _dispatch(
        ctx,
        "drive.export",
        {"id": item_id, "to": to},
        human=format_drive_export_human,
        as_json_flag=as_json_flag,
    )


@drive.command("read", epilog=help_text.DRIVE_READ_EPILOG)
@click.option("--id", "item_id", required=True, help="Google Doc id (from a listing).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_read_cmd(ctx: click.Context, item_id: str, as_json_flag: bool) -> None:
    """Read a Google Doc as flattened Markdown text. Google only."""
    _dispatch(
        ctx,
        "drive.read",
        {"id": item_id},
        human=format_drive_read_human,
        as_json_flag=as_json_flag,
    )


@drive.command("mkdir", epilog=help_text.DRIVE_MKDIR_EPILOG)
@click.option("--path", required=True, help="Folder path; missing parents are created.")
@click.option("--yes", "yes", is_flag=True, help="Required: this changes your drive.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_mkdir_cmd(ctx: click.Context, path: str, yes: bool, as_json_flag: bool) -> None:
    """Create a folder (mkdir -p). No-op if it already exists."""
    _dispatch(
        ctx,
        "drive.mkdir",
        {"path": path, "yes": yes},
        human=format_drive_mkdir_human,
        as_json_flag=as_json_flag,
    )


@drive.command("move", epilog=help_text.DRIVE_MOVE_EPILOG)
@click.option("--id", "item_id", required=True, help="Id of the file or folder to move.")
@click.option("--to", "to", default=None, help="Destination folder path (one of --to / --to-id).")
@click.option(
    "--to-id", "to_id", default=None, help="Destination folder id (one of --to / --to-id)."
)
@click.option(
    "--make-parents", "make_parents", is_flag=True, help="Create --to if it does not exist."
)
@click.option("--yes", "yes", is_flag=True, help="Required: this changes your drive.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_move_cmd(
    ctx: click.Context,
    item_id: str,
    to: str | None,
    to_id: str | None,
    make_parents: bool,
    yes: bool,
    as_json_flag: bool,
) -> None:
    """Reparent a file or folder. id / URL / sharing are unchanged."""
    _dispatch(
        ctx,
        "drive.move",
        {
            "id": item_id,
            "to": to,
            "to_id": to_id,
            "make_parents": make_parents,
            "yes": yes,
        },
        human=format_drive_move_human,
        as_json_flag=as_json_flag,
    )


@drive.command("rename", epilog=help_text.DRIVE_RENAME_EPILOG)
@click.option("--id", "item_id", required=True, help="Id of the file or folder to rename.")
@click.option("--name", required=True, help="New name.")
@click.option("--yes", "yes", is_flag=True, help="Required: this changes your drive.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def drive_rename_cmd(
    ctx: click.Context, item_id: str, name: str, yes: bool, as_json_flag: bool
) -> None:
    """Rename a file or folder in place."""
    _dispatch(
        ctx,
        "drive.rename",
        {"id": item_id, "name": name, "yes": yes},
        human=format_drive_rename_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "meeting.get",
        {"event_id": event_id},
        human=format_meeting_get_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch(
        ctx,
        "meeting.transcription",
        {"event_id": event_id, "enable": enable, "yes": yes},
        human=format_transcription_human,
        as_json_flag=as_json_flag,
    )


@main.group(epilog=help_text.PEOPLE_EPILOG)
def people() -> None:
    """Look up who you mean before you invite or message someone.

    `context` reads your local `email-context.md`; `resolve` searches the Graph
    directory (needs `wo1162425_scopes` + People.Read). Both fail closed - never
    guess when more than one person matches.
    """


@people.command("context", epilog=help_text.PEOPLE_CONTEXT_EPILOG)
@click.option("--name", "name", default=None, help="Filter to one contact by name or alias.")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def people_context_cmd(ctx: click.Context, name: str | None, as_json_flag: bool) -> None:
    """List the contacts in `~/.config/blumkin/email-context.md` (name, aliases,
    email, notes).

    Read-only, no network, no auth needed. Blumkin does not turn a name into an
    address itself - use this to look one up, then pass the real email to
    `mail draft` / `calendar create`. Empty output means no `email-context.md`
    (or nothing matched `--name`). The active profile's
    `profiles/<name>/email-context.md` is merged on top of the config-dir file.
    """
    _dispatch_local(
        ctx,
        "people.context",
        {"name": name},
        human=format_people_context_human,
        as_json_flag=as_json_flag,
    )


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
    _dispatch_soft(
        ctx,
        "people.resolve",
        {"name": name, "email": email, "top": top},
        human=format_resolve_human,
        as_json_flag=as_json_flag,
        is_failure=lambda payload: bool(payload.get("ambiguous")),
        fail_exit=EXIT_USAGE,
    )


@main.group(epilog=help_text.TASKS_EPILOG)
def tasks() -> None:
    """Named reusable prompt templates from `~/.config/blumkin/tasks/*.md`.

    Read-only, no network, no auth. blumkin surfaces the templates; you (or an
    agent) match the request to a `Trigger:` line, confirm the pick, and run the
    `Prompt:` block. blumkin ships no matcher and never calls a model.
    """


@tasks.command("list", epilog=help_text.TASKS_LIST_EPILOG)
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def tasks_list_cmd(ctx: click.Context, as_json_flag: bool) -> None:
    """List every template: name, title, trigger, input, output (not the prompt).

    Empty output means no `tasks/` directory. The active profile's
    `profiles/<name>/tasks/` is merged on top of the config-dir one; a template
    that differs between the two is flagged (`conflict: true`).
    """
    _dispatch_local(ctx, "tasks.list", {}, human=format_tasks_list_human, as_json_flag=as_json_flag)


@tasks.command("show", epilog=help_text.TASKS_SHOW_EPILOG)
@click.option("--name", "name", required=True, help="Template name (a unique prefix works).")
@click.option("--json", "as_json_flag", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def tasks_show_cmd(ctx: click.Context, name: str, as_json_flag: bool) -> None:
    """Show one template in full, including its `Prompt:` block.

    Unknown name exits 5 (not_found); an ambiguous prefix exits 2 (usage_error)
    with the candidates; a template that conflicts across the two dirs exits 2.
    """
    _dispatch_local(
        ctx,
        "tasks.show",
        {"name": name},
        human=format_tasks_show_human,
        as_json_flag=as_json_flag,
    )


if __name__ == "__main__":
    main(obj={})
