"""Build-metadata resolution: env override, embedded stamp, git, then unknown."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin import version as version_module
from blumkin.cli import main
from blumkin.exit_codes import EXIT_SUCCESS
from blumkin.version import (
    build_info,
    build_status_fields,
    format_cli_version_line,
    git_commit,
    package_version,
)


@pytest.fixture(autouse=True)
def _clear_build_info_cache():
    version_module.get_build_info.cache_clear()
    yield
    version_module.get_build_info.cache_clear()


def test_git_commit_prefers_blumkin_override_and_shortens(tmp_path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert git_commit(environ={"BLUMKIN_GIT_SHA": sha}, repository=tmp_path) == sha[:12]


def test_git_commit_ignores_ambient_github_sha(monkeypatch, tmp_path) -> None:
    """GITHUB_SHA is the workflow repo's commit, not blumkin's - it must not win."""
    monkeypatch.setattr(
        version_module._build_metadata, "EMBEDDED_COMMIT", "abcdef123456", raising=False
    )
    unrelated = "9999999999999999999999999999999999999999"
    assert git_commit(environ={"GITHUB_SHA": unrelated}, repository=tmp_path) == "abcdef123456"


def test_git_commit_falls_back_to_embedded(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        version_module._build_metadata, "EMBEDDED_COMMIT", "abcdef123456", raising=False
    )
    assert git_commit(environ={}, repository=tmp_path) == "abcdef123456"


def test_git_commit_unknown_without_checkout_env_or_embed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(version_module._build_metadata, "EMBEDDED_COMMIT", "", raising=False)
    assert git_commit(environ={}, repository=tmp_path) == "unknown"


def test_git_commit_reads_the_checkout() -> None:
    commit = git_commit(environ={})
    assert commit != "unknown"
    assert len(commit) == 12


def test_package_version_prefers_embedded(monkeypatch) -> None:
    monkeypatch.setattr(version_module._build_metadata, "EMBEDDED_VERSION", "9.9.9", raising=False)
    assert package_version() == "9.9.9"


def test_package_version_reads_pyproject_when_not_installed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(version_module._build_metadata, "EMBEDDED_VERSION", "", raising=False)

    def _raise(_name: str):
        raise version_module.PackageNotFoundError

    monkeypatch.setattr(version_module, "version", _raise)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    assert package_version(pyproject_path=tmp_path / "pyproject.toml") == "1.2.3"


def test_running_command_reinvokes_python_dash_m_for_a_module_launch(monkeypatch) -> None:
    """A `python -m blumkin mcp serve` launch's `sys.argv[0]` resolves to the
    non-executable `__main__.py` - `running_command()` must re-invoke via
    `<python> -m blumkin`, not try to run that file directly (issue #408
    review: the only prior test asserted `isinstance(..., tuple)`, which
    stays true even for the regression this branch exists to prevent)."""
    monkeypatch.setattr(sys, "argv", ["/x/blumkin/__main__.py", "mcp", "serve"])
    assert version_module.running_command() == (sys.executable, "-m", "blumkin")


def test_running_command_is_just_the_path_for_a_console_script_launch(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["blumkin", "mcp", "serve"])
    monkeypatch.setattr(version_module, "running_command_path", lambda: Path("/usr/bin/blumkin"))
    assert version_module.running_command() == ("/usr/bin/blumkin",)


def test_read_installed_version_expands_a_tuple_executable_into_argv(monkeypatch) -> None:
    """`read_installed_version` is called with `running_command()`'s tuple
    form (not a bare `Path`) in production - assert the whole tuple is
    spread into the spawned argv, not just its first element."""
    captured: dict[str, object] = {}

    def _fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return version_module.subprocess.CompletedProcess(
            argv, 0, stdout="blumkin 1.2.3 (abc123456789)\n", stderr=""
        )

    monkeypatch.setattr(version_module.subprocess, "run", _fake_run)
    result = version_module.read_installed_version((sys.executable, "-m", "blumkin"))
    assert result == "1.2.3 (abc123456789)"
    assert captured["argv"] == [sys.executable, "-m", "blumkin", "--version"]


def test_format_helpers_render_one_line() -> None:
    line = format_cli_version_line(prog="blumkin")
    assert line.startswith("blumkin ")
    assert line == build_info()
    assert "(" in line and line.endswith(")")


def test_build_status_fields_shape() -> None:
    fields = build_status_fields()
    assert set(fields) == {"build_commit", "build_version", "running_from"}
    assert all(isinstance(value, str) and value for value in fields.values())


def test_cli_version_flag_reports_version_commit_and_path() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == EXIT_SUCCESS
    first, second = result.output.strip().splitlines()
    assert first.startswith("blumkin ")
    assert second.startswith("running from ")


def test_doctor_json_carries_build_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[profiles.default]\n"
        'client_id = "fake-google-desktop-client.apps.googleusercontent.com"\n'
        'provider = "google"\n'
        'default_tz = "UTC"\n'
    )
    result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.output)
    assert set(payload["build"]) == {"build_commit", "build_version", "running_from"}


def test_auth_status_json_carries_build_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[profiles.default]\n"
        'client_id = "fake-google-desktop-client.apps.googleusercontent.com"\n'
        'provider = "google"\n'
        'default_tz = "UTC"\n'
    )
    result = CliRunner().invoke(main, ["auth", "status", "--json"])
    payload = json.loads(result.output)
    assert payload["build_version"] and payload["build_commit"] and payload["running_from"]
