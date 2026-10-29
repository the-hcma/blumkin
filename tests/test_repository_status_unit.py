"""Is the running blumkin behind the checkout it is used next to? (issue #547)"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_SUCCESS
from blumkin.repository_status import (
    checkout_commands,
    current_status,
    find_repository,
    repository_status,
)

_ROOT = Path(__file__).resolve().parents[1]
_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def _git_env() -> dict[str, str]:
    """The environment for the fixtures' git: no user config, hooks or inherited GIT_* state."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    return env


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
        [*_GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


def _make_repo(path: Path, cli: str = _CLI, name: str = "blumkin") -> Path:
    (path / "src" / "blumkin").mkdir(parents=True)
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    (path / "src" / "blumkin" / "cli.py").write_text(cli, encoding="utf-8")
    subprocess.run([*_GIT, "init", "-q", str(path)], check=True, env=_git_env())
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


def test_being_behind_alone_is_not_worth_a_note(tmp_path: Path) -> None:
    """A release is always some commits behind main; only missing commands are actionable."""
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    status = repository_status(repo, build_commit=old, running_commands=checkout_commands(repo))
    assert status.relation == "behind"
    assert status.note() is None


def test_missing_commands_are_the_note_and_it_offers_a_release_or_the_checkout(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    status = repository_status(repo, build_commit=old, running_commands=["auth", "doctor"])
    note = status.note() or ""
    assert status.missing_commands == ("browser", "sync", "upload-files")
    assert "lacks commands" in note and "browser" in note
    assert "1 commit behind" in note
    assert "blumkin upgrade" in note
    assert f"uv tool install -e {repo}" in note


def test_a_build_newer_than_the_checkout_points_at_the_checkout_not_the_install(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path / "r")
    first = _git(repo, "rev-parse", "HEAD")
    build = _commit(repo, "second")
    _git(repo, "checkout", "-q", first)

    status = repository_status(repo, build_commit=build[:12], running_commands=["auth"])

    assert status.relation == "ahead"
    note = status.note() or ""
    assert "git pull" in note
    assert "uv tool install" not in note


def test_diverged_history_is_reported_in_the_block_but_has_no_note_of_its_own(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path / "r")
    base = _git(repo, "rev-parse", "HEAD")
    side = _commit(repo, "side")
    _git(repo, "checkout", "-q", base)
    _commit(repo, "other")
    status = repository_status(
        repo, build_commit=side[:12], running_commands=checkout_commands(repo)
    )
    assert status.relation == "diverged"
    assert status.note() is None


@pytest.mark.parametrize("commit", ["a", "abc", "zzzzzzzz", "-x", "--help", "HEAD", ""])
def test_a_build_commit_that_is_not_a_real_short_hash_is_never_matched(
    tmp_path: Path, commit: str
) -> None:
    repo = _make_repo(tmp_path / "r")
    status = repository_status(repo, build_commit=commit, running_commands=checkout_commands(repo))
    assert status.relation == "unknown"
    assert status.note() is None


def test_a_build_commit_the_checkout_does_not_know_is_unknown_and_quiet(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    status = repository_status(
        repo, build_commit="deadbeefcafe", running_commands=checkout_commands(repo)
    )
    assert status.relation == "unknown"
    assert status.behind_by is None
    assert status.note() is None


def test_a_shallow_checkout_is_unknown_not_diverged(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    _commit(repo, "third")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)],
        check=True,
        env=_git_env(),
    )
    status = repository_status(
        shallow, build_commit=old, running_commands=checkout_commands(shallow)
    )
    assert status.relation == "unknown"


def test_a_checkout_that_is_a_linked_worktree_is_found_and_compared(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "side")
    assert (linked / ".git").is_file()
    assert find_repository(start=linked, environ={}) == linked
    head = _git(linked, "rev-parse", "HEAD")[:12]
    assert repository_status(linked, build_commit=head, running_commands=[]).relation == "same"


def test_a_missing_git_binary_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path / "r")

    def no_git(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr("blumkin.repository_status.subprocess.run", no_git)
    status = repository_status(repo, build_commit="abcdef1", running_commands=["auth"])
    assert status.relation == "unknown"
    assert status.head is None
    assert status.missing_commands != ()


def test_git_is_run_without_inherited_repository_state_or_user_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    seen: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.run

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen.append((argv, kwargs.get("env") or {}))
        return real(argv, **kwargs)

    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    monkeypatch.setenv("GIT_WORK_TREE", "/nonexistent")
    monkeypatch.setattr("blumkin.repository_status.subprocess.run", spy)
    head = _git(repo, "rev-parse", "HEAD")[:12] if False else "abcdef1"
    status = repository_status(repo, build_commit=head, running_commands=["auth"])
    assert status.head is not None  # the inherited GIT_DIR did not redirect git
    assert seen
    for argv, env in seen:
        assert "core.fsmonitor=false" in argv
        assert "core.hooksPath=/dev/null" in argv
        assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
        assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert env.get("GIT_TERMINAL_PROMPT") == "0"


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
            running_from=repo,
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
        running_from=None,
    )
    assert status is not None and status.path == repo


def test_with_no_repository_around_there_is_nothing_to_say(tmp_path: Path) -> None:
    assert (
        current_status(
            running_commands=[], start=tmp_path, environ={}, build_commit="abc", running_from=None
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
    assert lines[2].startswith("note: ") and "lacks commands" in lines[2]
    assert "blumkin upgrade" in lines[2]


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
    assert any("lacks commands" in warning for warning in payload["warnings"])


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


def test_doctor_reports_being_behind_without_a_warning_when_nothing_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    _installed_elsewhere(monkeypatch, repo, old)
    monkeypatch.setattr(
        "blumkin.repository_status.checkout_commands", lambda r: tuple(main.commands)
    )
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(
        '[profiles.default]\nclient_id = "fake.apps.googleusercontent.com"\nprovider = "google"\n'
        'default_tz = "UTC"\n',
        encoding="utf-8",
    )

    payload = json.loads(CliRunner().invoke(main, ["doctor", "--json"]).output)

    assert payload["repository"]["relation"] == "behind"
    assert not any("lacks commands" in warning for warning in payload["warnings"])


def test_git_ignores_the_users_global_and_system_configuration_and_never_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    seen: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.run

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen.append((argv, kwargs.get("env") or {}))
        return real(argv, **kwargs)

    monkeypatch.setattr("blumkin.repository_status.subprocess.run", spy)
    repository_status(repo, build_commit="abcdef1", running_commands=[])
    assert seen
    for argv, env in seen:
        assert env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
        assert env.get("GIT_CONFIG_SYSTEM") == "/dev/null"
        assert env.get("GIT_NO_LAZY_FETCH") == "1"
        for protocol in ("", "http.", "https.", "ssh.", "git.", "file."):
            assert f"protocol.{protocol}allow=never" in argv


def test_a_broken_user_git_configuration_does_not_break_the_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[broken\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    head = _git(repo, "rev-parse", "HEAD")[:12]
    status = repository_status(repo, build_commit=head, running_commands=checkout_commands(repo))
    assert status.relation == "same"


def test_the_suggested_command_quotes_a_checkout_path_with_spaces(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "My Projects" / "blum kin; echo hi")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    note = repository_status(repo, build_commit=old, running_commands=["auth"]).note() or ""
    command = note.split("`uv tool install -e ", 1)[1].split("`", 1)[0]
    assert shlex.split(command) == [str(repo)]


def _failing(*probes: str, code: int = 128) -> Any:
    """A subprocess.run that makes the git commands named in `probes` fail with `code`."""
    real = subprocess.run

    def run(argv: list[str], **kwargs: Any) -> Any:
        if any(probe in argv for probe in probes):
            return subprocess.CompletedProcess(argv, code, stdout="", stderr="boom")
        return real(argv, **kwargs)

    return run


def test_a_failed_ancestry_probe_is_unknown_not_diverged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    monkeypatch.setattr("blumkin.repository_status.subprocess.run", _failing("merge-base"))
    status = repository_status(repo, build_commit=old, running_commands=[])
    assert status.relation == "unknown"
    assert status.behind_by is None


def test_a_failed_commit_count_is_unknown_not_zero_commits_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    monkeypatch.setattr("blumkin.repository_status.subprocess.run", _failing("rev-list"))
    status = repository_status(repo, build_commit=old, running_commands=["auth"])
    assert status.relation == "unknown"
    assert "0 commit" not in (status.note() or "")


def test_a_failed_shallow_probe_is_not_taken_as_evidence_of_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "r")
    base = _git(repo, "rev-parse", "HEAD")
    side = _commit(repo, "side")
    _git(repo, "checkout", "-q", base)
    _commit(repo, "other")
    monkeypatch.setattr(
        "blumkin.repository_status.subprocess.run", _failing("--is-shallow-repository")
    )
    assert (
        repository_status(repo, build_commit=side[:12], running_commands=[]).relation == "unknown"
    )


def test_the_note_names_the_pipx_alternative_to_the_uv_checkout_install(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    note = repository_status(repo, build_commit=old, running_commands=["auth"]).note() or ""
    assert "pipx install --editable" in note


def test_the_agent_docs_keep_the_missing_command_guidance_inside_the_copied_block() -> None:
    text = (Path(__file__).parent.parent / "docs" / "agent-integration.md").read_text("utf-8")
    block = text.split("```markdown", 1)[1].split("\n```\n", 1)[0]
    assert "A command is missing" in block


def test_the_agent_guidance_says_upgrade_changes_the_install_and_needs_approval() -> None:
    root = Path(__file__).parent.parent
    for relative in (".agents/skills/blumkin/SKILL.md", "docs/agent-integration.md"):
        text = (root / relative).read_text("utf-8")
        line = next(item for item in text.splitlines() if "older than the repository" in item)
        assert "prints the" not in line, relative
        assert "approval" in line, relative


def test_on_windows_the_note_does_not_apply_posix_quoting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path / "My Projects" / "r")
    old = _git(repo, "rev-parse", "HEAD")[:12]
    _commit(repo, "second")
    monkeypatch.setattr("blumkin.repository_status._is_windows", lambda: True)
    note = repository_status(repo, build_commit=old, running_commands=["auth"]).note() or ""
    assert "'" not in note
    assert f"at {repo} has" in note
    assert "uv tool install -e PATH" in note


def test_the_agent_skill_says_editable_installs_need_upgrade_yes() -> None:
    text = (Path(__file__).parent.parent / ".agents/skills/blumkin/SKILL.md").read_text("utf-8")
    line = next(item for item in text.splitlines() if "older than the repository" in item)
    assert "upgrade --yes" in line and "editable" in line


def test_the_agent_docs_make_only_the_warning_conditional_on_missing_commands() -> None:
    text = (Path(__file__).parent.parent / "docs" / "agent-integration.md").read_text("utf-8")
    line = next(item for item in text.splitlines() if item.startswith("- **A command is missing"))
    assert "plus a warning when the checkout has commands" in line


def test_the_readme_points_at_doctor_json_for_the_repository_block() -> None:
    text = (Path(__file__).parent.parent / "README.md").read_text("utf-8")
    assert "`blumkin doctor --json` reports the full comparison under `repository`" in text
