"""Is the running blumkin behind the checkout it is used next to? (issue #547)"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from blumkin.repository_status import (
    checkout_commands,
    current_status,
    find_repository,
    repository_status,
)
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_SUCCESS

_ROOT = Path(__file__).resolve().parents[1]
_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
_CLI = """import click

@click.group()
def main(): ...

@main.group(epilog="x")
def auth(): ...

@main.group("browser", epilog="x")
def browser_group(): ...

@main.command(name="doctor")
def doctor_cmd(): ...

@main.command()
def upload_files(): ...

@main.group()
def sync_group(): ...

def helper(): ...

@auth.command("login")
def login(): ...
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*_GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_repo(path: Path, cli: str = _CLI, name: str = "blumkin") -> Path:
    (path / "src" / "blumkin").mkdir(parents=True)
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    (path / "src" / "blumkin" / "cli.py").write_text(cli, encoding="utf-8")
    subprocess.run([*_GIT, "init", "-q", str(path)], check=True)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "first")
    return path


def _commit(repo: Path, message: str) -> str:
    (repo / "note.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_the_scan_names_the_top_level_commands_the_way_click_does(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    assert checkout_commands(repo) == (
        "auth",
        "browser",
        "doctor",
        "sync",
        "upload-files",
    )


def test_the_scan_agrees_with_the_real_command_list() -> None:
    assert set(checkout_commands(_ROOT)) == set(main.commands)


def test_the_scan_of_a_missing_or_broken_file_is_empty(tmp_path: Path) -> None:
    assert checkout_commands(tmp_path) == ()
    (tmp_path / "src" / "blumkin").mkdir(parents=True)
    (tmp_path / "src" / "blumkin" / "cli.py").write_text("def broken(:\n", encoding="utf-8")
    assert checkout_commands(tmp_path) == ()


def test_a_repository_is_found_from_the_environment_or_by_walking_up(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    nested = repo / "src" / "blumkin"
    assert find_repository(start=nested, environ={}) == repo
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert find_repository(start=elsewhere, environ={}) is None
    assert find_repository(start=elsewhere, environ={"BLUMKIN_REPOSITORY": str(repo)}) == repo


def test_a_directory_that_is_not_a_blumkin_checkout_is_not_a_repository(tmp_path: Path) -> None:
    other = _make_repo(tmp_path / "other", name="something-else")
    assert find_repository(start=other, environ={}) is None
    assert find_repository(start=other, environ={"BLUMKIN_REPOSITORY": str(other)}) is None


def test_the_same_commit_is_not_behind(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    head = _git(repo, "rev-parse", "HEAD")
    status = repository_status(
        repo, build_commit=head[:12], running_commands=checkout_commands(repo)
    )
    assert status.relation == "same"
    assert status.behind_by == 0
    assert status.missing_commands == ()
    assert status.note() is None


def test_an_older_build_is_behind_by_the_commits_since(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    _commit(repo, "third")
    status = repository_status(repo, build_commit=old, running_commands=checkout_commands(repo))
    assert status.relation == "behind"
    assert status.behind_by == 2
    assert "2 commits behind" in (status.note() or "")
    assert "blumkin upgrade" in (status.note() or "")


def test_commands_the_checkout_has_and_the_running_cli_lacks_are_listed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    head = _git(repo, "rev-parse", "HEAD")[:12]
    status = repository_status(repo, build_commit=head, running_commands=["auth", "doctor"])
    assert status.missing_commands == ("browser", "sync", "upload-files")
    assert "browser" in (status.note() or "")


def test_a_newer_build_than_the_checkout_is_ahead_and_not_a_problem(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "second")
    _git(repo, "checkout", "-q", old)
    newer = "0" * 12
    status = repository_status(repo, build_commit=newer, running_commands=checkout_commands(repo))
    assert status.relation == "unknown"
    assert status.note() is not None


def test_a_build_commit_the_checkout_does_not_know_is_reported_as_not_comparable(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path / "r")
    status = repository_status(
        repo, build_commit="deadbeefcafe", running_commands=checkout_commands(repo)
    )
    assert status.relation == "unknown"
    assert status.behind_by is None
    assert "not in" in (status.note() or "")


def test_an_unknown_build_commit_is_reported_without_guessing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    status = repository_status(repo, build_commit="unknown", running_commands=["auth"])
    assert status.relation == "unknown"
    assert status.missing_commands != ()


def test_a_directory_that_is_not_a_git_repository_never_raises(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    (plain / "src" / "blumkin").mkdir(parents=True)
    (plain / "pyproject.toml").write_text('[project]\nname = "blumkin"\n', encoding="utf-8")
    status = repository_status(plain, build_commit="abc", running_commands=[])
    assert status.relation == "unknown"
    assert status.head is None


def test_the_payload_is_plain_data(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    head = _git(repo, "rev-parse", "HEAD")
    payload = repository_status(repo, build_commit=head[:12], running_commands=[]).as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["path"] == str(repo)
    assert payload["head"] == head[:12]


def test_a_cli_running_from_the_checkout_itself_is_never_compared(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    assert (
        current_status(
            running_commands=[],
            start=repo,
            environ={},
            build_commit="deadbeefcafe",
            source_tree=repo,
        )
        is None
    )


def test_an_installed_cli_is_compared_with_the_checkout_found_from_the_working_directory(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path / "r")
    status = current_status(
        running_commands=["auth"],
        start=repo / "src",
        environ={},
        build_commit=_git(repo, "rev-parse", "HEAD")[:12],
        source_tree=None,
    )
    assert status is not None and status.path == repo


def test_with_no_repository_around_there_is_nothing_to_say(tmp_path: Path) -> None:
    assert (
        current_status(
            running_commands=[], start=tmp_path, environ={}, build_commit="abc", source_tree=None
        )
        is None
    )


def _installed_elsewhere(monkeypatch: pytest.MonkeyPatch, repo: Path, build: str) -> None:
    """Make the CLI look like an installed release that is used next to `repo`."""
    monkeypatch.setenv("BLUMKIN_REPOSITORY", str(repo))
    monkeypatch.setattr("blumkin.repository_status.source_tree", lambda: None)
    monkeypatch.setattr("blumkin.repository_status.git_commit", lambda: build)


def test_version_adds_one_line_when_the_install_is_behind_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    _installed_elsewhere(monkeypatch, repo, old)

    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 3
    assert "behind" in lines[2] and "blumkin upgrade" in lines[2]


def test_version_stays_two_lines_when_the_install_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    head = _git(repo, "rev-parse", "HEAD")[:12]
    _installed_elsewhere(monkeypatch, repo, head)
    monkeypatch.setattr(
        "blumkin.repository_status.checkout_commands", lambda r: tuple(main.commands)
    )

    result = CliRunner().invoke(main, ["--version"])

    assert len(result.output.strip().splitlines()) == 2


def test_doctor_json_carries_the_repository_block_and_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    _installed_elsewhere(monkeypatch, repo, old)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(
        '[profiles.default]\nclient_id = "fake.apps.googleusercontent.com"\nprovider = "google"\n'
        'default_tz = "UTC"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--json"])

    payload: dict[str, Any] = json.loads(result.output)
    assert payload["repository"]["relation"] == "behind"
    assert payload["repository"]["behind_by"] == 1
    assert any("behind the checkout" in warning for warning in payload["warnings"])


def test_doctor_json_has_a_null_repository_when_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BLUMKIN_REPOSITORY", raising=False)
    monkeypatch.setattr("blumkin.repository_status.source_tree", lambda: None)
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(
        '[profiles.default]\nclient_id = "fake.apps.googleusercontent.com"\nprovider = "google"\n'
        'default_tz = "UTC"\n',
        encoding="utf-8",
    )

    payload = json.loads(CliRunner().invoke(main, ["doctor", "--json"]).output)

    assert payload["repository"] is None
