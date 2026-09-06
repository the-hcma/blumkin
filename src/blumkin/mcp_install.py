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


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text("utf-8"))
    except OSError, ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


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
    cur_cmd = str(current.get("command", ""))
    cmd_ok = cur_cmd == binary or Path(cur_cmd).name == Path(binary).name
    args_ok = list(current.get("args") or []) == list(desired["args"])
    return cmd_ok and args_ok


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
                config_path=config_path(client, scope, cwd) if via == "file" else None,
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


def _apply_cli(plan: ClientPlan, binary: str) -> str:
    if plan.client == "claude":
        _run(["claude", "mcp", "remove", "blumkin", "-s", plan.scope])
        add = ["claude", "mcp", "add", "blumkin", "-s", plan.scope, "--transport", "stdio"]
    else:  # copilot, user scope
        _run(["copilot", "mcp", "remove", "blumkin"])
        add = ["copilot", "mcp", "add", "blumkin", "--transport", "stdio", "--tools", "*"]
    proc = _run([*add, "--", binary, *plan.desired["args"]])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise McpInstallError(
            f"{plan.label}: `{plan.client} mcp add` failed"
            + (f" - {detail[-1]}" if detail else ""),
            hint=f"Run `{' '.join(add)} -- {binary} {' '.join(plan.desired['args'])}` to debug.",
        )
    return "added" if plan.action == "add" else "updated"


def _apply_file(plan: ClientPlan) -> str:
    path = plan.config_path
    assert path is not None  # via == "file" always carries a path
    data = _load_json(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers["blumkin"] = plan.desired
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    except OSError as exc:
        raise McpInstallError(
            f"{plan.label}: could not write {path}: {exc}",
            hint=f"Check that {path.parent} is writable and {path} is a regular file you own.",
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
