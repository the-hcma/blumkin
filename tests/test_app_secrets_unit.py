"""Unit tests for blumkin.app_secrets (issue #368's keychain-only app-secret layer).

The default autouse guard (``tests/conftest.py::_force_file_secret_backend``)
forces every other test onto the file backend by patching
``secret_store._keyring_module``. Because ``app_secrets`` calls that same
function through the ``secret_store`` module (not a private ``from ... import``
binding — see the module docstring on why that matters), the same guard
covers it: importing this test module must never touch a developer's real OS
keychain even without any local patching.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin import app_secrets, secret_store
from blumkin.config import load_config
from blumkin.secret_store import SecretWriteError


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module's module-level API."""

    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise self.errors.PasswordDeleteError("not found")
        del self.store[(service, username)]

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password


class _BrokenKeyring(_FakeKeyring):
    """A backend present but unusable at write/read time (e.g. locked/denied)."""

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("keychain access denied")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("keychain access denied")


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token_storage: str = "auto"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        f'[profiles.default]\nclient_id = "test-client"\ntoken_storage = "{token_storage}"\n'
    )
    return load_config()


def test_read_app_secret_returns_none_without_keyring_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    assert app_secrets.read_app_secret(cfg, "google_client_secret") is None


def test_read_app_secret_returns_none_when_nothing_vaulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    assert app_secrets.read_app_secret(cfg, "google_client_secret") is None


def test_write_then_read_app_secret_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    app_secrets.write_app_secret(cfg, "google_client_secret", "s3cr3t")
    assert app_secrets.read_app_secret(cfg, "google_client_secret") == "s3cr3t"
    assert app_secrets.app_secret_backend(cfg, "google_client_secret") == "keyring"


def test_read_app_secret_returns_none_when_token_storage_is_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="file")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    # A value written directly to the fake backend (bypassing write_app_secret's
    # own token_storage guard) must still not be read back once file-only is set.
    fake.set_password(
        secret_store._KEYRING_SERVICE, app_secrets._account(cfg, "google_client_secret"), "leaked"
    )
    assert app_secrets.read_app_secret(cfg, "google_client_secret") is None
    assert app_secrets.app_secret_backend(cfg, "google_client_secret") == "none"


def test_read_app_secret_returns_none_on_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`app_secret_backend` deliberately does not distinguish "the keychain
    probe raised/timed out" from "nothing is vaulted" - both report `"none"`.
    `doctor`'s vaulted-``ms_client_id`` check (`cli._vaulted_client_id_or_secret_present`)
    therefore cannot tell a transient environmental read failure (a locked
    login keychain, a denied ACL prompt) apart from a genuinely unconfigured
    profile, and will report `client_id missing in config.toml` for both
    (issue #368 review, round 5: documenting this as an accepted, existing
    trade-off - not new behavior - rather than a silent implication; a
    dedicated error/unknown third state is tracked as a #368 follow-up)."""
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())
    assert app_secrets.read_app_secret(cfg, "google_client_secret") is None
    assert app_secrets.app_secret_backend(cfg, "google_client_secret") == "none"


def test_read_app_secret_or_raise_propagates_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike `read_app_secret`, the internal `_read_app_secret_or_raise` probe
    (issue #384, used by `auth_setup.apply_microsoft_setup`'s post-write-failure
    stale-value check) must not swallow a backend failure into `None` - a
    caller distinguishing "nothing vaulted" from "could not tell" needs the
    exception to propagate."""
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())
    with pytest.raises(RuntimeError, match="keychain access denied"):
        app_secrets._read_app_secret_or_raise(cfg, "ms_client_id")


def test_read_app_secret_or_raise_round_trips_a_vaulted_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    app_secrets.write_app_secret(cfg, "ms_client_id", "11111111-1111-1111-1111-111111111111")
    assert (
        app_secrets._read_app_secret_or_raise(cfg, "ms_client_id")
        == "11111111-1111-1111-1111-111111111111"
    )


def test_read_app_secret_or_raise_still_returns_none_for_the_no_fallback_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`token_storage = "file"` and no keyring backend at all are genuinely
    "nothing vaulted" states, not failures - `_read_app_secret_or_raise` must
    keep reading those as `None` rather than raising."""
    file_cfg = _load(tmp_path / "file", monkeypatch, token_storage="file")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    assert app_secrets._read_app_secret_or_raise(file_cfg, "ms_client_id") is None

    no_backend_cfg = _load(tmp_path / "no-backend", monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    assert app_secrets._read_app_secret_or_raise(no_backend_cfg, "ms_client_id") is None


def test_app_secret_backend_is_none_for_a_usable_but_empty_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachable keychain with nothing vaulted must report `"none"` - not
    just a missing/raising backend (the only two shapes the other `"none"`
    assertions above cover). `doctor`'s `_vaulted_client_id_or_secret_present`
    relies on this distinction to keep flagging a genuinely missing
    `ms_client_id` on a machine with a real, working keychain (issue #368
    review, round 4)."""
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    assert app_secrets.read_app_secret(cfg, "ms_client_id") is None
    assert app_secrets.app_secret_backend(cfg, "ms_client_id") == "none"


def test_write_app_secret_raises_without_keyring_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    with pytest.raises(SecretWriteError, match="no usable OS keychain"):
        app_secrets.write_app_secret(cfg, "google_client_secret", "s3cr3t")


def test_write_app_secret_raises_when_token_storage_is_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="file")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    with pytest.raises(SecretWriteError, match='token_storage = "file"'):
        app_secrets.write_app_secret(cfg, "google_client_secret", "s3cr3t")


def test_write_app_secret_raises_on_empty_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    with pytest.raises(SecretWriteError, match="empty"):
        app_secrets.write_app_secret(cfg, "google_client_secret", "   ")


def test_write_app_secret_raises_on_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _BrokenKeyring())
    with pytest.raises(SecretWriteError, match="could not write"):
        app_secrets.write_app_secret(cfg, "google_client_secret", "s3cr3t")


def test_delete_app_secret_raises_without_keyring_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing entry is idempotent, but a missing *backend* must raise
    rather than silently report success - an entry could already exist and
    remain retrievable once the backend comes back (issue #368 review)."""
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
    with pytest.raises(SecretWriteError, match="no usable OS keychain backend"):
        app_secrets.delete_app_secret(cfg, "google_client_secret")


def test_delete_app_secret_tolerates_missing_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _FakeKeyring())
    app_secrets.delete_app_secret(cfg, "google_client_secret")  # must not raise


def test_delete_app_secret_raises_on_a_non_not_found_backend_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete failure that is *not* a not-found error (a locked/denied
    keychain, say) must raise `SecretWriteError`, not be swallowed alongside
    the idempotent missing-entry case - reporting "removed" here would be
    the exact false-success `delete_app_secret`'s docstring guards against
    (issue #368 review, round 5)."""

    class _DenyingKeyring(_FakeKeyring):
        def delete_password(self, service: str, username: str) -> None:
            raise RuntimeError("keychain is locked")

    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _DenyingKeyring())
    with pytest.raises(SecretWriteError, match="could not delete"):
        app_secrets.delete_app_secret(cfg, "google_client_secret")


def test_delete_app_secret_removes_a_vaulted_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    app_secrets.write_app_secret(cfg, "google_client_secret", "s3cr3t")
    app_secrets.delete_app_secret(cfg, "google_client_secret")
    assert app_secrets.read_app_secret(cfg, "google_client_secret") is None


def test_account_namespace_is_distinct_per_kind_and_never_collides_with_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    google_account = app_secrets._account(cfg, "google_client_secret")
    ms_account = app_secrets._account(cfg, "ms_client_id")
    assert google_account != ms_account
    grant_account = secret_store._keyring_account(cfg, "auth_record")
    assert google_account != grant_account
    assert ms_account != grant_account


def test_account_namespace_is_distinct_per_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    app_secrets.write_app_secret(cfg, "google_client_secret", "profile-one-secret")

    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\n'
        '[profiles.work]\nclient_id = "work-client"\n'
    )
    monkeypatch.setenv("BLUMKIN_PROFILE", "work")
    other_cfg = load_config()
    assert app_secrets.read_app_secret(other_cfg, "google_client_secret") is None


def test_account_namespace_is_distinct_per_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two config dirs (the `BLUMKIN_CONFIG_DIR` escape hatch used for e.g. a
    sandbox/CI tenant) that both use the same profile name must not share one
    keychain item - mirroring `test_keyring_account_is_namespaced_by_config_dir`
    for the grant accounts. Without `config_dir` in `_account`'s namespace,
    `blumkin auth set-app-secret --delete` in one dir would silently remove
    the other's vaulted secret (issue #368 review, round 6)."""
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    primary = _load(tmp_path / "primary", monkeypatch)
    app_secrets.write_app_secret(primary, "google_client_secret", "primary-secret")

    alternate = _load(tmp_path / "alternate", monkeypatch)
    assert app_secrets.read_app_secret(alternate, "google_client_secret") is None

    app_secrets.write_app_secret(alternate, "google_client_secret", "alternate-secret")
    assert app_secrets.read_app_secret(primary, "google_client_secret") == "primary-secret"
    assert app_secrets.read_app_secret(alternate, "google_client_secret") == "alternate-secret"

    app_secrets.delete_app_secret(alternate, "google_client_secret")
    assert app_secrets.read_app_secret(primary, "google_client_secret") == "primary-secret"
