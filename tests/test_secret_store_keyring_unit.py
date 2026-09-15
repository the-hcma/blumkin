"""Keyring-backend selection, migration, and fallback for blumkin.secret_store.

The default test-suite guard (``tests/conftest.py::_force_file_secret_backend``)
forces every other test onto the file backend so nothing here touches a
developer's real OS keychain. These tests install a small fake in-memory
keyring module instead, to exercise the keyring code paths deterministically.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from blumkin import secret_store
from blumkin.config import load_config
from blumkin.secret_store import SecretWriteError

# Captured before any fixture (see conftest._force_file_secret_backend) patches
# secret_store._keyring_module - the only way to reinstate the real function
# for the tests below without undoing every other autouse fixture's patches.
_REAL_KEYRING_MODULE = secret_store._keyring_module


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module's module-level API."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise _PasswordDeleteError("not found")
        del self.store[(service, username)]

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


class _PasswordDeleteError(Exception):
    """Stand-in for ``keyring.errors.PasswordDeleteError``."""


class _BrokenKeyring(_FakeKeyring):
    """A backend present but unusable at write time (e.g. locked/denied)."""

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("keychain access denied")


class _DeleteFailsKeyring(_FakeKeyring):
    """A backend that has an entry but refuses to delete it (not "not found")."""

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("keychain deletion denied")


class _ProbeFailsKeyring(_FakeKeyring):
    """A backend whose existence probe fails but whose deletion would succeed.

    Models a locked keychain / denied ACL prompt: ``get_password`` raises, but
    the entry is still really there and ``delete_password`` would work fine
    if actually attempted.
    """

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("keychain locked")


class _BreaksAfterFirstWriteKeyring(_FakeKeyring):
    """Accepts one write, then refuses every later one (e.g. a keychain that locks mid-session)."""

    def __init__(self) -> None:
        super().__init__()
        self._writes = 0

    def set_password(self, service: str, username: str, password: str) -> None:
        self._writes += 1
        if self._writes > 1:
            raise RuntimeError("keychain access denied")
        super().set_password(service, username, password)


def _account(cfg, kind: secret_store.SecretKind) -> tuple[str, str]:
    """The (service, account) key ``fake.store`` should hold for ``cfg``/``kind``."""
    return (secret_store._KEYRING_SERVICE, secret_store._keyring_account(cfg, kind))


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token_storage: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    assert fake.store[_account(cfg, "token_cache")] == "secret-payload"
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
    assert fake.store[_account(cfg, "token_cache")] == "legacy-value"


def test_migration_keeps_serving_file_if_keyring_write_fails(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    cfg.profile_dir.mkdir(parents=True)
    cfg.token_cache_path.write_text("legacy-value")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())

    assert secret_store.read_text(cfg, "token_cache") == "legacy-value"
    assert cfg.token_cache_path.is_file()


def test_migration_rolls_back_keyring_copy_when_unlink_fails(tmp_path: Path, monkeypatch) -> None:
    """A partial migration (keyring write ok, plaintext unlink fails) must retry.

    Otherwise the plaintext file lingers untracked forever: the keyring copy
    is served on every subsequent read, so cleanup of the stale file never
    happens (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    cfg.profile_dir.mkdir(parents=True)
    cfg.token_cache_path.write_text("legacy-value")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    real_unlink = Path.unlink

    def reject_unlink(self: Path, *args, **kwargs) -> None:
        raise OSError("device busy")

    monkeypatch.setattr(Path, "unlink", reject_unlink)

    assert secret_store.read_text(cfg, "token_cache") == "legacy-value"
    # Rolled back - the keyring must not hold a copy while the file still does.
    assert _account(cfg, "token_cache") not in fake.store
    assert cfg.token_cache_path.is_file()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    # A later read (unlink working again) retries and completes the migration.
    assert secret_store.read_text(cfg, "token_cache") == "legacy-value"
    assert not cfg.token_cache_path.exists()
    assert fake.store[_account(cfg, "token_cache")] == "legacy-value"


def test_delete_removes_from_both_backends(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "auth_record", "record-payload")
    secret_store.delete(cfg, "auth_record")
    assert secret_store.exists(cfg, "auth_record") is False
    assert not cfg.auth_record_path.exists()
    assert fake.store == {}


def test_delete_is_a_noop_when_nothing_is_stored(tmp_path: Path, monkeypatch) -> None:
    """No entry to delete - not an error, and the backend's delete is never called."""
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.delete(cfg, "auth_record")  # must not raise


def test_delete_surfaces_backend_failure_that_is_not_not_found(tmp_path: Path, monkeypatch) -> None:
    """A denied/failed deletion of an entry that *does* exist must not look like success.

    Silently swallowing every deletion failure leaves the credential usable
    after `auth logout` reports success (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _DeleteFailsKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "auth_record", "record-payload")
    with pytest.raises(SecretWriteError, match="cannot delete"):
        secret_store.delete(cfg, "auth_record")


def test_delete_attempts_deletion_when_existence_cannot_be_probed(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed 'does it exist' probe must not be treated as 'nothing stored'.

    Otherwise a locked keychain makes `auth logout` silently delete nothing
    and still report success, leaving the credential usable once the
    keychain unlocks (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _ProbeFailsKeyring()
    account = _account(cfg, "auth_record")
    fake.store[account] = "record-payload"  # bypass the (also-failing) probe on write
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    secret_store.delete(cfg, "auth_record")

    assert account not in fake.store


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
    """token_storage = "keyring" is an explicit ask - raise, don't downgrade."""
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())
    with pytest.raises(SecretWriteError, match="OS keychain"):
        secret_store.write_text(cfg, "token_cache", "payload")


def test_auto_falls_back_to_file_when_keyring_write_fails(tmp_path: Path, monkeypatch) -> None:
    """token_storage = "auto" must silently downgrade to the file on a write failure.

    Only "keyring" (an explicit ask) is allowed to raise; "auto" promises a
    silent fallback for *any* backend trouble, not just a missing backend
    (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())
    secret_store.write_text(cfg, "token_cache", "payload")
    assert cfg.token_cache_path.read_text() == "payload"


def test_auto_fallback_deletes_a_stale_keyring_entry_so_the_file_is_actually_read(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed re-write under "auto" must not leave an unreachable stale keyring entry.

    read_text() always prefers a present keyring value over the file, so if
    the old value from a prior successful write is left behind, the fresh
    value that was just written to the file (the whole point of the "auto"
    fallback) can never be read back (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    fake = _BreaksAfterFirstWriteKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    secret_store.write_text(cfg, "token_cache", "first-value")
    assert secret_store.read_text(cfg, "token_cache") == "first-value"

    secret_store.write_text(cfg, "token_cache", "second-value")

    assert _account(cfg, "token_cache") not in fake.store
    assert secret_store.read_text(cfg, "token_cache") == "second-value"


def test_auto_prefers_file_when_no_keyring_backend(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    secret_store.write_text(cfg, "google_token", "payload")
    assert cfg.google_token_path.read_text() == "payload"


def test_keyring_account_is_namespaced_by_config_dir(tmp_path: Path, monkeypatch) -> None:
    """Two config dirs with the same profile name must not share one keychain item.

    ``BLUMKIN_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` exist precisely to select a
    *different* config dir (e.g. a sandbox/CI tenant); without config_dir in
    the account name, logging out of one silently deletes the other's secret
    (issue #287 review).
    """
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    primary = _load(tmp_path / "primary", monkeypatch, token_storage="keyring")
    secret_store.write_text(primary, "auth_record", "primary-value")

    alternate = _load(tmp_path / "alternate", monkeypatch, token_storage="keyring")
    assert secret_store.read_text(alternate, "auth_record") is None

    secret_store.write_text(alternate, "auth_record", "alternate-value")
    assert secret_store.read_text(primary, "auth_record") == "primary-value"
    assert secret_store.read_text(alternate, "auth_record") == "alternate-value"

    secret_store.delete(alternate, "auth_record")
    assert secret_store.read_text(primary, "auth_record") == "primary-value"


@pytest.fixture
def _real_keyring_module_lookup(monkeypatch: pytest.MonkeyPatch):
    """Undo the autouse file-backend guard so ``_keyring_module`` itself runs.

    ``tests/conftest.py::_force_file_secret_backend`` replaces
    ``secret_store._keyring_module`` wholesale for every other test; these
    tests exist specifically to exercise the real function body (the lazy
    import, the ``keyring.backends.fail`` detection, and the module-level
    cache), which no other test in the suite does (issue #287 review: a
    regression there - e.g. the fail-backend check being inverted or dropped -
    would ship green while every login/cache write starts raising on hosts
    with no usable backend).
    """
    monkeypatch.setattr(secret_store, "_keyring_module", _REAL_KEYRING_MODULE)
    secret_store._keyring_checked = False
    secret_store._keyring_mod = None
    yield
    secret_store._keyring_checked = False
    secret_store._keyring_mod = None


def _install_fake_keyring_package(monkeypatch: pytest.MonkeyPatch, backend: object) -> None:
    stub = type(sys)("keyring")
    stub.get_keyring = lambda: backend
    monkeypatch.setitem(sys.modules, "keyring", stub)


def test_real_keyring_module_returns_none_for_a_fail_backend(
    _real_keyring_module_lookup, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailBackend:
        pass

    _FailBackend.__module__ = "keyring.backends.fail.Keyring"
    _install_fake_keyring_package(monkeypatch, _FailBackend())

    assert secret_store._keyring_module() is None


def test_real_keyring_module_returns_the_module_for_a_real_backend(
    _real_keyring_module_lookup, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _RealBackend:
        pass

    _RealBackend.__module__ = "keyring.backends.macOS.Keyring"
    _install_fake_keyring_package(monkeypatch, _RealBackend())

    module = secret_store._keyring_module()
    assert module is not None
    assert module.get_keyring() is not None


def test_real_keyring_module_caches_after_first_call(
    _real_keyring_module_lookup, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    class _RealBackend:
        pass

    _RealBackend.__module__ = "keyring.backends.macOS.Keyring"

    def get_keyring():
        calls["n"] += 1
        return _RealBackend()

    stub = type(sys)("keyring")
    stub.get_keyring = get_keyring
    monkeypatch.setitem(sys.modules, "keyring", stub)

    assert secret_store._keyring_module() is not None
    assert secret_store._keyring_module() is not None
    assert calls["n"] == 1


def test_keyring_probe_treats_a_hang_as_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend whose ``get_keyring()`` hangs (e.g. a stuck D-Bus call) must not
    hang login/`doctor` - the probe is bounded and treats a timeout as "unusable".
    """
    monkeypatch.setattr(secret_store, "_KEYRING_PROBE_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()

    class _HangingModule:
        @staticmethod
        def get_keyring():
            release.wait(timeout=5)
            return object()

    try:
        assert secret_store._probe_keyring_backend(_HangingModule()) is False
    finally:
        release.set()
