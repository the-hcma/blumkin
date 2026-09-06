"""Hermetic coverage for `blumkin mcp install` / `mcp status` (blumkin.mcp_install)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin import mcp_install as mi
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def all_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    binaries = {"claude", "cursor-agent", "copilot", "blumkin"}
    monkeypatch.setattr(
        mi.shutil, "which", lambda name: f"/usr/bin/{name}" if name in binaries else None
    )


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mi.subprocess, "run", _run)
    return calls


# --------------------------------------------------------------------------- module


def test_serve_spec_args() -> None:
    assert mi.ServeSpec().args() == ["mcp", "serve"]
    assert mi.ServeSpec(read_only=True).args() == ["mcp", "serve", "--read-only"]
    assert mi.ServeSpec(profile="work", only=("mail", "calendar")).args() == [
        "mcp",
        "serve",
        "--profile",
        "work",
        "--only",
        "mail",
        "--only",
        "calendar",
    ]


def test_desired_entry_copilot_carries_stdio_and_tools() -> None:
    assert mi.desired_entry("cursor", "blumkin", mi.ServeSpec()) == {
        "command": "blumkin",
        "args": ["mcp", "serve"],
    }
    assert mi.desired_entry("copilot", "blumkin", mi.ServeSpec()) == {
        "type": "stdio",
        "command": "blumkin",
        "args": ["mcp", "serve"],
        "tools": ["*"],
    }


def test_detect_reads_which_and_cursor_dir(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda name: "/x" if name == "claude" else None)
    assert mi.detect() == {"claude"}
    (home / ".cursor").mkdir()
    assert mi.detect() == {"claude", "cursor"}  # a bare ~/.cursor dir counts


def test_config_path_matrix(home: Path) -> None:
    cwd = Path("/repo")
    assert mi.config_path("claude", "user", cwd) == home / ".claude.json"
    assert mi.config_path("claude", "project", cwd) == cwd / ".mcp.json"
    assert mi.config_path("copilot", "user", cwd) == home / ".copilot" / "mcp-config.json"
    assert mi.config_path("copilot", "project", cwd) == cwd / ".mcp.json"
    assert mi.config_path("cursor", "user", cwd) == home / ".cursor" / "mcp.json"
    assert mi.config_path("cursor", "project", cwd) == cwd / ".cursor" / "mcp.json"


def test_build_plan_classifies_add_unchanged_update(home: Path, all_clients: None) -> None:
    cwd = home / "repo"
    cwd.mkdir()
    serve = mi.ServeSpec()

    (plan,) = mi.build_plan(
        clients=["cursor"], scope="project", binary="blumkin", serve=serve, cwd=cwd
    )
    assert plan.action == "add"

    (cwd / ".cursor").mkdir()
    (cwd / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"blumkin": mi.desired_entry("cursor", "blumkin", serve)}})
    )
    (plan,) = mi.build_plan(
        clients=["cursor"], scope="project", binary="blumkin", serve=serve, cwd=cwd
    )
    assert plan.action == "unchanged"

    (plan,) = mi.build_plan(
        clients=["cursor"],
        scope="project",
        binary="blumkin",
        serve=mi.ServeSpec(read_only=True),
        cwd=cwd,
    )
    assert plan.action == "update"


def test_matches_ignores_extra_registered_keys() -> None:
    current = {
        "type": "stdio",
        "command": "/opt/pipx/bin/blumkin",
        "args": ["mcp", "serve"],
        "env": {},
    }
    assert mi._matches(current, {"args": ["mcp", "serve"]}, "blumkin") is True


def test_apply_file_preserves_other_servers(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda _n: None)  # no cursor-agent enable
    cwd = home / "repo"
    cwd.mkdir()
    path = cwd / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    (plan,) = mi.build_plan(
        clients=["cursor"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    assert mi.apply_plan(plan, binary="blumkin") == "added"
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["blumkin"]["args"] == ["mcp", "serve"]


def test_apply_file_cursor_runs_enable_when_present(
    home: Path, all_clients: None, fake_subprocess: list[list[str]]
) -> None:
    cwd = home / "repo"
    cwd.mkdir()
    (plan,) = mi.build_plan(
        clients=["cursor"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    mi.apply_plan(plan, binary="blumkin")
    assert ["cursor-agent", "mcp", "enable", "blumkin"] in fake_subprocess


def test_apply_cli_invokes_claude_mcp_add(
    home: Path, all_clients: None, fake_subprocess: list[list[str]]
) -> None:
    (plan,) = mi.build_plan(
        clients=["claude"],
        scope="user",
        binary="blumkin",
        serve=mi.ServeSpec(read_only=True),
        cwd=home,
    )
    assert mi.apply_plan(plan, binary="blumkin") == "added"
    assert ["claude", "mcp", "remove", "blumkin", "-s", "user"] in fake_subprocess
    add = next(c for c in fake_subprocess if c[:3] == ["claude", "mcp", "add"])
    assert add == [
        "claude",
        "mcp",
        "add",
        "blumkin",
        "-s",
        "user",
        "--transport",
        "stdio",
        "--",
        "blumkin",
        "mcp",
        "serve",
        "--read-only",
    ]


def test_apply_cli_nonzero_returncode_raises(
    home: Path, all_clients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        rc = 1 if cmd[:3] == ["claude", "mcp", "add"] else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom")

    monkeypatch.setattr(mi.subprocess, "run", _run)
    (plan,) = mi.build_plan(
        clients=["claude"], scope="user", binary="blumkin", serve=mi.ServeSpec(), cwd=home
    )
    with pytest.raises(mi.McpInstallError, match="claude mcp add"):
        mi.apply_plan(plan, binary="blumkin")


# --------------------------------------------------------------------------- CLI


def _invoke(args: list[str], **kw: Any) -> Any:
    return CliRunner().invoke(main, args, **kw)


_CURSOR_PROJECT = ["mcp", "install", "--client", "cursor", "--scope", "project", "--yes"]


def test_cli_install_writes_and_is_idempotent(
    home: Path, all_clients: None, fake_subprocess: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    first = _invoke([*_CURSOR_PROJECT, "--json"])
    assert first.exit_code == EXIT_SUCCESS, first.output
    payload = json.loads(first.output)
    assert payload["ok"] is True
    assert payload["clients"][0]["action"] == "added"
    assert (home / ".cursor" / "mcp.json").is_file()

    again = _invoke([*_CURSOR_PROJECT, "--json"])
    assert json.loads(again.output)["clients"][0]["action"] == "unchanged"


def test_cli_install_no_client_detected_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda _n: None)
    monkeypatch.setattr(mi, "detect", lambda clients=mi.CLIENTS: set())
    result = _invoke(["mcp", "install", "--scope", "user", "--yes", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.output)["error"] == "usage_error"


def test_cli_install_without_scope_non_interactive_is_usage_error(all_clients: None) -> None:
    result = _invoke(["mcp", "install", "--client", "cursor", "--yes", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert "scope is required" in json.loads(result.output)["message"]


def test_cli_install_reports_a_failing_client(
    home: Path, all_clients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)

    def _boom(plan: mi.ClientPlan, **_kw: Any) -> str:
        raise mi.McpInstallError("copilot mcp add failed", hint="try again")

    monkeypatch.setattr(mi, "apply_plan", _boom)
    result = _invoke(
        ["mcp", "install", "--client", "copilot", "--scope", "user", "--yes", "--json"]
    )
    assert result.exit_code == EXIT_OTHER
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["clients"][0]["action"] == "failed"


def test_cli_status_lists_registrations(
    home: Path, all_clients: None, fake_subprocess: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    _invoke(_CURSOR_PROJECT)
    result = _invoke(["mcp", "status", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert "cursor" in payload["detected"]
    cursor_regs = [r for r in payload["registrations"] if r["client"] == "cursor"]
    assert cursor_regs and cursor_regs[0]["args"] == ["mcp", "serve"]
    assert cursor_regs[0]["resolves_to_blumkin"] is True
