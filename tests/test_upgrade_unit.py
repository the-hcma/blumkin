"""`blumkin upgrade` dispatches on the detected install method (issue #239)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin import cli
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS
from blumkin.install_method import (
    METHOD_EDITABLE_PIPX,
    METHOD_EDITABLE_UV,
    METHOD_PIPX,
    METHOD_SOURCE_CHECKOUT,
    METHOD_UNMANAGED,
    METHOD_UV_TOOL,
    Checkout,
    Install,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["<cmd>"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _editable_uv(checkout_path: str = "/co") -> Install:
    return Install(
        checkout=Checkout(
            behind_origin=2,
            branch="main",
            dirty=False,
            head="abcabcabcabc",
            path=Path(checkout_path),
        ),
        managed_path=Path("/home/u/.local/bin/blumkin"),
        method=METHOD_EDITABLE_UV,
    )


@pytest.fixture
def install(monkeypatch):
    """Pin the detected install; tests set ``holder['value']`` before invoking."""
    holder: dict[str, Install] = {}
    monkeypatch.setattr(cli, "detect_install", lambda **_k: holder["value"])
    monkeypatch.setattr(cli, "metadata_stale", lambda _path: None)
    monkeypatch.setattr(cli, "_read_app_version", lambda _p: "0.5.0 (aaaaaaaaaaaa)")
    monkeypatch.setattr(cli, "build_version", lambda: "0.5.0 (aaaaaaaaaaaa)")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Issue #408: `upgrade` scans for stale agent/mcp-serve processes after a
    # real action - pin it to "none found" so these tests are not at the
    # mercy of whatever happens to be running on the machine they execute on.
    monkeypatch.setattr(cli.agent_processes, "discover_agent_instances", lambda: [])
    monkeypatch.setattr(cli.agent_processes, "mcp_serve_processes", lambda: [])
    return holder


def _run_records(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _run(cmd, *_a, **_k):
        calls.append(list(cmd))
        return _completed(stdout="done")

    monkeypatch.setattr(cli.subprocess, "run", _run)
    return calls


# --- unmanaged -------------------------------------------------------------


def test_unmanaged_touches_nothing_and_exits_zero(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/usr/bin/blumkin"), method=METHOD_UNMANAGED
    )
    calls = _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_SUCCESS
    assert calls == []
    assert "not package-managed" in result.output
    assert "uv tool install blumkin" in result.output


def test_unmanaged_json_shape(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/usr/bin/blumkin"), method=METHOD_UNMANAGED
    )
    _run_records(monkeypatch)

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["ok"] is True
    assert payload["install_method"] == "unmanaged"
    assert payload["action_taken"] is None
    assert payload["checkout"] is None
    assert payload["suggested_commands"] == []
    assert payload["managed_path"] == "/usr/bin/blumkin"


# --- pipx / uv tool from PyPI --------------------------------------------


def test_pipx_pypi_runs_pipx_upgrade(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    versions = iter(["0.5.0 (aaaaaaaaaaaa)", "0.6.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "_read_app_version", lambda _p: next(versions))
    calls = _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_SUCCESS
    assert calls == [["/usr/bin/pipx", "upgrade", "blumkin"]]
    assert "from: 0.5.0 (aaaaaaaaaaaa)" in result.output
    assert "to:   0.6.0 (bbbbbbbbbbbb)" in result.output


def test_uv_tool_pypi_runs_uv_tool_upgrade(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_UV_TOOL
    )
    calls = _run_records(monkeypatch)

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert calls == [["/usr/bin/uv", "tool", "upgrade", "blumkin"]]
    assert payload["action_taken"] == "uv tool upgrade blumkin"
    assert payload["install_method"] == "uv-tool"


def test_pypi_upgrade_needs_the_manager_on_path(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_UV_TOOL
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_OTHER
    assert json.loads(result.stderr)["error"] == "upgrade_failed"


def test_pypi_upgrade_surfaces_a_non_zero_step(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *_a, **_k: _completed(returncode=1, stderr="boom")
    )

    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_OTHER
    payload = json.loads(result.stderr)
    assert payload["error"] == "upgrade_failed"
    assert "boom" in payload["hint"]


def test_pypi_upgrade_times_out(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )

    def _run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="pipx", timeout=300)

    monkeypatch.setattr(cli.subprocess, "run", _run)
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_OTHER
    assert "timed out" in result.stderr


# --- editable installs --------------------------------------------------


def test_editable_without_yes_prints_commands_and_runs_nothing(install, monkeypatch) -> None:
    install["value"] = _editable_uv()
    calls = _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_SUCCESS
    assert calls == []
    assert "git -C /co pull --ff-only" in result.output
    assert "uv tool install -e /co --force" in result.output
    assert "--yes" in result.output


def test_editable_without_yes_json_carries_checkout_and_commands(install) -> None:
    install["value"] = _editable_uv()

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["install_method"] == "editable-uv"
    assert payload["action_taken"] is None
    assert payload["checkout"] == {
        "behind_origin": 2,
        "branch": "main",
        "dirty": False,
        "head": "abcabcabcabc",
        "path": "/co",
    }
    assert payload["suggested_commands"] == [
        "git -C /co pull --ff-only",
        "uv tool install -e /co --force",
    ]


def test_editable_uv_with_yes_pulls_then_force_reinstalls(install, monkeypatch) -> None:
    install["value"] = _editable_uv()
    # from: the pre-pull build; to: must be re-read from disk after the reinstall.
    builds = iter(["0.5.0 (aaaaaaaaaaaa)", "0.6.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "_read_app_version", lambda _p: next(builds))
    # metadata_stale before the steps, coherent after -> the final report must
    # reflect the post-reinstall recompute, not the pre-step value.
    stale = iter([("0.5.0", "0.6.0"), None])
    monkeypatch.setattr(cli, "metadata_stale", lambda _p: next(stale))
    calls = _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade", "--yes", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert calls == [
        ["/usr/bin/git", "-C", "/co", "pull", "--ff-only"],
        ["/usr/bin/uv", "tool", "install", "-e", "/co", "--force"],
    ]
    payload = json.loads(result.output)
    assert payload["from"] == "0.5.0 (aaaaaaaaaaaa)"
    assert payload["to"] == "0.6.0 (bbbbbbbbbbbb)"
    assert payload["metadata_stale"] is False
    assert payload["action_taken"] == "git -C /co pull --ff-only && uv tool install -e /co --force"


def test_editable_pipx_with_yes_uses_pipx_install(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=Checkout(
            behind_origin=0, branch="main", dirty=False, head="aaaaaaaaaaaa", path=Path("/co")
        ),
        managed_path=Path("/home/u/.local/bin/blumkin"),
        method=METHOD_EDITABLE_PIPX,
    )
    calls = _run_records(monkeypatch)

    CliRunner().invoke(main, ["upgrade", "--yes"])
    assert calls == [
        ["/usr/bin/git", "-C", "/co", "pull", "--ff-only"],
        ["/usr/bin/pipx", "install", "-e", "/co", "--force"],
    ]


def test_editable_yes_stops_when_the_pull_fails(install, monkeypatch) -> None:
    install["value"] = _editable_uv()

    def _run(cmd, *_a, **_k):
        if "pull" in cmd:
            return _completed(returncode=1, stderr="not fast-forward")
        raise AssertionError("reinstall must not run after a failed pull")

    monkeypatch.setattr(cli.subprocess, "run", _run)
    result = CliRunner().invoke(main, ["upgrade", "--yes", "--json"])
    assert result.exit_code == EXIT_OTHER
    assert json.loads(result.stderr)["error"] == "upgrade_failed"


def test_editable_surfaces_stale_metadata(install, monkeypatch) -> None:
    install["value"] = _editable_uv()
    monkeypatch.setattr(cli, "metadata_stale", lambda _p: ("0.5.0", "0.6.0"))

    human = CliRunner().invoke(main, ["upgrade"]).output
    assert "installed metadata (0.5.0) is stale vs the checkout (0.6.0)" in human
    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["metadata_stale"] is True


def _source_checkout(path: str = "/co") -> Install:
    return Install(
        checkout=Checkout(
            behind_origin=1, branch="main", dirty=False, head="a" * 12, path=Path(path)
        ),
        managed_path=Path(f"{path}/.venv/bin/blumkin"),
        method=METHOD_SOURCE_CHECKOUT,
    )


def test_bare_source_checkout_without_yes_offers_only_the_pull(install, monkeypatch) -> None:
    install["value"] = _source_checkout()  # /co has no uv.lock
    calls = _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_SUCCESS
    assert calls == []
    assert "git -C /co pull --ff-only" in result.output
    assert "uv sync" not in result.output and "install -e" not in result.output


def test_bare_source_checkout_with_yes_runs_only_the_pull(install, monkeypatch) -> None:
    install["value"] = _source_checkout()
    calls = _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade", "--yes", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert calls == [["/usr/bin/git", "-C", "/co", "pull", "--ff-only"]]
    assert json.loads(result.output)["action_taken"] == "git -C /co pull --ff-only"


def test_uv_project_source_checkout_pulls_then_syncs(install, monkeypatch, tmp_path) -> None:
    (tmp_path / "uv.lock").write_text("")
    install["value"] = _source_checkout(str(tmp_path))
    calls = _run_records(monkeypatch)

    result = CliRunner().invoke(main, ["upgrade", "--yes", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert calls == [
        ["/usr/bin/git", "-C", str(tmp_path), "pull", "--ff-only"],
        ["/usr/bin/uv", "sync", "--project", str(tmp_path)],
    ]
    assert json.loads(result.output)["action_taken"].endswith(f"uv sync --project {tmp_path}")


# --- _read_app_version --------------------------------------------------


def test_read_app_version_strips_the_prog_prefix(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_a, **_k: _completed(stdout="blumkin 1.2.3 (deadbeefcafe)\nrunning from /x\n"),
    )
    assert cli._read_app_version(Path("/x/blumkin")) == "1.2.3 (deadbeefcafe)"


@pytest.mark.parametrize(
    "outcome",
    [
        _completed(returncode=2),
        _completed(stdout="   \n"),
        OSError("boom"),
        subprocess.TimeoutExpired(cmd="blumkin", timeout=30),
    ],
)
def test_read_app_version_none_on_failure(monkeypatch, outcome) -> None:
    def _run(*_a, **_k):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cli.subprocess, "run", _run)
    assert cli._read_app_version(Path("/x/blumkin")) is None


# --- stale_processes (issue #408) ----------------------------------------


def _agent_instance(*, pid: int = 111, version: str = "0.5.0 (aaaaaaaaaaaa)"):
    from blumkin.agent import processes as agent_processes

    return agent_processes.AgentInstance(
        socket_path=Path("/tmp/x/agent.sock"), pid=pid, version=version, is_primary=True
    )


def _mcp_serve_proc(*, pid: int = 222):
    from blumkin.agent import processes as agent_processes

    return agent_processes.McpServeProcess(pid=pid, command="blumkin mcp serve", started_at=None)


def _version_change(monkeypatch) -> None:
    """Make `before`/`after` app-version reads differ, so the stale-process
    scan (gated on an actual version change - PR #409 review) still runs."""
    versions = iter(["0.5.0 (aaaaaaaaaaaa)", "0.6.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "_read_app_version", lambda _p: next(versions))


def test_upgrade_stops_a_still_running_agent_after_a_real_upgrade(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    _version_change(monkeypatch)
    _run_records(monkeypatch)
    monkeypatch.setattr(
        cli.agent_processes, "discover_agent_instances", lambda: [_agent_instance()]
    )
    monkeypatch.setattr(cli.agent_client, "call_at", lambda _path, _cmd: {"ok": True})

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["stale_processes"] == [
        {
            "pid": 111,
            "kind": "agent",
            "build": "0.5.0 (aaaaaaaaaaaa)",
            "started_at": None,
            "action": "stopped",
        }
    ]


def test_upgrade_leaves_an_agent_already_on_the_new_build_alone(install, monkeypatch) -> None:
    """A concurrent operation (e.g. another skill call) can respawn the agent
    on the new build between the upgrade step finishing and this scan
    running - such an agent is already current and must not be shut down as
    if it were stale (PR #409 review)."""
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    _version_change(monkeypatch)
    _run_records(monkeypatch)
    monkeypatch.setattr(
        cli.agent_processes,
        "discover_agent_instances",
        lambda: [_agent_instance(version="0.6.0 (bbbbbbbbbbbb)")],
    )
    call_at = []
    monkeypatch.setattr(cli.agent_client, "call_at", lambda *a: call_at.append(a) or {"ok": True})

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["stale_processes"] == []
    assert call_at == []  # never even asked to shut down


def test_upgrade_counts_a_protocol_mismatch_agent_as_stopped(install, monkeypatch) -> None:
    """A legacy agent may reply `protocol_mismatch` to `shutdown` - it already
    committed to shutting itself down before replying (PR #404), so this is
    the success case, not a failure (PR #409 review)."""
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    _version_change(monkeypatch)
    _run_records(monkeypatch)
    monkeypatch.setattr(
        cli.agent_processes, "discover_agent_instances", lambda: [_agent_instance()]
    )
    monkeypatch.setattr(
        cli.agent_client,
        "call_at",
        lambda _path, _cmd: {"ok": False, "error": "protocol_mismatch"},
    )

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["stale_processes"][0]["action"] == "stopped"


def test_upgrade_reports_an_agent_it_could_not_stop(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    _version_change(monkeypatch)
    _run_records(monkeypatch)
    monkeypatch.setattr(
        cli.agent_processes, "discover_agent_instances", lambda: [_agent_instance()]
    )
    monkeypatch.setattr(cli.agent_client, "call_at", lambda _path, _cmd: None)

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["stale_processes"][0]["action"] == "stop_failed"


def test_upgrade_reports_but_does_not_kill_a_still_running_mcp_serve(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    _version_change(monkeypatch)
    _run_records(monkeypatch)
    monkeypatch.setattr(cli.agent_processes, "mcp_serve_processes", lambda: [_mcp_serve_proc()])
    call_at = []
    monkeypatch.setattr(cli.agent_client, "call_at", lambda *a: call_at.append(a) or None)

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["stale_processes"] == [
        {
            "pid": 222,
            "kind": "mcp_serve",
            "build": "0.5.0 (aaaaaaaaaaaa)",
            "started_at": None,
            "action": "restart_recommended",
        }
    ]
    assert call_at == []  # never killed - only agents are stopped


def test_upgrade_stale_processes_is_empty_when_none_found(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    _run_records(monkeypatch)

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["stale_processes"] == []


def test_upgrade_does_not_scan_for_stale_processes_when_no_action_was_taken(
    install, monkeypatch
) -> None:
    """An unmanaged install, or an editable checkout without `--yes`, never
    actually upgrades anything - scanning for processes to retire in that
    case would be pointless (nothing changed) and is skipped."""
    install["value"] = Install(
        checkout=None, managed_path=Path("/usr/bin/blumkin"), method=METHOD_UNMANAGED
    )
    _run_records(monkeypatch)
    discover = []
    monkeypatch.setattr(
        cli.agent_processes, "discover_agent_instances", lambda: discover.append(1) or []
    )

    result = CliRunner().invoke(main, ["upgrade", "--json"])
    payload = json.loads(result.output)
    assert "stale_processes" not in payload or payload["stale_processes"] == []
    assert discover == []


def test_upgrade_does_not_scan_for_stale_processes_when_the_version_did_not_change(
    install, monkeypatch
) -> None:
    """A pipx/uv reinstall that lands on the same version (e.g. `--force`
    reinstalling an unchanged release) is a no-op - stopping every reachable
    agent and telling the user to restart their MCP client would be a false
    positive, so the scan is skipped entirely (PR #409 review)."""
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    monkeypatch.setattr(cli, "_read_app_version", lambda _p: "0.5.0 (aaaaaaaaaaaa)")
    _run_records(monkeypatch)
    discover = []
    monkeypatch.setattr(
        cli.agent_processes,
        "discover_agent_instances",
        lambda: discover.append(1) or [_agent_instance()],
    )

    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).output)
    assert payload["stale_processes"] == []
    assert discover == []


def test_upgrade_scans_for_stale_processes_when_the_version_is_unmeasurable(
    install, monkeypatch
) -> None:
    """A bare source checkout (`managed_path=None`) never calls
    `_read_app_version`, so `before`/`after` are both `None` even after a
    real `git pull` + `uv sync` upgrade - `None == None` means "unmeasurable",
    not "unchanged", so the scan must still run rather than being silently
    skipped like a confirmed no-op (PR #409 review)."""
    install["value"] = Install(
        checkout=Checkout(
            behind_origin=2, branch="main", dirty=False, head="abcabcabcabc", path=Path("/co")
        ),
        managed_path=None,
        method=METHOD_EDITABLE_UV,
    )
    _run_records(monkeypatch)
    discover = []
    monkeypatch.setattr(
        cli.agent_processes, "discover_agent_instances", lambda: discover.append(1) or []
    )

    result = CliRunner().invoke(main, ["upgrade", "--yes", "--json"])
    payload = json.loads(result.output)
    assert payload["stale_processes"] == []
    assert discover == [1]  # the scan ran - it just found nothing reachable


def test_upgrade_text_reports_stale_processes(install, monkeypatch) -> None:
    install["value"] = Install(
        checkout=None, managed_path=Path("/home/u/.local/bin/blumkin"), method=METHOD_PIPX
    )
    _version_change(monkeypatch)
    _run_records(monkeypatch)
    monkeypatch.setattr(
        cli.agent_processes, "discover_agent_instances", lambda: [_agent_instance()]
    )
    monkeypatch.setattr(cli.agent_client, "call_at", lambda _path, _cmd: {"ok": True})

    result = CliRunner().invoke(main, ["upgrade"])
    assert "stale processes found" in result.output
    assert "pid=111" in result.output
    assert "retired" in result.output
