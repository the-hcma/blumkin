"""Secret file permission hardening for the MSAL cache and auth record."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from blumkin import auth

_POSIX_MODE_TESTS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix permission bits are not meaningful on Windows ACLs",
)


def test_write_secret_text_round_trips(tmp_path: Path) -> None:
    """Every platform must persist without raising (Windows has no fchmod)."""
    target = tmp_path / "msal_token_cache.json"
    auth._write_secret_text(target, '{"RefreshToken":{}}')
    assert target.read_text(encoding="utf-8") == '{"RefreshToken":{}}'


@_POSIX_MODE_TESTS
def test_write_secret_text_creates_0600(tmp_path: Path) -> None:
    target = tmp_path / "msal_token_cache.json"
    auth._write_secret_text(target, '{"RefreshToken":{}}')
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


@_POSIX_MODE_TESTS
def test_write_secret_text_tightens_existing_world_readable(tmp_path: Path) -> None:
    target = tmp_path / "msal_token_cache.json"
    target.write_text("stale", encoding="utf-8")
    target.chmod(0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    auth._write_secret_text(target, "secret-payload")
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600
    assert target.read_text(encoding="utf-8") == "secret-payload"


@pytest.mark.skipif(
    not getattr(os, "O_NOFOLLOW", 0) or sys.platform == "win32",
    reason="O_NOFOLLOW symlink refusal is POSIX-only",
)
def test_write_secret_text_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("keep", encoding="utf-8")
    link = tmp_path / "msal_token_cache.json"
    link.symlink_to(real)
    with pytest.raises(OSError, match="cannot write secret file"):
        auth._write_secret_text(link, "hijack")
    assert real.read_text(encoding="utf-8") == "keep"


@_POSIX_MODE_TESTS
def test_ensure_secret_dir_tightens_existing_mode(tmp_path: Path) -> None:
    directory = tmp_path / "blumkin"
    directory.mkdir(mode=0o755)
    auth._ensure_secret_dir(directory)
    mode = stat.S_IMODE(directory.stat().st_mode)
    assert mode == 0o700
