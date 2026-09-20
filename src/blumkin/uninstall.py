"""Planning and execution logic behind ``blumkin uninstall``.

Issue #344 splits teardown into independently confirmable categories: the
agent runtime, MCP registrations, the managed package, local config state,
and keyring items. The CLI layer owns prompting, `--dry-run`, and JSON/human
formatting; this module only computes what is present up front and executes one
category at a time, returning structured results the CLI can report and test
without a Click runner.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from blumkin.agent import client as agent_client
from blumkin.agent.paths import is_supported_platform, runtime_dir
from blumkin.config import BlumkinConfig, config_dir, list_profiles, profile_probe_config
from blumkin.install_method import Install, detect_install, uninstall_steps
from blumkin.mcp_install import (
    _LABELS,
    CLIENTS,
    McpInstallError,
    Scope,
    current_entry,
    remove_entry,
)
from blumkin.secret_store import (
    SecretKind,
    SecretWriteError,
    delete_keyring_entry,
    exists,
    ms_bundle_exists,
)

Category = Literal["agent", "mcp", "package", "config", "keyring"]
OutcomeName = Literal["failed", "not_present", "removed", "skipped", "would_remove"]

# Declaration order matches the issue's user-visible sequencing (1-5) - not
# alphabetical; this order is part of the CLI's contract.
CATEGORY_ORDER: tuple[Category, ...] = ("agent", "mcp", "package", "config", "keyring")


@dataclass(frozen=True, slots=True)
class Outcome:
    """Structured result for one uninstall target or category."""

    category: Category
    label: str
    outcome: OutcomeName
    detail: str
    client: str | None = None
    scope: Scope | None = None

    def as_dict(self) -> dict[str, str | None]:
        """JSON-ready representation for CLI output."""
        return {
            "category": self.category,
            "client": self.client,
            "detail": self.detail,
            "label": self.label,
            "outcome": self.outcome,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """Every target across every category, computed once up front.

    Profile configs are captured here (not re-read later) specifically so the
    keyring category - which runs last, after config deletion may already have
    removed ``config.toml`` - still knows which profiles existed and what each
    profile's ``token_storage`` preference was.
    """

    agent: Target
    mcp: tuple[Target, ...]
    package: Target
    config: Target
    keyring: Target
    _install: Install
    _profiles: tuple[tuple[str, BlumkinConfig], ...]


@dataclass(frozen=True, slots=True)
class Target:
    """One thing ``blumkin uninstall`` could remove, pending confirmation."""

    category: Category
    label: str
    present: bool
    detail: str
    client: str | None = None
    scope: Scope | None = None


def build_plan(*, cwd: Path | None = None) -> Plan:
    """Compute every uninstall target without deleting or deregistering anything."""
    worktree = Path.cwd() if cwd is None else cwd
    install = detect_install()
    profiles = tuple(
        (row["name"], profile_probe_config(str(row["name"]))) for row in list_profiles()
    )
    return Plan(
        agent=_build_agent_target(),
        config=_build_config_target(),
        keyring=_build_keyring_target(profiles),
        mcp=tuple(
            _build_mcp_target(client, scope, cwd=worktree)
            for client in CLIENTS
            for scope in ("user", "project")
        ),
        package=_build_package_target(install),
        _install=install,
        _profiles=profiles,
    )


def remove_agent() -> Outcome:
    """Stop a running agent if possible, then remove its runtime directory."""
    label = "Stop and remove blumkin-agent"
    if not is_supported_platform():
        return Outcome(
            category="agent",
            label=label,
            outcome="not_present",
            detail="blumkin-agent is not supported on this platform",
        )
    runtime = runtime_dir()
    try:
        agent_client.call("shutdown", spawn=False)
    except agent_client.AgentUnreachableError as exc:
        return Outcome(category="agent", label=label, outcome="failed", detail=str(exc))
    except agent_client.AgentUnavailableError:
        pass
    shutil.rmtree(runtime, ignore_errors=True)
    return Outcome(
        category="agent",
        label=label,
        outcome="removed",
        detail=f"removed runtime directory {runtime}",
    )


def remove_config() -> Outcome:
    """Delete blumkin's config directory, refusing to follow a symlink."""
    directory = config_dir()
    label = "Delete local state"
    if directory.is_symlink():
        return Outcome(
            category="config",
            label=label,
            outcome="failed",
            detail=f"{directory} is a symlink - refusing to remove it",
        )
    if not directory.exists():
        return Outcome(
            category="config",
            label=label,
            outcome="not_present",
            detail=f"{directory} is already absent",
        )
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        return Outcome(
            category="config",
            label=label,
            outcome="not_present",
            detail=f"{directory} is already absent",
        )
    except OSError as exc:
        return Outcome(category="config", label=label, outcome="failed", detail=str(exc))
    return Outcome(
        category="config",
        label=label,
        outcome="removed",
        detail=f"deleted {directory}",
    )


def remove_keyring(profiles: tuple[tuple[str, BlumkinConfig], ...]) -> Outcome:
    """Delete keyring entries for every captured profile, leaving files untouched."""
    label = "Delete blumkin-owned keyring items"
    before = {name: _profile_secret_presence(cfg) for name, cfg in profiles}
    touched = sorted(name for name, presence in before.items() if any(presence.values()))
    if not touched:
        return Outcome(
            category="keyring",
            label=label,
            outcome="not_present",
            detail="no keyring-backed secrets detected for configured profiles",
        )
    failures: list[str] = []
    for name, cfg in profiles:
        for kind in _SECRET_KINDS:
            try:
                delete_keyring_entry(cfg, kind)
            except SecretWriteError as exc:
                failures.append(f"{name}:{kind}: {exc}")
    if failures:
        return Outcome(
            category="keyring",
            label=label,
            outcome="failed",
            detail="; ".join(failures),
        )
    detail = "removed keyring entries for " + ", ".join(touched)
    return Outcome(category="keyring", label=label, outcome="removed", detail=detail)


def remove_mcp(client: str, scope: Scope, *, cwd: Path | None = None) -> Outcome:
    """Remove one MCP client/scope registration if present."""
    label = _mcp_label(client, scope)
    worktree = Path.cwd() if cwd is None else cwd
    try:
        status = remove_entry(client, scope, worktree)
    except McpInstallError as exc:
        return Outcome(
            category="mcp",
            client=client,
            detail=str(exc),
            label=label,
            outcome="failed",
            scope=scope,
        )
    if status == "absent":
        return Outcome(
            category="mcp",
            client=client,
            detail="no registration present",
            label=label,
            outcome="not_present",
            scope=scope,
        )
    return Outcome(
        category="mcp",
        client=client,
        detail="registration removed",
        label=label,
        outcome="removed",
        scope=scope,
    )


def remove_package(install: Install) -> Outcome:
    """Uninstall the managed blumkin package, when a package manager owns it."""
    label = "Uninstall the blumkin package"
    steps = uninstall_steps(install)
    if not steps:
        return Outcome(
            category="package",
            label=label,
            outcome="not_present",
            detail="no uv tool or pipx managed package install detected",
        )
    cmd = steps[0]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            text=True,
            timeout=_PACKAGE_UNINSTALL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return Outcome(
            category="package",
            label=label,
            outcome="failed",
            detail=f"`{' '.join(cmd)}` timed out after {_PACKAGE_UNINSTALL_TIMEOUT_S}s",
        )
    except OSError as exc:
        return Outcome(category="package", label=label, outcome="failed", detail=str(exc))
    if completed.returncode != 0:
        detail = _subprocess_detail(completed)
        return Outcome(category="package", label=label, outcome="failed", detail=detail)
    return Outcome(
        category="package",
        label=label,
        outcome="removed",
        detail=f"ran `{' '.join(cmd)}`",
    )


_PACKAGE_UNINSTALL_TIMEOUT_S = 60
_SECRET_KINDS: tuple[SecretKind, ...] = ("auth_record", "token_cache", "google_token")


def _build_agent_target() -> Target:
    label = "Stop and remove blumkin-agent"
    if not is_supported_platform():
        return Target(
            category="agent",
            label=label,
            present=False,
            detail="blumkin-agent is not supported on this platform",
        )
    runtime = _runtime_dir_path()
    try:
        agent_client.call("ping", spawn=False)
    except agent_client.AgentUnreachableError as exc:
        return Target(category="agent", label=label, present=True, detail=str(exc))
    except agent_client.AgentUnavailableError:
        present = runtime.exists() or (runtime / "agent.sock").exists()
        if present:
            detail = f"stale runtime data under {runtime}"
        else:
            detail = f"no running agent or runtime data under {runtime}"
    else:
        return Target(
            category="agent",
            label=label,
            present=True,
            detail=f"agent is reachable; will shut it down and remove {runtime}",
        )
    return Target(category="agent", label=label, present=present, detail=detail)


def _build_config_target() -> Target:
    directory = config_dir()
    present = directory.is_dir() or directory.is_symlink()
    detail = str(directory)
    if not present:
        detail = f"{directory} is already absent"
    return Target(category="config", label="Delete local state", present=present, detail=detail)


def _build_keyring_target(profiles: tuple[tuple[str, BlumkinConfig], ...]) -> Target:
    touched = sorted(name for name, cfg in profiles if any(_profile_secret_presence(cfg).values()))
    if touched:
        detail = "profiles: " + ", ".join(touched)
    else:
        detail = "no keyring-backed secrets detected for configured profiles"
    return Target(
        category="keyring",
        label="Delete blumkin-owned keyring items",
        present=bool(touched),
        detail=detail,
    )


def _build_mcp_target(client: str, scope: Scope, *, cwd: Path) -> Target:
    present = current_entry(client, scope, cwd) is not None
    detail = "registration present" if present else "no registration present"
    return Target(
        category="mcp",
        client=client,
        detail=detail,
        label=_mcp_label(client, scope),
        present=present,
        scope=scope,
    )


def _build_package_target(install: Install) -> Target:
    manager = install.manager
    if manager is None:
        detail = "no uv tool or pipx managed package install detected"
    else:
        detail = f"{manager}-managed install ({install.method})"
    return Target(
        category="package",
        label="Uninstall the blumkin package",
        present=manager is not None,
        detail=detail,
    )


def _mcp_label(client: str, scope: Scope) -> str:
    return f"Remove MCP registration for {_LABELS[client]} ({scope})"


def _profile_secret_presence(cfg: BlumkinConfig) -> dict[SecretKind, bool]:
    ms = ms_bundle_exists(cfg)
    return {
        "auth_record": ms["auth_record"],
        "google_token": exists(cfg, "google_token"),
        "token_cache": ms["token_cache"],
    }


def _runtime_dir_path() -> Path:
    base = Path(os.environ.get("BLUMKIN_AGENT_RUNTIME_DIR", tempfile.gettempdir()))
    return base / f"blumkin-agent-{os.getuid()}"


def _subprocess_detail(completed: subprocess.CompletedProcess[str]) -> str:
    output = (completed.stderr or completed.stdout or "").strip().splitlines()
    if not output:
        return f"`{' '.join(completed.args)}` exited {completed.returncode}"
    return output[-1]
