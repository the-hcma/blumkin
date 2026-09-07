"""Guided install of the blumkin MCP server into agent CLIs.

``blumkin mcp install`` detects Claude Code, Cursor, and GitHub Copilot CLI, then
registers ``blumkin mcp serve`` with each - through the client's own ``mcp add``
command where it has one (Claude, Copilot at user scope), or a direct merge into
its JSON config (Cursor, and Copilot at project scope).

Idempotent: an entry that already matches the requested shape is left untouched
(``unchanged``); one that differs is rewritten (``update``); a missing one is
created (``add``). Nothing here prompts - the CLI layer drives confirmation and
passes a ready plan to :func:`apply_plan`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from blumkin.version import running_command_path

Scope = Literal["user", "project"]
Action = Literal["add", "update", "unchanged"]

CLIENTS: tuple[str, ...] = ("claude", "cursor", "copilot")
_LABELS = {"claude": "Claude Code", "cursor": "Cursor", "copilot": "GitHub Copilot CLI"}
# Clients driven through their own `mcp add`/`mcp remove` CLI rather than a file merge.
_CLI_DRIVEN = {"claude", "copilot"}
_SUBPROCESS_TIMEOUT = 30


class McpInstallError(RuntimeError):
    """A client command or config write failed. Carries an operator-facing hint."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class ServeSpec:
    """The ``blumkin mcp serve`` flavour to register."""

    profile: str | None = None
    read_only: bool = False
    only: tuple[str, ...] = ()

    def args(self) -> list[str]:
        out = ["mcp", "serve"]
        if self.profile:
            out += ["--profile", self.profile]
        if self.read_only:
            out.append("--read-only")
        for prefix in self.only:
            out += ["--only", prefix]
        return out


@dataclass
class ClientPlan:
    client: str
    label: str
    scope: Scope
    detected: bool
    via: Literal["cli", "file"]
    config_path: Path | None
    current: dict[str, Any] | None
    desired: dict[str, Any]
    action: Action

    @property
    def target(self) -> str:
        """Where the change lands, for display."""
        if self.via == "cli":
            return f"{self.client} mcp add (scope: {self.scope})"
        return str(self.config_path)


# --------------------------------------------------------------------------- detection


def detect(clients: tuple[str, ...] = CLIENTS) -> set[str]:
    """Which of ``clients`` look installed on this machine."""
    found: set[str] = set()
    if shutil.which("claude"):
        found.add("claude")
    if shutil.which("cursor-agent") or shutil.which("cursor") or (Path.home() / ".cursor").is_dir():
        found.add("cursor")
    if shutil.which("copilot"):
        found.add("copilot")
    return {c for c in clients if c in found}


def _canonical(command: str) -> Path | None:
    """The real executable a registered ``command`` points at, or ``None``."""
    located = command if os.path.isabs(command) else shutil.which(command)
    if not located:
        return None
    try:
        return Path(located).resolve()
    except OSError:
        return None


def command_is_current(registered: str, binary: str) -> bool:
    """Does a registered ``command`` still resolve to *this* blumkin?

    Compares canonical executable paths, so a dead `/old/venv/bin/blumkin` (or a
    bare `blumkin` no longer on PATH) is reported stale even though the basename
    matches.
    """
    if registered == binary:
        return True
    reg, want = _canonical(registered), _canonical(binary)
    return reg is not None and reg == want


def resolve_binary() -> tuple[str, bool]:
    """``(command-to-register, is_on_PATH)``.

    Prefer the bare name ``blumkin`` so the entry keeps working after a
    reinstall; fall back to the absolute path of the running executable when
    ``blumkin`` is not on ``PATH`` (an editable/uv-run invocation).
    """
    if shutil.which("blumkin"):
        return "blumkin", True
    return str(running_command_path()), False


# --------------------------------------------------------------------------- planning


def config_path(client: str, scope: Scope, cwd: Path) -> Path | None:
    home = Path.home()
    project = scope == "project"
    if client == "claude":
        return (cwd / ".mcp.json") if project else (home / ".claude.json")
    if client == "copilot":
        return (cwd / ".mcp.json") if project else (home / ".copilot" / "mcp-config.json")
    if client == "cursor":
        return (cwd / ".cursor" / "mcp.json") if project else (home / ".cursor" / "mcp.json")
    return None


def _file_text(path: Path) -> str:
    """Best-effort UTF-8 read; an unreadable file counts as empty here."""
    try:
        return path.read_text("utf-8")
    except OSError:
        return ""


def _load_json(path: Path | None) -> dict[str, Any]:
    """Lenient read for planning: a missing *or* unreadable file is ``{}``.

    ``_read_config`` is the strict variant a merge uses - it refuses to proceed
    past a file it cannot parse rather than reporting it as absent.
    """
    if path is None or not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text("utf-8"))
    except OSError, ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text("utf-8")
        if not text.strip():
            # A 0-byte or whitespace-only file (some clients leave one behind as a
            # placeholder) has no content to clobber - treat it like an absent
            # file and merge into it, same as `_load_json` (issue #198).
            return {}
        loaded = json.loads(text)
    except (OSError, ValueError) as exc:
        raise McpInstallError(
            f"{path} is not valid JSON",
            hint="mcp install merges into this file and will not overwrite content it "
            "cannot parse. Fix or remove it, then retry.",
        ) from exc
    if not isinstance(loaded, dict):
        raise McpInstallError(
            f"{path} is not a JSON object",
            hint='Expected `{ "mcpServers": { ... } }`. Fix or remove it, then retry.',
        )
    return loaded


def current_entry(client: str, scope: Scope, cwd: Path) -> dict[str, Any] | None:
    servers = _load_json(config_path(client, scope, cwd)).get("mcpServers")
    if isinstance(servers, dict) and isinstance(servers.get("blumkin"), dict):
        return servers["blumkin"]
    return None


def desired_entry(client: str, binary: str, serve: ServeSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": binary, "args": serve.args()}
    if client == "copilot":
        return {"type": "stdio", **entry, "tools": ["*"]}
    return entry


def _matches(current: dict[str, Any], desired: dict[str, Any], binary: str) -> bool:
    # The command must be exactly what we would write - a bare `blumkin` entry
    # left behind after the binary moved (pipx -> uv-run) no longer resolves for
    # the host, so basename equality is not enough.
    if str(current.get("command", "")) != binary:
        return False
    if list(current.get("args") or []) != list(desired["args"]):
        return False
    # Other keys `desired` requires (Copilot's `type`) must match too; `tools`
    # may have been narrowed by the user, so it is not part of the comparison.
    for key, value in desired.items():
        if key not in ("command", "args", "tools") and current.get(key) != value:
            return False
    return True


def _via(client: str, scope: Scope) -> Literal["cli", "file"]:
    if client == "claude":
        return "cli"
    if client == "copilot":
        return "cli" if scope == "user" else "file"
    return "file"


def build_plan(
    *, clients: list[str], scope: Scope, binary: str, serve: ServeSpec, cwd: Path
) -> list[ClientPlan]:
    detected = detect()
    plans: list[ClientPlan] = []
    for client in clients:
        current = current_entry(client, scope, cwd)
        desired = desired_entry(client, binary, serve)
        if current is None:
            action: Action = "add"
        elif _matches(current, desired, binary):
            action = "unchanged"
        else:
            action = "update"
        via = _via(client, scope)
        plans.append(
            ClientPlan(
                client=client,
                label=_LABELS[client],
                scope=scope,
                detected=client in detected,
                via=via,
                config_path=config_path(client, scope, cwd),
                current=current,
                desired=desired,
                action=action,
            )
        )
    return plans


# --------------------------------------------------------------------------- apply


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise McpInstallError(
            f"could not run `{cmd[0]} {cmd[1]}`: {exc}",
            hint=f"Run `{' '.join(cmd)}` yourself to see the error.",
        ) from exc


def _last_line(proc: subprocess.CompletedProcess[str]) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else ""


def _client_add_argv(client: str, scope: Scope, command: str, args: list[str]) -> list[str]:
    if client == "claude":
        base = ["claude", "mcp", "add", "blumkin", "-s", scope, "--transport", "stdio"]
    else:  # copilot, user scope
        base = ["copilot", "mcp", "add", "blumkin", "--transport", "stdio", "--tools", "*"]
    return [*base, "--", command, *args]


def _restore_previous(plan: ClientPlan) -> bool:
    """Re-register the entry `plan.current`, preserving its keys. Returns success."""
    old = plan.current or {}
    if plan.client == "claude":  # add-json puts the exact object back (type / env / ...)
        proc = _run(["claude", "mcp", "add-json", "-s", plan.scope, "blumkin", json.dumps(old)])
    else:
        argv = ["copilot", "mcp", "add", "blumkin", "--transport", str(old.get("type") or "stdio")]
        if isinstance(old.get("tools"), list):
            argv += ["--tools", ",".join(str(t) for t in old["tools"])]
        for key, value in (old.get("env") or {}).items():
            argv += ["--env", f"{key}={value}"]
        argv += ["--", str(old.get("command", "")), *(str(a) for a in old.get("args") or [])]
        proc = _run(argv)
    return proc.returncode == 0


def _apply_cli(plan: ClientPlan, binary: str) -> str:
    # `claude mcp add -s project` rewrites cwd/.mcp.json - a freshly cloned repo
    # can ship that as a symlink, same as the file-merge path guards against.
    if plan.scope == "project" and plan.config_path is not None:
        _reject_symlinked_target(plan.config_path)
    remove = (
        ["claude", "mcp", "remove", "blumkin", "-s", plan.scope]
        if plan.client == "claude"
        else ["copilot", "mcp", "remove", "blumkin"]
    )
    replacing = plan.current is not None
    if replacing:
        # `mcp add` errors on a duplicate name, so a replace removes first. Bail if
        # that fails - the entry is still there and nothing has been lost.
        rm = _run(remove)
        if rm.returncode != 0:
            raise McpInstallError(
                f"{plan.label}: could not remove the existing entry to replace it"
                + (f" - {_last_line(rm)}" if _last_line(rm) else ""),
                hint=f"Run `{' '.join(remove)}` to see the error.",
            )
    add = _client_add_argv(plan.client, plan.scope, binary, plan.desired["args"])
    proc = _run(add)
    if proc.returncode != 0:
        message = f"{plan.label}: `{plan.client} mcp add` failed"
        if _last_line(proc):
            message += f" - {_last_line(proc)}"
        if replacing and not _restore_previous(plan):
            raise McpInstallError(
                message + "; the previous registration was removed and could NOT be restored",
                hint="Re-run `blumkin mcp install` to re-register blumkin with this client.",
            )
        raise McpInstallError(message, hint=f"Run `{' '.join(add)}` to see the error.")
    return "added" if plan.action == "add" else "updated"


def _reject_symlinked_target(path: Path) -> None:
    """Refuse ``path`` or any parent (down to the filesystem root) that is a
    symlink.

    Called for **project scope** only: a freshly cloned repo can ship ``.cursor``
    or ``.mcp.json`` as a link, and the write would then clobber the link's real
    target. A user-scope config the operator symlinked into their own dotfiles is
    their choice and left alone.
    """
    current = path
    while True:
        if current.is_symlink():
            raise McpInstallError(
                f"{current} is a symlink - refusing to write through it",
                hint="Remove the symlink (or install at a different scope), then retry.",
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _apply_file(plan: ClientPlan) -> str:
    path = plan.config_path
    assert path is not None  # config_path covers every client
    if plan.scope == "project":
        _reject_symlinked_target(path)
    # A user-scope config the operator symlinked into their dotfiles: honour the
    # link, write to its real target rather than replacing the symlink with a file.
    target = path.resolve() if plan.scope == "user" and path.is_symlink() else path
    data = _read_config(path)  # strict: refuses an existing file it cannot parse
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers["blumkin"] = plan.desired
    body = json.dumps(data, indent=2) + "\n"
    # Keep a locked-down existing file's mode, but an empty / whitespace-only
    # placeholder (which `_read_config` already treats as absent) has no mode
    # worth preserving - create it private, like a fresh file (issue #198 review).
    preserve_mode = target.is_file() and bool(_file_text(target).strip())
    tmp = target.with_name(f".{target.name}.blumkin-{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, "utf-8")
        # MCP configs can hold `env` credentials (ours and other servers'), so do
        # not widen a locked-down file, and create a new one private.
        if preserve_mode:
            shutil.copymode(target, tmp)
        else:
            tmp.chmod(0o600)
        os.replace(tmp, target)  # atomic: a failed write never truncates the real file
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise McpInstallError(
            f"{plan.label}: could not write {target}: {exc}",
            hint=f"Check that {target.parent} is writable and {target} is a regular file you own.",
        ) from exc
    if plan.client == "cursor" and shutil.which("cursor-agent"):
        _run(["cursor-agent", "mcp", "enable", "blumkin"])  # best effort; ignore result
    return "added" if plan.action == "add" else "updated"


def apply_plan(plan: ClientPlan, *, binary: str, force: bool = False) -> str:
    """Carry out one :class:`ClientPlan`. Returns the outcome word.

    ``force`` re-writes an entry that already matches. Raises
    :class:`McpInstallError` on a client-command or write failure.
    """
    if plan.action == "unchanged" and not force:
        return "unchanged"
    return _apply_cli(plan, binary) if plan.via == "cli" else _apply_file(plan)
