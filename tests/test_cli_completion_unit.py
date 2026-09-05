"""`blumkin completion <shell>` prints a usable script (issue #98) or installs it (#167)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE


@pytest.mark.parametrize(
    ("shell", "needle"),
    [
        ("bash", "_BLUMKIN_COMPLETE=bash_complete"),
        ("zsh", "compdef _blumkin_completion blumkin"),
        ("fish", "_BLUMKIN_COMPLETE=fish_complete"),
    ],
)
def test_completion_emits_a_script_per_shell(shell: str, needle: str) -> None:
    result = CliRunner().invoke(main, ["completion", shell])
    assert result.exit_code == EXIT_SUCCESS
    assert needle in result.output
    assert "blumkin" in result.output
    assert "Traceback" not in result.output


def test_completion_force_without_install_is_usage_error() -> None:
    result = CliRunner().invoke(main, ["completion", "bash", "--force", "--json"])
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.stderr)["error"] == "usage_error"


def test_completion_help_lists_all_three_shells() -> None:
    out = CliRunner().invoke(main, ["completion", "--help"]).output
    for shell in ("bash", "zsh", "fish"):
        assert shell in out


def test_completion_install_human_output_bash_says_open_a_new_shell(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = CliRunner().invoke(main, ["completion", "bash", "--install"])
    assert result.exit_code == EXIT_SUCCESS
    assert result.stdout.splitlines()[0].startswith("written: ")
    assert "open a new shell" in result.stdout
    assert "fpath" not in result.stdout


def test_completion_install_human_output_zsh_names_fpath(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    target = tmp_path / "zsh" / "site-functions" / "_blumkin"
    first = CliRunner().invoke(main, ["completion", "zsh", "--install"])
    assert first.exit_code == EXIT_SUCCESS
    assert first.stderr == ""
    assert first.stdout.splitlines()[0] == f"written: {target}"
    assert "fpath" in first.stdout and "compinit" in first.stdout
    again = CliRunner().invoke(main, ["completion", "zsh", "--install"])
    assert again.stdout.splitlines()[0] == f"unchanged: {target}"


def test_completion_install_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    first = CliRunner().invoke(main, ["completion", "bash", "--install", "--json"])
    second = CliRunner().invoke(main, ["completion", "bash", "--install", "--json"])
    assert first.exit_code == second.exit_code == EXIT_SUCCESS
    assert json.loads(first.stdout)["action"] == "written"
    assert json.loads(second.stdout)["action"] == "unchanged"


def test_completion_install_refuses_to_clobber_without_force(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    target = tmp_path / "bash-completion" / "completions" / "blumkin.bash"
    target.parent.mkdir(parents=True)
    target.write_text("# stale hand-edited content\n")
    blocked = CliRunner().invoke(main, ["completion", "bash", "--install", "--json"])
    assert blocked.exit_code == EXIT_USAGE
    assert json.loads(blocked.stderr)["error"] == "usage_error"
    assert target.read_text() == "# stale hand-edited content\n"
    forced = CliRunner().invoke(main, ["completion", "bash", "--install", "--force", "--json"])
    assert forced.exit_code == EXIT_SUCCESS
    assert json.loads(forced.stdout)["action"] == "written"
    assert "_BLUMKIN_COMPLETE" in target.read_text()


def test_completion_install_reports_a_clean_error_when_target_is_a_directory(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "bash-completion" / "completions" / "blumkin.bash").mkdir(parents=True)
    result = CliRunner().invoke(main, ["completion", "bash", "--install", "--json"])
    assert result.exit_code == EXIT_OTHER
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"] == "install_failed"
    assert str(tmp_path) in payload["message"]
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("shell", "rel"),
    [
        ("bash", "xdg/bash-completion/completions/blumkin.bash"),
        ("zsh", "xdg/zsh/site-functions/_blumkin"),
        ("fish", "cfg/fish/completions/blumkin.fish"),
    ],
)
def test_completion_install_writes_the_conventional_path(
    monkeypatch, tmp_path: Path, shell: str, rel: str
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    result = CliRunner().invoke(main, ["completion", shell, "--install", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.stdout)
    target = tmp_path / rel
    assert payload["shell"] == shell
    assert payload["path"] == str(target)
    assert payload["action"] == "written"
    assert target.read_text() == CliRunner().invoke(main, ["completion", shell]).output
    assert target.stat().st_mode & 0o777  # readable


def test_completion_json_without_install_returns_the_script() -> None:
    result = CliRunner().invoke(main, ["completion", "fish", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.stdout)
    assert payload["shell"] == "fish"
    assert "_BLUMKIN_COMPLETE=fish_complete" in payload["script"]


def test_completion_rejects_unknown_shell() -> None:
    result = CliRunner().invoke(main, ["completion", "tcsh"])
    assert result.exit_code == EXIT_USAGE
    # Click writes the Choice usage error to stderr on newer Click; combine.
    assert "tcsh" in (result.output or "") + (result.stderr or "")


def test_completion_requires_a_shell_argument() -> None:
    result = CliRunner().invoke(main, ["completion"])
    assert result.exit_code == EXIT_USAGE
