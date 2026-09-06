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
    binaries = {"blumkin", "claude", "copilot", "cursor-agent"}
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


def test_matches_command_and_missing_required_keys() -> None:
    copilot = mi.desired_entry("copilot", "blumkin", mi.ServeSpec())

    # extra keys the client added (env) are ignored; command must be exact
    ok = {"type": "stdio", "command": "blumkin", "args": ["mcp", "serve"], "env": {}}
    assert mi._matches(ok, copilot, "blumkin") is True

    # a bare `blumkin` left behind after the binary moved no longer matches an abs path
    moved = {"command": "blumkin", "args": ["mcp", "serve"]}
    desired_abs = {"command": "/venv/bin/blumkin", "args": ["mcp", "serve"]}
    assert mi._matches(moved, desired_abs, "/venv/bin/blumkin") is False

    # a Copilot entry missing the required `type` is NOT a match -> forces an update
    no_type = {"command": "blumkin", "args": ["mcp", "serve"], "tools": ["*"]}
    assert mi._matches(no_type, copilot, "blumkin") is False

    # `tools` narrowed by the user is still a match
    narrowed = {**ok, "tools": ["calendar"]}
    assert mi._matches(narrowed, copilot, "blumkin") is True


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


def test_apply_file_refuses_a_malformed_config(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda _n: None)
    cwd = home / "repo"
    cwd.mkdir()
    path = cwd / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text('{ "mcpServers": { "other": {} },')  # trailing comma - invalid
    (plan,) = mi.build_plan(
        clients=["cursor"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    with pytest.raises(mi.McpInstallError, match="not valid JSON"):
        mi.apply_plan(plan, binary="blumkin")
    assert path.read_text() == '{ "mcpServers": { "other": {} },'  # left untouched


def test_apply_file_refuses_a_symlinked_target(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda _n: None)
    cwd = home / "repo"
    cwd.mkdir()
    victim = home / "secret"
    victim.write_text("do not touch")
    (cwd / ".cursor").mkdir()
    (cwd / ".cursor" / "mcp.json").symlink_to(victim)
    (plan,) = mi.build_plan(
        clients=["cursor"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    with pytest.raises(mi.McpInstallError, match="symlink"):
        mi.apply_plan(plan, binary="blumkin")
    assert victim.read_text() == "do not touch"


def test_build_plan_updates_a_copilot_entry_missing_type(home: Path, all_clients: None) -> None:
    cwd = home / "repo"
    cwd.mkdir()
    (cwd / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"blumkin": {"command": "blumkin", "args": ["mcp", "serve"]}}})
    )
    (plan,) = mi.build_plan(
        clients=["copilot"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    assert plan.action == "update"


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


def test_apply_cli_fresh_add_does_not_remove_first(
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
    assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_subprocess)
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


def test_apply_cli_update_removes_then_adds(
    home: Path, all_clients: None, fake_subprocess: list[list[str]]
) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"blumkin": {"command": "blumkin", "args": ["mcp", "serve"]}}})
    )
    (plan,) = mi.build_plan(
        clients=["claude"],
        scope="user",
        binary="blumkin",
        serve=mi.ServeSpec(read_only=True),
        cwd=home,
    )
    assert plan.action == "update"
    assert mi.apply_plan(plan, binary="blumkin") == "updated"
    assert ["claude", "mcp", "remove", "blumkin", "-s", "user"] in fake_subprocess


def test_apply_cli_restores_previous_entry_on_add_failure(
    home: Path, all_clients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".claude.json").write_text(
        json.dumps(
            {"mcpServers": {"blumkin": {"command": "/old/bin/blumkin", "args": ["mcp", "serve"]}}}
        )
    )
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        rc = 1 if cmd[:3] == ["claude", "mcp", "add"] else 0  # the replacing add fails
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="nope")

    monkeypatch.setattr(mi.subprocess, "run", _run)
    (plan,) = mi.build_plan(
        clients=["claude"],
        scope="user",
        binary="blumkin",
        serve=mi.ServeSpec(read_only=True),
        cwd=home,
    )
    with pytest.raises(mi.McpInstallError, match="claude mcp add"):
        mi.apply_plan(plan, binary="blumkin")
    # add-json put the old entry back verbatim (preserves every key)
    restore = next(c for c in calls if c[:3] == ["claude", "mcp", "add-json"])
    assert json.loads(restore[-1]) == {"command": "/old/bin/blumkin", "args": ["mcp", "serve"]}


def test_apply_cli_copilot_restore_reconstructs_env_and_tools(
    home: Path, all_clients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".copilot").mkdir()
    (home / ".copilot" / "mcp-config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "blumkin": {
                        "type": "stdio",
                        "command": "blumkin",
                        "args": ["mcp", "serve"],
                        "tools": ["calendar", "mail"],
                        "env": {"BLUMKIN_PROFILE": "work"},
                    }
                }
            }
        )
    )
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        # the replacing add fails; remove and the restore add succeed
        rc = 1 if cmd[:3] == ["copilot", "mcp", "add"] and "--read-only" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="nope")

    monkeypatch.setattr(mi.subprocess, "run", _run)
    (plan,) = mi.build_plan(
        clients=["copilot"],
        scope="user",
        binary="blumkin",
        serve=mi.ServeSpec(read_only=True),
        cwd=home,
    )
    assert plan.action == "update"
    with pytest.raises(mi.McpInstallError, match="copilot mcp add"):
        mi.apply_plan(plan, binary="blumkin")
    restore = [c for c in calls if c[:3] == ["copilot", "mcp", "add"]][-1]
    assert "--tools" in restore and restore[restore.index("--tools") + 1] == "calendar,mail"
    assert "--env" in restore and "BLUMKIN_PROFILE=work" in restore


def test_apply_cli_claude_project_scope_refuses_a_symlink(
    home: Path, all_clients: None, fake_subprocess: list[list[str]]
) -> None:
    cwd = home / "repo"
    cwd.mkdir()
    victim = home / "secret"
    victim.write_text("keep me")
    (cwd / ".mcp.json").symlink_to(victim)
    (plan,) = mi.build_plan(
        clients=["claude"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    with pytest.raises(mi.McpInstallError, match="symlink"):
        mi.apply_plan(plan, binary="blumkin")
    assert not any(c[:2] == ["claude", "mcp"] for c in fake_subprocess)
    assert victim.read_text() == "keep me"


def test_apply_file_user_scope_honours_a_dotfile_symlink(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda _n: None)
    dotfiles = home / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "cursor-mcp.json"
    real.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").symlink_to(real)
    (plan,) = mi.build_plan(
        clients=["cursor"], scope="user", binary="blumkin", serve=mi.ServeSpec(), cwd=home
    )
    assert mi.apply_plan(plan, binary="blumkin") == "added"
    assert (home / ".cursor" / "mcp.json").is_symlink()  # link preserved
    data = json.loads(real.read_text())  # real target updated
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert "blumkin" in data["mcpServers"]


def test_apply_cli_reports_when_restore_also_fails(
    home: Path, all_clients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"blumkin": {"command": "blumkin", "args": ["mcp", "serve"]}}})
    )

    def _run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        rc = 0 if cmd[:3] == ["claude", "mcp", "remove"] else 1  # remove ok, both adds fail
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="down")

    monkeypatch.setattr(mi.subprocess, "run", _run)
    (plan,) = mi.build_plan(
        clients=["claude"],
        scope="user",
        binary="blumkin",
        serve=mi.ServeSpec(read_only=True),
        cwd=home,
    )
    with pytest.raises(mi.McpInstallError, match="could NOT be restored"):
        mi.apply_plan(plan, binary="blumkin")


def test_apply_file_write_is_atomic_and_cleans_up(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda _n: None)
    cwd = home / "repo"
    cwd.mkdir()
    path = cwd / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    monkeypatch.setattr(mi.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    (plan,) = mi.build_plan(
        clients=["cursor"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    with pytest.raises(mi.McpInstallError, match="could not write"):
        mi.apply_plan(plan, binary="blumkin")
    assert json.loads(path.read_text())["mcpServers"] == {"other": {"command": "x"}}  # intact
    assert not list(path.parent.glob(".*blumkin*"))  # temp file cleaned up


def test_command_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mi.command_is_current("blumkin", "blumkin") is True
    monkeypatch.setattr(
        mi.shutil, "which", lambda n: "/opt/bin/blumkin" if n == "blumkin" else None
    )
    assert mi.command_is_current("blumkin", "/opt/bin/blumkin") is True
    assert mi.command_is_current("/dead/venv/bin/blumkin", "blumkin") is False


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


def test_cli_install_non_tty_without_yes_is_usage_error(all_clients: None) -> None:
    # CliRunner gives a non-TTY stdin/stdout: writing every client's config
    # without the per-client prompt must require an explicit --yes.
    result = _invoke(["mcp", "install", "--client", "cursor", "--scope", "user", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert "--yes" in json.loads(result.output)["message"]


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


def test_cli_status_empty_and_stale(
    home: Path, all_clients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    empty = _invoke(["mcp", "status"])
    assert "no client has blumkin registered" in empty.output

    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"blumkin": {"command": "/dead/bin/blumkin", "args": []}}})
    )
    stale = _invoke(["mcp", "status", "--json"])
    reg = json.loads(stale.output)["registrations"][0]
    assert reg["resolves_to_blumkin"] is False
    assert "does not resolve" in _invoke(["mcp", "status"]).output


def test_cli_install_force_reapplies_a_matching_entry(
    home: Path, all_clients: None, fake_subprocess: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    _invoke([*_CURSOR_PROJECT, "--json"])
    forced = _invoke([*_CURSOR_PROJECT, "--force", "--json"])
    assert json.loads(forced.output)["clients"][0]["action"] == "updated"


def _tty(monkeypatch: pytest.MonkeyPatch) -> None:
    import blumkin.cli as cli

    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)


def test_cli_install_interactive_decline_skips_without_writing(
    home: Path, all_clients: None, fake_subprocess: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    _tty(monkeypatch)
    # scope prompt -> project; read-only? -> n; families -> blank; confirm cursor -> n
    result = _invoke(["mcp", "install", "--client", "cursor"], input="project\nn\n\nn\n")
    assert result.exit_code == EXIT_SUCCESS
    assert "skipped" in result.output
    assert not (home / ".cursor" / "mcp.json").exists()


def test_cli_install_interactive_confirm_writes(
    home: Path, all_clients: None, fake_subprocess: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    _tty(monkeypatch)
    result = _invoke(["mcp", "install", "--client", "cursor"], input="project\nn\n\ny\n")
    assert result.exit_code == EXIT_SUCCESS
    assert "added" in result.output
    assert (home / ".cursor" / "mcp.json").is_file()


def test_apply_file_keeps_a_locked_down_file_private(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mi.shutil, "which", lambda _n: None)
    cwd = home / "repo"
    cwd.mkdir()
    path = cwd / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"secret": {"env": {"TOKEN": "x"}}}}))
    path.chmod(0o600)
    (plan,) = mi.build_plan(
        clients=["copilot"], scope="project", binary="blumkin", serve=mi.ServeSpec(), cwd=cwd
    )
    mi.apply_plan(plan, binary="blumkin")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text())["mcpServers"]["secret"] == {"env": {"TOKEN": "x"}}
