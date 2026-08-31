"""Secret file permission hardening for the MSAL cache and auth record."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from blumkin import auth
from blumkin.auth import SecretWriteError

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
    with pytest.raises(SecretWriteError, match="cannot write secret file"):
        auth._write_secret_text(link, "hijack")
    assert real.read_text(encoding="utf-8") == "keep"


@_POSIX_MODE_TESTS
def test_ensure_secret_dir_tightens_existing_mode(tmp_path: Path) -> None:
    directory = tmp_path / "blumkin"
    directory.mkdir(mode=0o755)
    auth._ensure_secret_dir(directory, stop_at=directory)
    mode = stat.S_IMODE(directory.stat().st_mode)
    assert mode == 0o700


def test_ensure_secret_dir_refuses_symlink(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("symlink config-dir layout is a POSIX concern")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "blumkin"
    link.symlink_to(real)
    with pytest.raises(SecretWriteError, match="cannot use symlinked config dir"):
        auth._ensure_secret_dir(link, stop_at=link)


def test_ensure_secret_dir_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    """Named-profile paths must refuse a symlinked config-dir or profiles/ parent."""
    if sys.platform == "win32":
        pytest.skip("symlink config-dir layout is a POSIX concern")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "blumkin"
    link.symlink_to(real)
    nested = link / "profiles" / "work"
    with pytest.raises(SecretWriteError, match="cannot use symlinked config dir"):
        auth._ensure_secret_dir(nested, stop_at=link)
    assert not (real / "profiles").exists()


def test_ensure_secret_dir_allows_platform_symlinks_above_config(tmp_path: Path) -> None:
    """Walk stops at config_dir so macOS /var → /private/var does not refuse writes."""
    config_dir = tmp_path / "blumkin"
    profile_dir = config_dir / "profiles" / "work"
    auth._ensure_secret_dir(profile_dir, stop_at=config_dir)
    assert profile_dir.is_dir()
    assert not profile_dir.is_symlink()


def test_ensure_secret_dir_rejects_path_outside_stop_at(tmp_path: Path) -> None:
    config_dir = tmp_path / "blumkin"
    config_dir.mkdir()
    outside = tmp_path / "elsewhere" / "work"
    with pytest.raises(SecretWriteError, match="outside config dir"):
        auth._ensure_secret_dir(outside, stop_at=config_dir)
    assert not outside.exists()


def test_ensure_secret_dir_survives_chmod_oserror(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "blumkin"
    directory.mkdir()

    def reject(_path: Path | str, _mode: int) -> None:
        raise OSError("chmod unsupported")

    monkeypatch.setattr(os, "chmod", reject)
    auth._ensure_secret_dir(directory, stop_at=directory)
    assert directory.is_dir()


def test_write_secret_text_survives_fchmod_oserror(tmp_path: Path, monkeypatch) -> None:
    if not hasattr(os, "fchmod"):
        pytest.skip("fchmod unavailable")
    target = tmp_path / "msal_token_cache.json"

    def reject(_fd: int, _mode: int) -> None:
        raise OSError("fchmod unsupported")

    monkeypatch.setattr(os, "fchmod", reject)
    auth._write_secret_text(target, "still-saved")
    assert target.read_text(encoding="utf-8") == "still-saved"
