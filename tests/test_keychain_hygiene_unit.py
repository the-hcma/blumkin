"""The guard that keeps tests and temporary config directories off the real keychain (#759)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blumkin import keychain_hygiene
from blumkin.keychain_hygiene import KeychainRefusedError
from blumkin.keychain_vault import VaultKeyring

# ------------------------------------------------------------------ the guard


class _Backend:
    class errors:  # noqa: N801
        PasswordDeleteError = KeyError

    def __init__(self) -> None:
        self.touched = False

    def get_password(self, service: str, account: str) -> None:
        self.touched = True

    def set_password(self, service: str, account: str, password: str) -> None:
        self.touched = True

    def delete_password(self, service: str, account: str) -> None:
        self.touched = True


def test_a_temporary_config_dir_is_refused_the_real_keychain(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend, refuse_temporary=True)
    account = json.dumps([str(tmp_path), "work", "token_cache"])
    with pytest.raises(KeychainRefusedError, match="BLUMKIN_ALLOW_TEMP_KEYCHAIN"):
        vault.get_password("blumkin", account)
    with pytest.raises(KeychainRefusedError):
        vault.set_password("blumkin", account, "v")
    with pytest.raises(KeychainRefusedError):
        vault.delete_password("blumkin", account)
    assert backend.touched is False


def test_a_test_run_is_refused_even_for_a_real_looking_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keychain_hygiene, "is_temporary", lambda _d: False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    with pytest.raises(KeychainRefusedError):
        keychain_hygiene.refuse_temporary_config_dir("/Users/someone/.config/blumkin")


def test_a_live_test_run_is_exempt_so_it_can_use_the_real_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keychain_hygiene, "is_temporary", lambda _d: False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setenv("BLUMKIN_LIVE", "1")
    keychain_hygiene.refuse_temporary_config_dir("/Users/someone/.config/blumkin")
    assert keychain_hygiene.refuses_directory("/Users/someone/.config/blumkin") is False


def test_the_override_lifts_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(keychain_hygiene.ALLOW_TEMP_ENV, "1")
    keychain_hygiene.refuse_temporary_config_dir(str(tmp_path))


def test_the_guard_is_off_unless_asked_for(tmp_path: Path) -> None:
    backend = _Backend()
    account = json.dumps([str(tmp_path), "work", "token_cache"])
    VaultKeyring(backend).get_password("blumkin", account)
    assert backend.touched is True


def test_a_temporary_config_dir_uses_the_file_backend_without_touching_the_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from blumkin import secret_store

    backend = _Backend()
    fake = VaultKeyring(backend, refuse_temporary=True)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    cfg = SimpleNamespace(token_storage="auto", config_dir=tmp_path)
    assert secret_store._backend_for(cfg) == "file"  # type: ignore[arg-type]
    pinned = SimpleNamespace(token_storage="keyring", config_dir=tmp_path)
    assert secret_store._backend_for(pinned) == "keyring"  # type: ignore[arg-type]
    assert backend.touched is False
