"""`_ensure_private_owned_dir`'s symlink/ownership refusals (issue #328).

The happy path (`runtime_dir()` creating a fresh 0700 directory) is already
exercised implicitly by every test in `test_agent_client_server_unit.py`'s
`_sandboxed_runtime_dir` fixture; these tests cover the refusal branches -
the actual security control this function exists for - which nothing else
in the suite reaches (see PR #329 review).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from blumkin.agent import paths as agent_paths


@pytest.fixture
def _base_dir() -> Iterator[Path]:
    base = tempfile.mkdtemp(dir="/tmp")
    try:
        yield Path(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_runtime_dir_adopts_an_existing_directory_owned_by_this_uid(_base_dir: Path) -> None:
    existing = _base_dir / f"blumkin-agent-{os.getuid()}"
    existing.mkdir(mode=0o755)

    agent_paths._ensure_private_owned_dir(existing)

    assert existing.is_dir()
    assert (existing.stat().st_mode & 0o777) == 0o700


def test_runtime_dir_refuses_a_planted_regular_file(_base_dir: Path) -> None:
    planted = _base_dir / f"blumkin-agent-{os.getuid()}"
    planted.write_text("not a directory")

    with pytest.raises(RuntimeError, match="not a directory"):
        agent_paths._ensure_private_owned_dir(planted)


def test_runtime_dir_refuses_a_planted_symlink(_base_dir: Path) -> None:
    target = _base_dir / "elsewhere"
    target.mkdir()
    planted = _base_dir / f"blumkin-agent-{os.getuid()}"
    planted.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        agent_paths._ensure_private_owned_dir(planted)


class TestRuntimeBaseDir:
    """Issue #402: the base dir must not depend on `$TMPDIR` on macOS.

    A login-shell CLI invocation and a GUI-launched process (e.g. `blumkin
    mcp serve` spawned by an MCP client with no `TMPDIR` set) previously
    resolved to two different directories and therefore two different
    agents; `runtime_base_dir` must give both the same answer regardless of
    what (if anything) `$TMPDIR` is set to.
    """

    def test_env_override_wins_over_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLUMKIN_AGENT_RUNTIME_DIR", "/explicit/override")
        monkeypatch.setenv("TMPDIR", "/should/be/ignored")

        assert agent_paths.runtime_base_dir() == Path("/explicit/override")

    def test_darwin_ignores_tmpdir_and_uses_confstr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BLUMKIN_AGENT_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(agent_paths.sys, "platform", "darwin")
        monkeypatch.setattr(agent_paths.os, "confstr", lambda _key: "/var/folders/xx/T/")
        monkeypatch.setenv("TMPDIR", "/tmp")

        assert agent_paths.runtime_base_dir() == Path("/var/folders/xx/T/")

    def test_darwin_falls_back_when_confstr_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BLUMKIN_AGENT_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(agent_paths.sys, "platform", "darwin")

        def _raise(_key: int) -> str:
            raise ValueError("unrecognized configuration name")

        monkeypatch.setattr(agent_paths.os, "confstr", _raise)
        monkeypatch.setattr(agent_paths.tempfile, "gettempdir", lambda: "/tmp")

        assert agent_paths.runtime_base_dir() == Path("/tmp")

    def test_non_darwin_prefers_xdg_runtime_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BLUMKIN_AGENT_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(agent_paths.sys, "platform", "linux")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

        assert agent_paths.runtime_base_dir() == Path("/run/user/1000")

    def test_non_darwin_without_xdg_falls_back_to_gettempdir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BLUMKIN_AGENT_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(agent_paths.sys, "platform", "linux")
        monkeypatch.setattr(agent_paths.tempfile, "gettempdir", lambda: "/tmp")

        assert agent_paths.runtime_base_dir() == Path("/tmp")
