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
