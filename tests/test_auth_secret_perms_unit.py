"""Secret file permission hardening for the MSAL cache and auth record."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from blumkin import auth


def test_write_secret_text_creates_0600(tmp_path: Path) -> None:
    target = tmp_path / "msal_token_cache.json"
    auth._write_secret_text(target, '{"RefreshToken":{}}')
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_write_secret_text_tightens_existing_world_readable(tmp_path: Path) -> None:
    target = tmp_path / "msal_token_cache.json"
    target.write_text("stale", encoding="utf-8")
    target.chmod(0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    auth._write_secret_text(target, "secret-payload")
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600
    assert target.read_text(encoding="utf-8") == "secret-payload"


def test_write_secret_text_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("keep", encoding="utf-8")
    link = tmp_path / "msal_token_cache.json"
    link.symlink_to(real)
    with pytest.raises(OSError, match="cannot write secret file"):
        auth._write_secret_text(link, "hijack")
    assert real.read_text(encoding="utf-8") == "keep"


def test_ensure_secret_dir_tightens_existing_mode(tmp_path: Path) -> None:
    directory = tmp_path / "blumkin"
    directory.mkdir(mode=0o755)
    auth._ensure_secret_dir(directory)
    mode = stat.S_IMODE(directory.stat().st_mode)
    assert mode == 0o700
