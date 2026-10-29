"""`blumkin completion <shell>` prints a usable script (issue #98) or installs it (#167)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.shell_completion import BashComplete
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


def test_completion_install_ignores_a_relative_xdg_dir(monkeypatch, tmp_path: Path) -> None:
    # XDG spec: a relative base-dir value is invalid and must be ignored. Chdir
    # into tmp_path so the fallback (~/.local/share) is the only writable option
    # we can assert on without polluting CWD.
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["completion", "bash", "--install", "--json"])
    assert result.exit_code == EXIT_SUCCESS
    assert json.loads(result.stdout)["path"] == str(
        tmp_path / ".local" / "share" / "bash-completion" / "completions" / "blumkin.bash"
    )
    assert not (tmp_path / "relative").exists()


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


@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo is POSIX-only")
def test_completion_install_rejects_a_non_regular_target(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    target = tmp_path / "bash-completion" / "completions" / "blumkin.bash"
    target.parent.mkdir(parents=True)
    os.mkfifo(target)
    result = CliRunner().invoke(main, ["completion", "bash", "--install", "--force", "--json"])
    assert result.exit_code == EXIT_OTHER
    payload = json.loads(result.stderr)
    assert payload["error"] == "install_failed"
    assert "not a regular file" in payload["message"]


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


def _complete(words: str) -> list[str]:
    result = CliRunner().invoke(
        main,
        [],
        env={
            "_BLUMKIN_COMPLETE": "bash_complete",
            "COMP_WORDS": words,
            "COMP_CWORD": str(len(words.split()) - (0 if words.endswith(" ") else 1)),
        },
        prog_name="blumkin",
    )
    return [line.split(",", 1)[-1] for line in result.output.splitlines() if line]


def test_the_browser_group_and_its_commands_complete() -> None:
    assert "browser" in _complete("blumkin br")
    commands = _complete("blumkin browser ")
    for name in ("launch", "profile", "status"):
        assert name in commands


def test_the_browser_profile_subcommands_complete() -> None:
    assert {"create", "delete", "list"} <= set(_complete("blumkin browser profile "))


# --------------------------------------------------------------------------- surface audit (#567)


def _visible_commands(group: click.Group, ctx: click.Context) -> list[tuple[str, click.Command]]:
    found = []
    for name in group.list_commands(ctx):
        sub = group.get_command(ctx, name)
        if sub is not None and not getattr(sub, "hidden", False):
            found.append((name, sub))
    return found


def _walk(command: click.Command, path: list[str]) -> list[tuple[list[str], click.Command]]:
    """Every visible command under `command`, groups and leaves, with its path."""
    found = [(path, command)]
    if isinstance(command, click.Group):
        ctx = click.Context(command, info_name=path[-1] if path else "blumkin")
        for name, sub in _visible_commands(command, ctx):
            found.extend(_walk(sub, [*path, name]))
    return found


def _completions(args: list[str], incomplete: str) -> set[str]:
    completer = BashComplete(main, {}, "blumkin", "_BLUMKIN_COMPLETE")
    return {item.value for item in completer.get_completions(args, incomplete)}


def test_every_group_completes_exactly_its_subcommands() -> None:
    for path, command in _walk(main, []):
        if not isinstance(command, click.Group):
            continue
        ctx = click.Context(command, info_name="x")
        expected = {name for name, _ in _visible_commands(command, ctx)}
        assert _completions(path, "") == expected, " ".join(path) or "blumkin"


def test_every_command_completes_all_of_its_long_options() -> None:
    for path, command in _walk(main, []):
        if isinstance(command, click.Group):
            continue
        expected = {
            opt
            for param in command.params
            if isinstance(param, click.Option) and not param.hidden
            for opt in (*param.opts, *param.secondary_opts)
            if opt.startswith("--")
        }
        ctx = click.Context(command, info_name="x")
        expected.update(opt for opt in command.get_help_option_names(ctx) if opt.startswith("--"))
        assert _completions(path, "--") == expected, " ".join(path)


def test_every_choice_option_completes_its_values() -> None:
    checked = 0
    for path, command in _walk(main, []):
        for param in command.params:
            if isinstance(param, click.Option) and isinstance(param.type, click.Choice):
                option = next(opt for opt in param.opts if opt.startswith("--"))
                values = _completions([*path, option], "")
                assert values == set(param.type.choices), (" ".join(path), option)
                checked += 1
    assert checked > 0


def test_the_bash_script_only_asks_for_nosort_where_bash_supports_it() -> None:
    """Click's template runs `complete -o nosort` unconditionally; stock macOS bash 3.2 rejects
    it with `invalid option name` and registers no completion at all (#567).
    """
    script = CliRunner().invoke(main, ["completion", "bash"]).output
    assert "BASH_VERSINFO" in script
    assert "complete -o nosort" not in script
    assert "$nosort_option" in script


def _register_with(script: str, version: tuple[int, int, int]) -> list[str]:
    """Run the script in bash with a fake `complete` and a pretend bash version; return its args."""
    pretend = script.replace("BASH_VERSINFO", "PRETEND_BASH_VERSINFO")
    harness = (
        f"PRETEND_BASH_VERSINFO=({version[0]} {version[1]} {version[2]})\n"
        "complete() { printf '%s\\n' \"$@\"; }\n" + pretend
    )
    done = subprocess.run(
        ["bash", "-c", harness], capture_output=True, check=False, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def test_the_bash_script_registers_without_nosort_on_bash_3_2_and_with_it_on_4_4() -> None:
    script = CliRunner().invoke(main, ["completion", "bash"]).output
    old = _register_with(script, (3, 2, 57))
    assert "-F" in old and "nosort" not in old
    assert "-o" in _register_with(script, (4, 4, 0))
    assert "nosort" in _register_with(script, (5, 3, 0))
    assert "nosort" not in _register_with(script, (4, 3, 48))


def test_the_bash_script_is_valid_bash() -> None:
    script = CliRunner().invoke(main, ["completion", "bash"]).output
    done = subprocess.run(
        ["bash", "-n"], input=script, capture_output=True, check=False, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr


def test_every_command_has_help_an_examples_section_and_help_on_every_option() -> None:
    """New commands must come with help (#567); nothing is exempt on purpose today."""
    problems = []
    for path, command in _walk(main, []):
        if isinstance(command, click.Group) or not path:
            continue
        name = " ".join(path)
        if not (command.help or "").strip():
            problems.append(f"{name}: no help text")
        if "xample" not in (command.epilog or ""):
            problems.append(f"{name}: no Examples section")
        problems.extend(
            f"{name} {param.opts[0]}: no help text"
            for param in command.params
            if isinstance(param, click.Option) and not (param.help or "").strip()
        )
    assert not problems, "\n".join(problems)
