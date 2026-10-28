"""`blumkin browser init-task`: a built-in task template, written only on request (#545)."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.profiles import list_profiles
from blumkin.cli import main
from blumkin.config import load_browser_setup
from blumkin.exit_codes import EXIT_SUCCESS, EXIT_USAGE

_BASE = '[profiles.default]\nclient_id = "abc-123"\n'


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(directory))
    (directory / "config.toml").write_text(_BASE, encoding="utf-8")
    return directory


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", "browser", "init-task", *args])


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def test_without_yes_it_prints_the_table_and_leaves_config_toml_alone(config: Path) -> None:
    before = (config / "config.toml").read_text(encoding="utf-8")

    result = _run("--template", "teams-transcript")

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _payload(result)
    assert payload["written"] is False
    assert payload["task"] == "teams-transcript"
    assert payload["table"].startswith("[browser.tasks.teams-transcript]")
    assert (config / "config.toml").read_text(encoding="utf-8") == before


def test_it_creates_the_tasks_browser_profile_either_way(config: Path) -> None:
    assert _run("--template", "teams-transcript").exit_code == EXIT_SUCCESS
    assert [profile.task for profile in list_profiles(config)] == ["teams-transcript"]


def test_the_table_declares_the_teams_and_sharepoint_hosts_and_only_navigation_controls(
    config: Path,
) -> None:
    table = tomllib.loads(_payload(_run("--template", "teams-transcript"))["table"])
    policy = table["browser"]["tasks"]["teams-transcript"]
    assert "*.sharepoint.com" in policy["allowed_hosts"]
    assert any(host.startswith("teams.") for host in policy["allowed_hosts"])
    assert policy["allowed_controls"] == ["Notes", "Transcript"]
    assert set(policy) == {"allowed_controls", "allowed_hosts"}


def test_with_yes_the_table_is_appended_and_the_task_policy_loads(config: Path) -> None:
    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _payload(result)["written"] is True
    text = (config / "config.toml").read_text(encoding="utf-8")
    assert text.startswith(_BASE)
    policy = load_browser_setup().browser.tasks["teams-transcript"]
    assert "*.sharepoint.com" in policy.allowed_hosts
    assert policy.allowed_controls == ("Notes", "Transcript")


def test_with_yes_and_no_config_file_it_creates_one_that_only_the_user_can_read(
    config: Path,
) -> None:
    (config / "config.toml").unlink()

    assert _run("--template", "teams-transcript", "--yes").exit_code == EXIT_SUCCESS

    mode = stat.S_IMODE((config / "config.toml").stat().st_mode)
    assert mode == 0o600


def test_a_second_run_does_not_duplicate_or_change_the_table(config: Path) -> None:
    _run("--template", "teams-transcript", "--yes")
    once = (config / "config.toml").read_text(encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _payload(result)
    assert payload["written"] is False
    assert payload["already_declared"] is True
    assert (config / "config.toml").read_text(encoding="utf-8") == once


def test_an_existing_task_policy_of_the_same_name_is_never_overwritten(config: Path) -> None:
    custom = _BASE + '\n[browser.tasks.teams-transcript]\nallowed_controls = ["Mine"]\n'
    (config / "config.toml").write_text(custom, encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert _payload(result)["already_declared"] is True
    assert (config / "config.toml").read_text(encoding="utf-8") == custom


def test_the_task_can_have_another_name(config: Path) -> None:
    result = _run("weekly-sync", "--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "weekly-sync" in load_browser_setup().browser.tasks
    assert [profile.task for profile in list_profiles(config)] == ["weekly-sync"]


def test_a_task_name_that_is_not_valid_is_a_usage_error(config: Path) -> None:
    result = _run("Not Valid", "--template", "teams-transcript", "--yes")
    assert result.exit_code == EXIT_USAGE, result.output
    assert (config / "config.toml").read_text(encoding="utf-8") == _BASE


def test_a_template_that_does_not_exist_is_a_usage_error(config: Path) -> None:
    assert _run("--template", "nope").exit_code == EXIT_USAGE


def test_a_config_file_that_is_not_valid_toml_is_refused_and_left_alone(config: Path) -> None:
    (config / "config.toml").write_text("this is [not toml", encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert (config / "config.toml").read_text(encoding="utf-8") == "this is [not toml"


def test_a_config_whose_browser_table_cannot_take_the_new_one_is_left_alone(config: Path) -> None:
    clash = _BASE + "\n[browser]\ntasks = 5\n"
    (config / "config.toml").write_text(clash, encoding="utf-8")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert (config / "config.toml").read_text(encoding="utf-8") == clash


def test_a_config_file_that_is_a_symlink_is_not_written_through(
    config: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.toml"
    target.write_text(_BASE, encoding="utf-8")
    (config / "config.toml").unlink()
    os.symlink(target, config / "config.toml")

    result = _run("--template", "teams-transcript", "--yes")

    assert result.exit_code == EXIT_USAGE, result.output
    assert target.read_text(encoding="utf-8") == _BASE


def test_the_next_steps_name_the_commands_to_run_and_never_a_password_or_token(
    config: Path,
) -> None:
    steps = " ".join(_payload(_run("--template", "teams-transcript"))["next_steps"])
    for command in ("browser launch teams-transcript", "browser transcript teams-transcript"):
        assert command in steps
    assert "sign in" in steps.lower()


def test_the_human_output_shows_the_table_and_how_to_write_it(config: Path) -> None:
    result = CliRunner().invoke(main, ["browser", "init-task", "--template", "teams-transcript"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "[browser.tasks.teams-transcript]" in result.output
    assert "--yes" in result.output


def test_the_template_names_complete(config: Path) -> None:
    from click.testing import CliRunner as Runner

    result = Runner().invoke(
        main,
        [],
        env={
            "_BLUMKIN_COMPLETE": "bash_complete",
            "COMP_WORDS": "blumkin browser init-task --template ",
            "COMP_CWORD": "4",
        },
        prog_name="blumkin",
    )
    assert "teams-transcript" in result.output
