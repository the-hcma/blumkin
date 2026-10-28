"""Guided first-run setup: `auth setup` creates a missing config safely (issue #524)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin import cli as cli_module
from blumkin import secret_store
from blumkin.auth_setup import FirstRunConfigError, create_first_run_config
from blumkin.cli import main
from blumkin.config import load_config
from blumkin.exit_codes import EXIT_SUCCESS, EXIT_USAGE

_GUID = "12345678-1234-1234-1234-123456789012"
_MS_FLAGS = [
    "--client-id",
    _GUID,
    "--tenant-id",
    "contoso.onmicrosoft.com",
    "--account-type",
    "organizational",
]


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


@pytest.fixture
def fresh_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "does" / "not" / "exist" / "blumkin"
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(target))
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    return target


def test_a_missing_config_is_created_private_with_one_profile(tmp_path: Path) -> None:
    directory = tmp_path / "a" / "b" / "blumkin"
    path = create_first_run_config(directory, profile="default", provider="microsoft")
    assert path == directory / "config.toml"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == '[profiles.default]\nprovider = "microsoft"\n'


def test_an_existing_config_is_never_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / "blumkin"
    directory.mkdir()
    existing = directory / "config.toml"
    existing.write_text("# mine\n")
    with pytest.raises(FirstRunConfigError, match="already exists"):
        create_first_run_config(directory, profile="default", provider="google")
    assert existing.read_text() == "# mine\n"


def test_an_existing_directory_keeps_its_permissions(tmp_path: Path) -> None:
    directory = tmp_path / "blumkin"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    create_first_run_config(directory, profile="default", provider="google")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_a_symlink_to_an_existing_directory_is_used_as_it_is(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "blumkin"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    create_first_run_config(link, profile="default", provider="google")
    assert (real / "config.toml").read_text() == '[profiles.default]\nprovider = "google"\n'


def test_a_dangling_symlink_or_a_file_in_the_way_is_refused_cleanly(tmp_path: Path) -> None:
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(FirstRunConfigError, match="Expected"):
        create_first_run_config(dangling, profile="default", provider="google")
    in_the_way = tmp_path / "file"
    in_the_way.write_text("x")
    with pytest.raises(FirstRunConfigError, match="Expected"):
        create_first_run_config(in_the_way, profile="default", provider="google")


@pytest.mark.parametrize(
    ("profile", "provider"),
    [("", "google"), ("a b", "google"), ("../x", "google"), ("default", "yahoo"), ("default", "")],
)
def test_a_bad_profile_name_or_provider_is_refused_before_anything_is_written(
    tmp_path: Path, profile: str, provider: str
) -> None:
    directory = tmp_path / "blumkin"
    with pytest.raises(FirstRunConfigError, match="Expected"):
        create_first_run_config(directory, profile=profile, provider=provider)
    assert not directory.exists()


def test_without_a_config_and_without_a_terminal_setup_says_what_to_pass(fresh_dir: Path) -> None:
    result = CliRunner().invoke(main, ["auth", "setup", "--yes", *_MS_FLAGS, "--json"])
    assert result.exit_code == EXIT_USAGE
    body = json.loads(result.output)
    assert body["error"] == "usage_error"
    assert "--provider" in body["message"] and str(fresh_dir) in body["message"]
    assert not fresh_dir.exists()


def test_setup_with_a_provider_creates_the_config_and_finishes(fresh_dir: Path) -> None:
    result = CliRunner().invoke(
        main, ["auth", "setup", "--yes", "--provider", "microsoft", *_MS_FLAGS, "--json"]
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert stat.S_IMODE(fresh_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((fresh_dir / "config.toml").stat().st_mode) == 0o600
    written = load_config()
    assert written.client_id == _GUID and written.tenant_id == "contoso.onmicrosoft.com"
    assert json.loads(result.output)["created_config"] == str(fresh_dir / "config.toml")


def test_running_setup_again_does_not_recreate_or_clobber(fresh_dir: Path) -> None:
    args = ["auth", "setup", "--yes", "--provider", "microsoft", *_MS_FLAGS, "--json"]
    CliRunner().invoke(main, args)
    first = (fresh_dir / "config.toml").read_text()
    again = CliRunner().invoke(main, args)
    assert again.exit_code == EXIT_SUCCESS, again.output
    assert "created_config" not in json.loads(again.output)
    assert (fresh_dir / "config.toml").read_text() == first


def test_a_provider_flag_that_disagrees_with_the_existing_profile_is_refused(
    fresh_dir: Path,
) -> None:
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "config.toml").write_text('[profiles.default]\nprovider = "google"\n')
    result = CliRunner().invoke(
        main, ["auth", "setup", "--yes", "--provider", "microsoft", *_MS_FLAGS, "--json"]
    )
    assert result.exit_code == EXIT_USAGE
    assert "google" in json.loads(result.output)["message"]


def test_the_provider_is_asked_for_on_a_terminal_and_only_when_it_is_needed() -> None:
    asked: list[str] = []

    def prompt(text: str, **kwargs: Any) -> str:
        asked.append(text)
        return "google"

    resolve = cli_module._first_run_provider
    assert resolve("microsoft", tty=True, yes=False, prompt=prompt) == "microsoft"
    assert asked == []
    assert resolve(None, tty=True, yes=False, prompt=prompt) == "google"
    assert len(asked) == 1
    assert resolve(None, tty=True, yes=True, prompt=prompt) is None
    assert resolve(None, tty=False, yes=False, prompt=prompt) is None


def test_a_setup_that_fails_validation_leaves_no_config_behind(fresh_dir: Path) -> None:
    bad = CliRunner().invoke(
        main,
        [
            "auth",
            "setup",
            "--yes",
            "--provider",
            "microsoft",
            "--client-id",
            "not-a-guid",
            "--tenant-id",
            "contoso.onmicrosoft.com",
            "--account-type",
            "organizational",
            "--json",
        ],
    )
    assert bad.exit_code == EXIT_USAGE
    assert not fresh_dir.exists()
    good = CliRunner().invoke(main, ["auth", "setup", "--yes", "--provider", "google", "--json"])
    assert "match" not in json.loads(good.output).get("message", "")


def test_an_interrupted_setup_leaves_no_config_behind(
    fresh_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import click

    def abort(*args: Any, **kwargs: Any) -> None:
        raise click.Abort()

    monkeypatch.setattr(cli_module, "_collect_microsoft_setup", abort)
    result = CliRunner().invoke(
        main, ["auth", "setup", "--yes", "--provider", "microsoft", *_MS_FLAGS]
    )
    assert result.exit_code != 0
    assert not fresh_dir.exists()


def test_the_rollback_keeps_a_directory_that_was_already_there(
    fresh_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "notes.txt").write_text("mine")
    result = CliRunner().invoke(
        main,
        ["auth", "setup", "--yes", "--provider", "microsoft", "--client-id", "bad", *_MS_FLAGS[2:]],
    )
    assert result.exit_code == EXIT_USAGE
    assert (fresh_dir / "notes.txt").read_text() == "mine"
    assert not (fresh_dir / "config.toml").exists()


def test_a_failed_write_leaves_no_partial_config_and_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    def full_disk(fd: int) -> None:
        raise OSError("no space left")

    monkeypatch.setattr(os, "fsync", full_disk)
    directory = tmp_path / "blumkin"
    with pytest.raises(FirstRunConfigError, match="Expected"):
        create_first_run_config(directory, profile="default", provider="google")
    assert not (directory / "config.toml").exists()


def test_the_profile_environment_variable_names_the_first_run_profile(
    fresh_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_PROFILE", "work")
    result = CliRunner().invoke(
        main, ["auth", "setup", "--yes", "--provider", "microsoft", *_MS_FLAGS, "--json"]
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "[profiles.work]" in (fresh_dir / "config.toml").read_text()


def test_an_explicit_profile_flag_wins_over_the_environment_variable(
    fresh_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_PROFILE", "work")
    result = CliRunner().invoke(
        main,
        ["--profile", "home", "auth", "setup", "--yes", "--provider", "microsoft", *_MS_FLAGS],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "[profiles.home]" in (fresh_dir / "config.toml").read_text()
