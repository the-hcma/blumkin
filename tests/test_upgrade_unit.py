"""`blumkin upgrade` wraps `pipx upgrade blumkin` and reports the pipx app's build."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin import cli
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS
from blumkin.pipx_install import pipx_blumkin_path


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["pipx", "upgrade", "blumkin"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _run_returns(monkeypatch, result):
    def _run(*_args, **_kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(cli.subprocess, "run", _run)


@pytest.fixture
def pipx_on_path(monkeypatch):
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pipx" if name == "pipx" else None
    )
    monkeypatch.setattr(cli, "build_version", lambda: "0.1.0 (aaaaaaaaaaaa)")
    monkeypatch.setattr(cli, "is_source_checkout", lambda: False)


def test_upgrade_pipx_missing_is_upgrade_failed(monkeypatch) -> None:
    """Doc/PR say exit 1 / upgrade_failed for "could not run" - not exit 2."""
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_OTHER
    assert json.loads(result.stderr)["error"] == "upgrade_failed"


def test_upgrade_reports_pipx_app_before_and_after(monkeypatch, pipx_on_path, tmp_path) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    versions = iter(["0.1.0 (aaaaaaaaaaaa)", "0.2.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: app)
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(versions))
    _run_returns(monkeypatch, _completed(stdout="upgraded blumkin"))

    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.stdout)
    assert payload["pipx_app"] == {
        "path": str(app),
        "before": "0.1.0 (aaaaaaaaaaaa)",
        "after": "0.2.0 (bbbbbbbbbbbb)",
    }
    assert payload["running_from"]["build"] == "0.1.0 (aaaaaaaaaaaa)"
    assert payload["source_checkout"] is False


def test_upgrade_human_output_shows_pipx_app_movement(monkeypatch, pipx_on_path, tmp_path) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    versions = iter(["0.1.0 (aaaaaaaaaaaa)", "0.2.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: app)
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(versions))
    _run_returns(monkeypatch, _completed(stdout="upgraded"))

    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_SUCCESS
    assert "from: 0.1.0 (aaaaaaaaaaaa)" in result.stdout
    assert "to:   0.2.0 (bbbbbbbbbbbb)" in result.stdout
    assert "source checkout" not in result.stdout


def test_upgrade_from_a_checkout_reports_the_pipx_app_and_the_checkout(
    monkeypatch, pipx_on_path, tmp_path
) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli, "is_source_checkout", lambda: True)
    monkeypatch.setattr(cli, "build_version", lambda: "0.9.0 (cccccccccccc)")
    versions = iter(["0.1.0 (aaaaaaaaaaaa)", "0.2.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: app)
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(versions))
    _run_returns(monkeypatch, _completed(stdout="upgraded"))

    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_SUCCESS
    # from/to describe the pipx app, not the checkout it was launched from.
    assert "from: 0.1.0 (aaaaaaaaaaaa)" in result.stdout
    assert "to:   0.2.0 (bbbbbbbbbbbb)" in result.stdout
    assert "source checkout (0.9.0 (cccccccccccc))" in result.stdout


def test_upgrade_when_pipx_app_version_unreadable_after(
    monkeypatch, pipx_on_path, tmp_path
) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    # readable before, unreadable after (the just-replaced shim mis-execs, etc).
    reads = iter(["0.1.0 (aaaaaaaaaaaa)", None])
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: app)
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(reads))
    _run_returns(monkeypatch, _completed(stdout="upgraded"))

    human = CliRunner().invoke(main, ["upgrade"])
    assert human.exit_code == EXIT_SUCCESS
    assert "from: 0.1.0 (aaaaaaaaaaaa)" in human.stdout
    assert f"to:   run `{app} --version` to confirm" in human.stdout

    reads2 = iter(["0.1.0 (aaaaaaaaaaaa)", None])
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(reads2))
    js = CliRunner().invoke(main, ["upgrade", "--json"])
    payload = json.loads(js.stdout)
    assert payload["pipx_app"]["before"] == "0.1.0 (aaaaaaaaaaaa)"
    assert payload["pipx_app"]["after"] is None
    assert payload["pipx_app"]["path"] == str(app)


def test_upgrade_when_pipx_app_version_unreadable_before_human_and_json_agree(
    monkeypatch, pipx_on_path, tmp_path
) -> None:
    """before=None must never be papered over with the running process's build."""
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    # Running from some other non-checkout install (build 0.9.0); the pipx app
    # exists but its --version fails, so `before` is unknowable.
    monkeypatch.setattr(cli, "build_version", lambda: "0.9.0 (cccccccccccc)")
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: app)
    reads = iter([None, "0.2.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(reads))
    _run_returns(monkeypatch, _completed(stdout="upgraded"))

    human = CliRunner().invoke(main, ["upgrade"])
    assert "from: (could not read the pipx app before upgrading)" in human.stdout
    assert "0.9.0" not in human.stdout  # the running build is not a stand-in

    reads2 = iter([None, "0.2.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(reads2))
    payload = json.loads(CliRunner().invoke(main, ["upgrade", "--json"]).stdout)
    assert payload["pipx_app"]["before"] is None
    assert payload["running_from"]["build"] == "0.9.0 (cccccccccccc)"


def test_upgrade_surfaces_pipx_failure(monkeypatch, pipx_on_path) -> None:
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: None)
    _run_returns(monkeypatch, _completed(returncode=1, stderr="Package is not installed"))
    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_OTHER
    payload = json.loads(result.stderr)
    assert payload["error"] == "upgrade_failed"
    assert "not installed" in payload["hint"]


def test_upgrade_handles_pipx_not_executable(monkeypatch, pipx_on_path) -> None:
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: None)
    _run_returns(monkeypatch, OSError("Exec format error"))
    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_OTHER
    assert json.loads(result.stderr)["error"] == "upgrade_failed"


def test_upgrade_times_out(monkeypatch, pipx_on_path) -> None:
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: None)
    _run_returns(monkeypatch, subprocess.TimeoutExpired(cmd="pipx", timeout=180))
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_OTHER
    assert "timed out" in result.stderr


def test_read_pipx_version_strips_the_prog_prefix(monkeypatch) -> None:
    _run_returns(
        monkeypatch,
        _completed(stdout="blumkin 1.2.3 (deadbeefcafe)\nrunning from /x\n"),
    )
    assert cli._read_pipx_version(Path("/x/blumkin")) == "1.2.3 (deadbeefcafe)"


@pytest.mark.parametrize(
    "outcome",
    [
        _completed(returncode=2),
        _completed(stdout="   \n"),
        OSError("boom"),
        subprocess.TimeoutExpired(cmd="blumkin", timeout=30),
    ],
)
def test_read_pipx_version_none_on_failure(monkeypatch, outcome) -> None:
    _run_returns(monkeypatch, outcome)
    assert cli._read_pipx_version(Path("/x/blumkin")) is None


def test_pipx_blumkin_path_honors_bin_dir(monkeypatch, tmp_path) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PIPX_BIN_DIR", str(tmp_path))
    assert pipx_blumkin_path() == app.resolve()


def test_pipx_blumkin_path_none_when_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PIPX_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert pipx_blumkin_path() is None
