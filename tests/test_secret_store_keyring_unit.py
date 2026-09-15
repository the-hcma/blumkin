"""Keyring-backend selection, migration, and fallback for blumkin.secret_store.

The default test-suite guard (``tests/conftest.py::_force_file_secret_backend``)
forces every other test onto the file backend so nothing here touches a
developer's real OS keychain. These tests install a small fake in-memory
keyring module instead, to exercise the keyring code paths deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin import secret_store
from blumkin.config import load_config
from blumkin.secret_store import SecretWriteError


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module's module-level API."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


class _BrokenKeyring(_FakeKeyring):
    """A backend present but unusable at write time (e.g. locked/denied)."""

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("keychain access denied")


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token_storage: str):
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        f'[profiles.default]\nclient_id = "test-client"\ntoken_storage = "{token_storage}"\n'
    )
    return load_config()


def test_active_backend_reports_file_when_forced(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="file")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    assert secret_store.active_backend(cfg) == "file"


def test_active_backend_reports_keyring_when_usable(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    assert secret_store.active_backend(cfg) == "keyring"


def test_write_then_read_round_trips_through_keyring(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "token_cache", "secret-payload")
    assert not cfg.token_cache_path.exists()
    assert fake.store[("blumkin", "default:token_cache")] == "secret-payload"
    assert secret_store.read_text(cfg, "token_cache") == "secret-payload"
    assert secret_store.exists(cfg, "token_cache") is True


def test_legacy_file_migrates_into_keyring_on_first_read(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    cfg.profile_dir.mkdir(parents=True)
    cfg.token_cache_path.write_text("legacy-value")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    assert secret_store.read_text(cfg, "token_cache") == "legacy-value"

    assert not cfg.token_cache_path.exists()
    assert fake.store[("blumkin", "default:token_cache")] == "legacy-value"


def test_migration_keeps_serving_file_if_keyring_write_fails(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    cfg.profile_dir.mkdir(parents=True)
    cfg.token_cache_path.write_text("legacy-value")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())

    assert secret_store.read_text(cfg, "token_cache") == "legacy-value"
    assert cfg.token_cache_path.is_file()


def test_delete_removes_from_both_backends(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "auth_record", "record-payload")
    secret_store.delete(cfg, "auth_record")
    assert secret_store.exists(cfg, "auth_record") is False
    assert not cfg.auth_record_path.exists()
    assert ("blumkin", "default:auth_record") not in fake.store


def test_explicit_keyring_falls_back_to_file_and_warns_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)

    secret_store.write_text(cfg, "token_cache", "payload")

    assert cfg.token_cache_path.is_file()
    warnings = capsys.readouterr().err
    assert "no usable OS keychain backend" in warnings
    # Only warned once even though _backend_for is consulted on every call.
    secret_store.read_text(cfg, "token_cache")
    assert capsys.readouterr().err == ""


def test_write_text_raises_secret_write_error_when_keyring_unusable_mid_write(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())
    with pytest.raises(SecretWriteError, match="OS keychain"):
        secret_store.write_text(cfg, "token_cache", "payload")


def test_auto_prefers_file_when_no_keyring_backend(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    secret_store.write_text(cfg, "google_token", "payload")
    assert cfg.google_token_path.read_text() == "payload"
