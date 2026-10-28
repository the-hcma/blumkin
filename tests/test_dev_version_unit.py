"""The dev version marker and the config resolution report (issue #524)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin import version as version_module
from blumkin.cli import main
from blumkin.config import config_dir, config_dir_source
from blumkin.version import dev_version_suffix, source_tree


@pytest.fixture(autouse=True)
def _clear_build_info_cache():
    version_module.get_build_info.cache_clear()
    yield
    version_module.get_build_info.cache_clear()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_DATE": "2026-10-27T12:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-10-27T12:00:00+0000",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "tracked.txt").write_text("one\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "--quiet", "--message", "first")
    return repo


def test_a_clean_checkout_gets_the_short_sha_and_the_commit_date(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert re.fullmatch(r"\+dev\.[0-9a-f]{7}\.20261027", dev_version_suffix(repository=repo))


def test_uncommitted_changes_add_dirty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("two\n")
    assert dev_version_suffix(repository=repo).endswith("-dirty")


def test_an_untracked_file_counts_as_dirty_but_an_ignored_one_does_not(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("ignored.txt\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "--quiet", "--message", "ignore")
    (repo / "ignored.txt").write_text("x\n")
    assert not dev_version_suffix(repository=repo).endswith("-dirty")
    (repo / "new.txt").write_text("x\n")
    assert dev_version_suffix(repository=repo).endswith("-dirty")


def test_a_release_install_has_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert dev_version_suffix(repository=tmp_path) == ""  # not a checkout
    repo = _repo(tmp_path)
    monkeypatch.setattr(version_module._build_metadata, "EMBEDDED_VERSION", "1.2.3")
    assert dev_version_suffix(repository=repo) == ""  # a baked-in release version wins


def test_a_missing_git_binary_means_no_marker_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(version_module.subprocess, "run", boom)
    assert dev_version_suffix(repository=repo) == ""


needs_checkout = pytest.mark.skipif(source_tree() is None, reason="needs a git checkout")


@needs_checkout
def test_the_marker_appears_only_for_a_dev_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLUMKIN_DEV", raising=False)
    package, _commit = version_module.get_build_info()
    assert "+dev" not in package  # an editable install keeps its plain version
    version_module.get_build_info.cache_clear()
    monkeypatch.setenv("BLUMKIN_DEV", "1")
    package, _commit = version_module.get_build_info()
    assert re.search(r"\+dev\.[0-9a-f]{7}\.\d{8}(-dirty)?$", package)


@needs_checkout
def test_a_dev_run_shows_the_marker_in_every_place_the_version_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLUMKIN_DEV", "1")
    result = CliRunner().invoke(main, ["--version"])
    assert re.search(r"\+dev\.[0-9a-f]{7}\.\d{8}", result.output.splitlines()[0])
    assert re.search(r"\+dev\.[0-9a-f]{7}\.\d{8}", version_module.build_version())
    assert re.search(r"\+dev\.", version_module.build_status_fields()["build_version"])


def test_git_is_asked_not_to_take_optional_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    seen: list[list[str]] = []
    real = version_module.subprocess.run

    def spy(argv: list[str], **kwargs: object):
        seen.append(list(argv))
        return real(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(version_module.subprocess, "run", spy)
    dev_version_suffix(repository=repo)
    status = [argv for argv in seen if "status" in argv]
    assert status and all("--no-optional-locks" in argv for argv in status)


@needs_checkout
def test_the_source_tree_is_the_checkout_here_and_none_without_one(tmp_path: Path) -> None:
    tree = source_tree()
    assert tree is not None and (tree / "pyproject.toml").is_file()
    assert source_tree(repository=tmp_path) is None


def test_config_dir_source_names_what_selected_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BLUMKIN_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir_source() == (tmp_path / ".config" / "blumkin", "default")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir_source() == (tmp_path / "xdg" / "blumkin", "XDG_CONFIG_HOME")
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path / "over"))
    assert config_dir_source() == (tmp_path / "over", "BLUMKIN_CONFIG_DIR")
    assert config_dir() == tmp_path / "over"


def test_doctor_reports_the_source_tree_and_how_the_config_dir_was_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[profiles.default]\n"
        'client_id = "fake-google-desktop-client.apps.googleusercontent.com"\n'
        'provider = "google"\n'
        'default_tz = "UTC"\n'
    )
    payload = json.loads(CliRunner().invoke(main, ["doctor", "--json"]).output)
    assert payload["config"]["dir"] == str(tmp_path)
    assert payload["config"]["selected_by"] == "BLUMKIN_CONFIG_DIR"
    assert payload["config"]["source_tree"] == str(source_tree())
    text = CliRunner().invoke(main, ["doctor"]).output
    assert f"config_dir: {tmp_path} (BLUMKIN_CONFIG_DIR)" in text
    assert "source_tree:" in text


def _agent_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "rust-agent" / "src").mkdir(parents=True)
    (tree / "rust-agent" / "Cargo.toml").write_text("[package]\n")
    (tree / "rust-agent" / "src" / "main.rs").write_text("fn main() {}\n")
    return tree


def test_the_agent_binary_status_says_missing_stale_or_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from blumkin.agent import paths as agent_paths

    tree = _agent_tree(tmp_path)
    binary = tmp_path / "bin" / "blumkin-agent"
    monkeypatch.setattr(agent_paths, "binary_path", lambda: binary)
    monkeypatch.setattr(agent_paths, "is_supported_platform", lambda: True)
    missing = agent_paths.binary_status(tree)
    assert (missing["present"], missing["stale"], missing["supported"]) == (False, None, True)
    binary.parent.mkdir()
    binary.write_text("binary")
    old = tree / "rust-agent" / "src" / "main.rs"
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(tree / "rust-agent" / "Cargo.toml", (1_000_000_000, 1_000_000_000))
    assert agent_paths.binary_status(tree)["stale"] is False
    os.utime(old, None)  # a source file newer than the binary
    os.utime(binary, (1_000_000_100, 1_000_000_100))
    assert agent_paths.binary_status(tree)["stale"] is True
    assert agent_paths.binary_status(None)["stale"] is None


def test_doctor_warns_in_a_checkout_when_the_agent_binary_is_missing_or_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[profiles.default]\n"
        'client_id = "fake-google-desktop-client.apps.googleusercontent.com"\n'
        'provider = "google"\n'
        'default_tz = "UTC"\n'
    )

    def run(status: dict[str, object]) -> dict:
        monkeypatch.setattr("blumkin.cli.binary_status", lambda tree: status)
        return json.loads(CliRunner().invoke(main, ["doctor", "--json"]).output)

    base = {"path": "/x/blumkin-agent", "present": True, "stale": False, "supported": True}
    quiet = run(base)
    assert quiet["agent_binary"] == base
    assert not any("blumkin-agent" in w for w in quiet["warnings"])
    missing = run({**base, "present": False, "stale": None})
    assert any("not built" in w and "uv sync" in w for w in missing["warnings"])
    stale = run({**base, "stale": True})
    assert any("older than" in w and "rust-agent" in w for w in stale["warnings"])
    unsupported = run({**base, "present": False, "stale": None, "supported": False})
    assert not any("not built" in w for w in unsupported["warnings"])
