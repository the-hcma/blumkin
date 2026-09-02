"""`blumkin upgrade` wraps `pipx upgrade blumkin` and reports the from/to build."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin import cli
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE
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


def test_upgrade_requires_pipx(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.stdout or result.output)["error"] == "usage_error"


def test_upgrade_reports_from_and_to(monkeypatch, pipx_on_path, tmp_path) -> None:
    app = tmp_path / "blumkin"
    app.write_text("#!/bin/sh\n")
    versions = iter(["blumkin 0.1.0 (aaaaaaaaaaaa)", "blumkin 0.2.0 (bbbbbbbbbbbb)"])
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: app)
    monkeypatch.setattr(cli, "_read_pipx_version", lambda _exe: next(versions))
    _run_returns(monkeypatch, _completed(stdout="upgraded blumkin"))

    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["pipx_app_before"] == "blumkin 0.1.0 (aaaaaaaaaaaa)"
    assert payload["to"]["build"] == "blumkin 0.2.0 (bbbbbbbbbbbb)"
    assert payload["to"]["pipx_app"] == str(app)


def test_upgrade_surfaces_pipx_failure(monkeypatch, pipx_on_path) -> None:
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: None)
    _run_returns(monkeypatch, _completed(returncode=1, stderr="Package is not installed"))
    result = CliRunner().invoke(main, ["upgrade", "--json"])
    assert result.exit_code == EXIT_OTHER
    payload = json.loads(result.stdout or result.output)
    assert payload["error"] == "upgrade_failed"
    assert "not installed" in payload["hint"]


def test_upgrade_times_out(monkeypatch, pipx_on_path) -> None:
    monkeypatch.setattr(cli, "pipx_blumkin_path", lambda: None)
    _run_returns(monkeypatch, subprocess.TimeoutExpired(cmd="pipx", timeout=180))
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == EXIT_OTHER
    assert "timed out" in (result.stderr or result.output)


def test_read_pipx_version_parses_first_line(monkeypatch) -> None:
    _run_returns(
        monkeypatch,
        _completed(stdout="blumkin 1.2.3 (deadbeefcafe)\nrunning from /x\n"),
    )
    assert cli._read_pipx_version(Path("/x/blumkin")) == "blumkin 1.2.3 (deadbeefcafe)"


def test_read_pipx_version_none_on_nonzero(monkeypatch) -> None:
    _run_returns(monkeypatch, _completed(returncode=2))
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
