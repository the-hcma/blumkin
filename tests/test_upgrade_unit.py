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
