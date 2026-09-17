"""Keyring-backend selection, migration, and fallback for blumkin.secret_store.

The default test-suite guard (``tests/conftest.py::_force_file_secret_backend``)
forces every other test onto the file backend so nothing here touches a
developer's real OS keychain. These tests install a small fake in-memory
keyring module instead, to exercise the keyring code paths deterministically.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from blumkin import secret_store
from blumkin.config import load_config
from blumkin.secret_store import SecretWriteError

# Captured before any fixture (see conftest._force_file_secret_backend) patches
# secret_store._keyring_module - the only way to reinstate the real function
# for the tests below without undoing every other autouse fixture's patches.
_REAL_KEYRING_MODULE = secret_store._keyring_module


class _PasswordDeleteError(Exception):
    """Stand-in for ``keyring.errors.PasswordDeleteError``."""


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module's module-level API."""

    class errors:
        """Stand-in for ``keyring.errors`` - just enough for ``_is_not_found``."""

        PasswordDeleteError = _PasswordDeleteError

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


class _BrokenAndUncleanableKeyring(_BrokenKeyring):
    """A backend whose write *and* stale-entry cleanup both fail.

    Models the double-fault "auto" must not swallow: the fresh write fails
    (as ``_BrokenKeyring`` already does), and the best-effort cleanup of the
    stale prior entry fails too, for a reason other than "already gone" - so
    the keyring is left holding a stale value while the file gets the new
    one, and the two backends now disagree (issue #287 review).
    """

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("keychain deletion denied")


class _LockedKeyring(_FakeKeyring):
    """A backend present but entirely unreachable (locked login keychain over SSH, etc.).

    Every call - read, write, or delete - fails with the same access error;
    there is no way to distinguish "there is a stale value we can't reach"
    from "there was never anything here" from the outside (issue #287
    review, round 7: the single root cause behind a failed write and a
    failed stale-entry probe/cleanup must not, on its own, be treated as
    evidence of a real disagreement between the two backends).
    """

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("keychain locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("keychain locked")

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("keychain locked")


class _HangsOnMutationKeyring(_FakeKeyring):
    """A backend whose ``set_password``/``delete_password`` never return in time.

    Models a locked Linux Secret Service / macOS Keychain prompting for
    interactive unlock: the call is not merely slow to fail, it genuinely
    hangs, so ``_call_keyring_with_timeout`` abandons it on a daemon thread
    that keeps running - and could still complete after the caller has
    moved on (issue #287 review, round 6).
    """

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release

    def set_password(self, service: str, username: str, password: str) -> None:
        self._release.wait(timeout=5)
        super().set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        self._release.wait(timeout=5)
        super().delete_password(service, username)


def _account(cfg, kind: secret_store.SecretKind) -> tuple[str, str]:
    """The (service, account) key ``fake.store`` should hold for ``cfg``/``kind``."""
    return (secret_store._KEYRING_SERVICE, secret_store._keyring_account(cfg, kind))


def _bundle_seed(fake: _FakeKeyring, cfg, kind: secret_store.SecretKind, value: str) -> None:
    """Set ``kind``'s value at its (possibly shared) account, preserving any sibling already there.

    ``auth_record``/``token_cache`` share one keychain item holding a
    ``{kind: text}`` JSON object - seeding a fake store for a test must merge
    into that object rather than overwrite it, exactly like the real
    ``_bundle_write_kind`` does, or a test that seeds both kinds would have
    the second seed clobber the first.
    """
    key = _account(cfg, kind)
    bundle = secret_store._bundle_dict_from_raw(fake.store.get(key))
    bundle[kind] = value
    fake.store[key] = json.dumps(bundle)


def _bundle_value(fake: _FakeKeyring, cfg, kind: secret_store.SecretKind) -> str | None:
    """Read back just ``kind``'s value from its (possibly shared) account, or ``None``."""
    return secret_store._bundle_dict_from_raw(fake.store.get(_account(cfg, kind))).get(kind)


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
    assert secret_store.active_backend(cfg, "token_cache") == "file"


def test_active_backend_reports_keyring_when_usable(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    assert secret_store.active_backend(cfg, "token_cache") == "keyring"


def test_active_backend_reports_file_once_a_write_has_actually_fallen_back_there(
    tmp_path: Path, monkeypatch
) -> None:
    """A profile preferring keyring must not be misreported once it always falls back.

    ``_backend_for`` only reflects config + whether *some* backend is usable
    in principle - it cannot see that every write for this profile has been
    falling back to the file at runtime (a payload past the backend's own
    size cap, a keychain that only fails at write time, etc.). Once that has
    happened, `doctor`/`profiles list` must say "file", the backend actually
    holding the current value, not "keyring" (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    # The keyring never received anything (write always fell back); the file
    # is the one place the secret actually landed.
    secret_store._file_path(cfg, "token_cache").parent.mkdir(parents=True, exist_ok=True)
    secret_store._file_path(cfg, "token_cache").write_text("cache-payload")

    assert secret_store.active_backend(cfg, "token_cache") == "file"


def test_active_backend_reports_the_preference_for_a_brand_new_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """Nothing stored anywhere yet: report the preference, not a hard-coded default."""
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    assert secret_store.active_backend(cfg, "token_cache") == "keyring"


def test_write_then_read_round_trips_through_keyring(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "token_cache", "secret-payload")
    assert not cfg.token_cache_path.exists()
    assert _bundle_value(fake, cfg, "token_cache") == "secret-payload"
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
    assert _bundle_value(fake, cfg, "token_cache") == "legacy-value"


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
    assert _bundle_value(fake, cfg, "token_cache") == "legacy-value"


def test_read_reconciles_a_leftover_plaintext_file_once_the_keyring_has_a_value(
    tmp_path: Path, monkeypatch
) -> None:
    """A stale plaintext file next to a live keyring value must not linger forever.

    This models a prior "auto" write that fell back to the file, whose own
    best-effort stale-keyring cleanup didn't land (a delayed unlock, a
    racing process, etc.) - the keyring still holds a value, so it wins, but
    the leftover file must still get cleaned up once it is safe to do so
    (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    cfg.profile_dir.mkdir(parents=True)
    cfg.token_cache_path.write_text("stale-file-value")
    fake = _FakeKeyring()
    _bundle_seed(fake, cfg, "token_cache", "keyring-value")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    assert secret_store.read_text(cfg, "token_cache") == "keyring-value"
    assert not cfg.token_cache_path.exists()


# --- auth_record / token_cache share one keychain item ("bundling") -----------------


@pytest.mark.parametrize("order", [("auth_record", "token_cache"), ("token_cache", "auth_record")])
def test_bundled_kinds_share_one_keychain_item(
    tmp_path: Path, monkeypatch, order: tuple[secret_store.SecretKind, secret_store.SecretKind]
) -> None:
    """Writing both bundled kinds lands in exactly one keychain item, not two.

    This is the whole point of bundling: one macOS Keychain item means one
    authorization prompt for what is, from the operator's point of view, one
    sign-in - two items (the pre-bundle behavior) meant two prompts.
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    first, second = order
    secret_store.write_text(cfg, first, f"{first}-value")
    secret_store.write_text(cfg, second, f"{second}-value")

    assert len(fake.store) == 1
    (raw,) = fake.store.values()
    assert json.loads(raw) == {
        "auth_record": "auth_record-value",
        "token_cache": "token_cache-value",
    }
    assert secret_store.read_text(cfg, "auth_record") == "auth_record-value"
    assert secret_store.read_text(cfg, "token_cache") == "token_cache-value"


def test_deleting_one_bundled_kind_preserves_its_sibling(tmp_path: Path, monkeypatch) -> None:
    """Deleting `token_cache` must not take `auth_record` down with it (and vice versa)."""
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "auth_record", "record-payload")
    secret_store.write_text(cfg, "token_cache", "cache-payload")

    secret_store.delete(cfg, "token_cache")

    assert len(fake.store) == 1  # the shared item is still there ...
    assert secret_store.exists(cfg, "token_cache") is False
    assert secret_store.exists(cfg, "auth_record") is True
    assert secret_store.read_text(cfg, "auth_record") == "record-payload"


@pytest.mark.parametrize("order", [("auth_record", "token_cache"), ("token_cache", "auth_record")])
def test_deleting_both_bundled_kinds_removes_the_shared_item(
    tmp_path: Path, monkeypatch, order: tuple[secret_store.SecretKind, secret_store.SecretKind]
) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "auth_record", "record-payload")
    secret_store.write_text(cfg, "token_cache", "cache-payload")

    first, second = order
    secret_store.delete(cfg, first)
    secret_store.delete(cfg, second)

    assert fake.store == {}


def test_pre_bundle_per_kind_keyring_entries_are_not_carried_over(
    tmp_path: Path, monkeypatch
) -> None:
    """There is deliberately no migration from the old one-account-per-kind scheme.

    A profile still holding entries under the pre-bundle account naming (one
    keychain item per kind, keyed directly by kind name rather than
    ``_BUNDLE_SLOT``) reads back empty here and needs a fresh `blumkin auth
    login` - the old items are simply never read again, not folded in.
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    resolved_config_dir = str(cfg.config_dir.resolve())
    pre_bundle_account = (
        secret_store._KEYRING_SERVICE,
        json.dumps([resolved_config_dir, cfg.profile, "auth_record"], separators=(",", ":")),
    )
    fake.store[pre_bundle_account] = "pre-bundle-value"
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    assert secret_store.exists(cfg, "auth_record") is False
    assert secret_store.read_text(cfg, "auth_record") is None
    assert secret_store.active_backend(cfg, "auth_record") == "keyring"
    # The old entry is left untouched, not cleaned up or folded in.
    assert fake.store == {pre_bundle_account: "pre-bundle-value"}


def test_status_dict_touches_one_keychain_item_for_both_bundled_kinds(
    tmp_path: Path, monkeypatch
) -> None:
    """The user-facing claim: `doctor`/`auth status` cause one keychain touch, not two.

    Regression for the actual complaint that motivated bundling - `auth
    status_dict()` used to read `auth_record` and `token_cache` as two
    separate secrets, each its own keychain item, each its own OS
    authorization prompt.
    """
    from blumkin.auth import status_dict

    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "auth_record", "{}")
    secret_store.write_text(cfg, "token_cache", json.dumps({"AccessToken": {}, "RefreshToken": {}}))

    touched: set[tuple[str, str]] = set()
    real_get = fake.get_password

    def _tracking_get_password(service: str, account: str) -> str | None:
        touched.add((service, account))
        return real_get(service, account)

    monkeypatch.setattr(fake, "get_password", _tracking_get_password)

    status_dict(cfg)

    assert touched == {_account(cfg, "auth_record")}


def test_delete_removes_from_both_backends(tmp_path: Path, monkeypatch) -> None:
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.write_text(cfg, "auth_record", "record-payload")
    secret_store.delete(cfg, "auth_record")
    assert secret_store.exists(cfg, "auth_record") is False
    assert not cfg.auth_record_path.exists()
    assert fake.store == {}


def test_delete_never_touches_the_keyring_when_pinned_to_file(tmp_path: Path, monkeypatch) -> None:
    """token_storage = "file" must not run the keyring path in delete() at all.

    Every other entry point (read_text/write_text/exists/active_backend)
    already short-circuits on ``_backend_for(cfg) == "file"`` - delete() used
    to be the one exception, reaching into a keyring that could be locked or
    unreachable even though this profile's real (and only) backend is the
    file, which had already been removed successfully by that point (issue
    #287 review, round 10).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="file")
    fake = _LockedKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    cfg.auth_record_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.auth_record_path.write_text("payload")

    secret_store.delete(cfg, "auth_record")  # must not raise or touch the keyring

    assert not cfg.auth_record_path.exists()


def test_delete_awaits_an_abandoned_migration_write_before_declaring_nothing_to_remove(
    tmp_path: Path, monkeypatch
) -> None:
    """delete() must give a previously-abandoned mutation for this account a further chance.

    Models read_text()'s legacy-file migration write timing out (abandoned,
    not cancelled - it keeps running) followed immediately by delete(): if
    delete() trusts an immediate probe without first waiting on any known
    pending mutation, it can conclude "nothing to remove" moments before the
    abandoned write actually lands, silently leaving the credential behind
    after logout reports success (issue #287 review, round 10).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    release = threading.Event()
    fake = _HangsOnMutationKeyring(release)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)

    account = _account(cfg, "auth_record")[1]
    with pytest.raises(TimeoutError):
        secret_store._call_keyring_with_timeout(
            fake.set_password, secret_store._KEYRING_SERVICE, account, "migrated-value"
        )
    assert account in secret_store._pending_mutations

    # The abandoned write "lands" partway through delete()'s bounded wait.
    def _release_after_a_moment() -> None:
        time.sleep(0.05)
        release.set()

    threading.Thread(target=_release_after_a_moment, daemon=True).start()
    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 2.0)

    secret_store.delete(cfg, "auth_record")

    assert account not in fake.store


def test_await_pending_mutation_waits_for_a_previously_abandoned_call_to_land(
    monkeypatch,
) -> None:
    """`_await_pending_mutation` gives a registered abandoned thread a further bounded chance."""
    release = threading.Event()
    landed = threading.Event()

    def set_password(service: str, account: str, value: str) -> None:
        # Named `set_password` (rather than a generic helper name) because
        # `_call_keyring_with_timeout` only registers abandoned mutating
        # calls - identified by `func.__name__` - in `_pending_mutations`
        # (issue #287 review, round 12).
        release.wait(timeout=5)
        landed.set()

    account = "test-account-for-await-pending-mutation"
    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(TimeoutError):
        secret_store._call_keyring_with_timeout(set_password, "svc", account, "value")
    assert account in secret_store._pending_mutations

    release.set()
    secret_store._await_pending_mutation(account, timeout=2)

    assert landed.is_set()
    assert account not in secret_store._pending_mutations


def test_a_timed_out_read_does_not_displace_a_registered_pending_mutation(
    monkeypatch,
) -> None:
    """A timed-out ``get_password`` must never overwrite an account's registered mutation.

    Only a genuinely mutating call (``set_password``/``delete_password``) can
    resurrect or remove a credential once it lands, so it is the only kind of
    call whose abandoned thread is worth waiting on. Registering a timed-out
    *read* (``read_text``/``exists``/``active_backend``/``delete``'s own
    existence probe) under the same account key would silently discard the
    real pending mutation - `_await_pending_mutation` would then join and
    clear the harmless read thread instead, letting a caller like `delete()`
    act on stale information moments before the real abandoned write lands
    (issue #287 review, round 12).
    """
    account = "test-account-for-read-vs-mutation-registration"
    write_release = threading.Event()

    def set_password(service: str, account: str, value: str) -> None:
        write_release.wait(timeout=5)

    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(TimeoutError):
        secret_store._call_keyring_with_timeout(set_password, "svc", account, "value")
    mutation_thread = secret_store._pending_mutations[account]

    read_release = threading.Event()

    def get_password(service: str, account: str) -> str | None:
        read_release.wait(timeout=5)
        return None

    with pytest.raises(TimeoutError):
        secret_store._call_keyring_with_timeout(get_password, "svc", account)

    assert secret_store._pending_mutations[account] is mutation_thread
    write_release.set()
    read_release.set()


def test_delete_is_a_noop_when_nothing_is_stored(tmp_path: Path, monkeypatch) -> None:
    """No entry to delete - not an error, and the backend's delete is never called."""
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    secret_store.delete(cfg, "auth_record")  # must not raise


def test_delete_raises_secret_write_error_when_the_file_cannot_be_unlinked(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed file-side delete (permission denied, read-only mount, ...) is not swallowed.

    ``path.unlink()`` was the one filesystem mutation in this module not
    already wrapped into ``SecretWriteError`` - an ``OSError`` here escaped
    `auth logout` as a bare, unclassified traceback, bypassing the CLI's
    `secret_write_failed` contract, and did so *before* the keychain-side
    deletion below even ran, aborting logout early while the credential
    stayed in place (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="file")
    secret_store.write_text(cfg, "auth_record", "record-payload")
    real_unlink = Path.unlink

    def _denied_unlink(self: Path, *args, **kwargs):
        if self == cfg.auth_record_path:
            raise PermissionError("denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _denied_unlink)
    with pytest.raises(SecretWriteError, match="cannot delete"):
        secret_store.delete(cfg, "auth_record")


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


def test_delete_treats_an_unprobeable_but_genuinely_absent_entry_as_a_noop(
    tmp_path: Path, monkeypatch
) -> None:
    """Probe fails *and* nothing was ever there - must not report a failed logout.

    The existence probe and the delete call are two independent ways to ask
    "is anything stored", and keyring's own APIs cannot always tell "not
    found" apart from "denied" - but when the delete attempt reports "not
    found" for an entry we could never confirm existed in the first place,
    that must resolve as an ordinary no-op logout, not a reported
    ``SecretWriteError`` (issue #287 review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _ProbeFailsKeyring()  # empty store: nothing was ever written
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    secret_store.delete(cfg, "auth_record")  # must not raise


def test_auto_delete_does_not_raise_when_the_keychain_is_locked_and_unprobeable(
    tmp_path: Path, monkeypatch
) -> None:
    """`auto` must not fail a logout just because the keychain is locked/unreachable.

    A locked keychain fails both the existence probe and the delete
    attempt with the same access error - there is no way to tell "there is
    a confirmed leftover we failed to remove" from "there may never have
    been anything here at all". Under `token_storage = "auto"` the file
    (this profile's only backend, if the keychain has never been
    reachable) was already unlinked above, so raising here would report a
    fully successful logout as failed - contradicting the "auto must never
    fail because no keychain backend can service it" contract this module
    documents for every other operation (issue #287 review, round 11).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    fake = _LockedKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    secret_store.delete(cfg, "auth_record")  # must not raise


def test_keyring_pinned_delete_still_raises_when_the_keychain_is_locked_and_unprobeable(
    tmp_path: Path, monkeypatch
) -> None:
    """`token_storage = "keyring"` is an explicit ask, so it keeps surfacing this failure.

    Unlike `auto`, a profile explicitly pinned to the keyring has no file
    fallback to have already succeeded on - a locked/unreachable keychain
    here is a real failure the caller needs to know about, not one "auto"
    quietly resolves (issue #287 review, round 11).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _LockedKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    with pytest.raises(SecretWriteError, match="cannot delete"):
        secret_store.delete(cfg, "auth_record")


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


def test_auto_leaves_the_stale_keyring_entry_intact_when_the_file_write_itself_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed fallback file write must not first delete the still-good keyring entry.

    The stale-entry cleanup used to run *before* the fallback file write -
    if that write then failed too (a read-only mount, ENOSPC, a symlinked
    path component, ...), the secret was gone from both backends at once,
    with nothing left to recover from. Cleaning up only after the file
    write has actually landed means a failed file write leaves the old
    keyring value intact rather than losing the secret outright (issue #287
    review, round 9).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    fake = _BreaksAfterFirstWriteKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    secret_store.write_text(cfg, "token_cache", "first-value")
    assert secret_store.read_text(cfg, "token_cache") == "first-value"

    monkeypatch.setattr(
        secret_store,
        "_write_file_secret",
        lambda *a, **k: (_ for _ in ()).throw(SecretWriteError("disk full")),
    )
    with pytest.raises(SecretWriteError, match="disk full"):
        secret_store.write_text(cfg, "token_cache", "second-value")

    # The stale keyring entry must still be there - it was never deleted,
    # since the file write it exists to make redundant never succeeded.
    assert _account(cfg, "token_cache") in fake.store
    assert _bundle_value(fake, cfg, "token_cache") == "first-value"


def test_auto_raises_when_write_fails_and_the_stale_entry_cannot_be_cleaned_up(
    tmp_path: Path, monkeypatch
) -> None:
    """A write failure *and* a failed stale-entry cleanup must not look like success.

    Unlike an ordinary write failure (silently downgraded to the file), this
    double-fault leaves the keyring holding the *old* value while the file
    now holds the *new* one - a real disagreement between the two backends
    that "auto" must surface rather than silently paper over (issue #287
    review).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    fake = _BrokenAndUncleanableKeyring()
    _bundle_seed(fake, cfg, "token_cache", "stale-value")  # a value from a prior successful write
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    with pytest.raises(SecretWriteError, match="now disagree"):
        secret_store.write_text(cfg, "token_cache", "new-value")


def test_auto_falls_back_when_a_locked_keychain_fails_a_brand_new_accounts_first_write(
    tmp_path: Path, monkeypatch
) -> None:
    """A locked/unreachable keychain must not block a profile's very first write.

    A single root cause (e.g. a locked login keychain over SSH) fails both
    the write and the probe/cleanup used to check for a stale entry - with
    no stale entry actually existing (this is the account's first-ever
    write), that must not be mistaken for the write-and-cleanup double
    fault that legitimately leaves the two backends disagreeing (issue #287
    review, round 7 regression: the "auto" contract - never fail a login
    just because the keychain is unreachable - must hold even when the
    keychain also can't be probed for a false-positive stale entry).
    """
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    fake = _LockedKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    secret_store.write_text(cfg, "token_cache", "new-value")

    assert cfg.token_cache_path.read_text() == "new-value"


def test_auto_raises_rather_than_falls_back_when_a_write_times_out(
    tmp_path: Path, monkeypatch
) -> None:
    """A write timeout must not fall back to the file, unlike other write failures.

    A timed-out ``set_password`` call is abandoned, not cancelled - the
    daemon thread keeps running and can still land in the keyring after
    this call returns, possibly after a *later* login for the same account
    has already completed. Falling back to the file (as "auto" does for
    every other failure) would leave that race in place; raising instead
    tells the caller to retry rather than risk a stale value clobbering
    newer state later (issue #287 review, round 6).
    """
    release = threading.Event()
    cfg = _load(tmp_path, monkeypatch, token_storage="auto")
    fake = _HangsOnMutationKeyring(release)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)

    try:
        with pytest.raises(SecretWriteError, match="did not respond"):
            secret_store.write_text(cfg, "token_cache", "new-value")
        # Must not have silently fallen back to the file.
        assert not cfg.token_cache_path.exists()
    finally:
        release.set()


def test_keyring_pinned_also_raises_rather_than_falls_back_on_a_write_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    """A pinned ``"keyring"`` write timeout raises the same actionable error as "auto"."""
    release = threading.Event()
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _HangsOnMutationKeyring(release)
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)

    try:
        with pytest.raises(SecretWriteError, match="did not respond"):
            secret_store.write_text(cfg, "auth_record", "new-value")
    finally:
        release.set()


def test_delete_raises_rather_than_treats_a_delete_timeout_as_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    """A delete timeout must not be treated as "nothing to delete" or silently succeed.

    An abandoned ``delete_password`` call is not cancelled by the timeout -
    it can still complete later, possibly removing credentials created by a
    *later* login for the same account. The caller must be told to retry
    rather than have `auth logout` report success while that race is still
    live (issue #287 review, round 6).
    """
    release = threading.Event()
    cfg = _load(tmp_path, monkeypatch, token_storage="keyring")
    fake = _HangsOnMutationKeyring(release)
    _bundle_seed(fake, cfg, "auth_record", "existing-value")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)
    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)

    try:
        with pytest.raises(SecretWriteError, match="did not respond"):
            secret_store.delete(cfg, "auth_record")
    finally:
        release.set()


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


def test_keyring_account_resolves_config_dir_so_relative_spellings_still_match(
    tmp_path: Path, monkeypatch
) -> None:
    """Two spellings of the same config dir must key the same keychain item.

    ``_keyring_account`` used to key on ``str(cfg.config_dir)`` directly -
    only ``expanduser()``'d, never resolved - so a config dir addressed via a
    ``..`` segment (or a different relative path that resolves to the same
    directory) would silently miss the entry a previous write under a
    different-but-equivalent spelling created (issue #287 review, round 9).
    """
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: fake)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    written = _load(real_dir, monkeypatch, token_storage="keyring")
    secret_store.write_text(written, "auth_record", "value")

    detoured = _load(tmp_path / "detour" / ".." / "real", monkeypatch, token_storage="keyring")
    assert detoured.config_dir.resolve() == real_dir.resolve()
    assert secret_store.read_text(detoured, "auth_record") == "value"


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
    monkeypatch.setattr(secret_store, "_keyring_checked", False)
    monkeypatch.setattr(secret_store, "_keyring_mod", None)
    yield


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


def test_real_keyring_module_resolves_consistently_under_concurrent_callers(
    _real_keyring_module_lookup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent first calls must not observe a "checked but not yet resolved" gap.

    Setting ``_keyring_checked`` before ``_keyring_mod`` is populated lets a
    second thread's call land in that window and see "already checked" while
    the module is still ``None`` - wrongly concluding no backend is usable
    even though the first call's probe is about to succeed (issue #287
    review).
    """
    release = threading.Event()
    probed = threading.Event()

    class _RealBackend:
        pass

    _RealBackend.__module__ = "keyring.backends.macOS.Keyring"

    def get_keyring():
        probed.set()
        release.wait(timeout=5)
        return _RealBackend()

    stub = type(sys)("keyring")
    stub.get_keyring = get_keyring
    monkeypatch.setitem(sys.modules, "keyring", stub)

    results: list[Any] = []

    def call_once() -> None:
        results.append(secret_store._keyring_module())

    first = threading.Thread(target=call_once)
    first.start()
    assert probed.wait(timeout=5), "first caller never reached the probe"

    second = threading.Thread(target=call_once)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(results) == 2
    assert results[0] is stub
    assert results[1] is stub


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


def test_keyring_io_call_treats_a_hang_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hanging keyring backend call must not block a login or `doctor` check forever.

    Only backend *selection* used to be time-bounded; a hanging
    ``get_password``/``set_password``/``delete_password`` (e.g. a locked
    Linux Secret Service prompting for interactive unlock) could still hang
    an agent shell indefinitely (issue #287 review).
    """
    release = threading.Event()

    def hangs() -> str:
        release.wait(timeout=5)
        return "too-late"

    try:
        with pytest.raises(TimeoutError):
            secret_store._call_keyring_with_timeout(hangs, timeout=0.05)
    finally:
        release.set()


def test_keyring_io_call_re_raises_the_backend_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def fails() -> None:
        raise RuntimeError("keychain denied")

    with pytest.raises(RuntimeError, match="keychain denied"):
        secret_store._call_keyring_with_timeout(fails)


def test_keyring_io_call_returns_the_backend_result(monkeypatch: pytest.MonkeyPatch) -> None:
    assert secret_store._call_keyring_with_timeout(lambda a, b: a + b, 1, 2) == 3
