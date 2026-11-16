"""The one-item-per-config-dir keychain vault (issue #759)."""

from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path
from typing import Any

import pytest

from blumkin import keychain_vault
from blumkin.keychain_vault import VaultKeyring

_SERVICE = "blumkin"


class _NotFound(Exception):
    pass


class _Backend:
    """An in-memory keyring that counts calls, shaped like the real module."""

    class errors:  # noqa: N801 - mirrors keyring.errors
        PasswordDeleteError = _NotFound

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", account))
        if (service, account) not in self.items:
            raise _NotFound(account)
        del self.items[(service, account)]

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", account))
        return self.items.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.calls.append(("set", account))
        self.items[(service, account)] = password


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    keychain_vault.clear_cache()


def _account(config_dir: Path, profile: str = "work", slot: str = "ms_credentials") -> str:
    return json.dumps([str(config_dir), profile, slot], separators=(",", ":"))


def _vault_account(config_dir: Path) -> str:
    return json.dumps([str(config_dir), "vault"], separators=(",", ":"))


def test_every_secret_of_a_config_dir_lands_in_one_item(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    for profile, slot in (("work", "ms_credentials"), ("personal", "google_token")):
        vault.set_password(_SERVICE, _account(tmp_path, profile, slot), f"{profile}-secret")
    vault.set_password(_SERVICE, _account(tmp_path, "work", "client_id"), "cid")

    assert list(backend.items) == [(_SERVICE, _vault_account(tmp_path))]
    assert vault.get_password(_SERVICE, _account(tmp_path, "work", "ms_credentials")) == (
        "work-secret"
    )
    assert vault.get_password(_SERVICE, _account(tmp_path, "work", "client_id")) == "cid"


def test_two_config_dirs_do_not_share_an_item(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    first, second = tmp_path / "a", tmp_path / "b"
    vault.set_password(_SERVICE, _account(first), "one")
    vault.set_password(_SERVICE, _account(second), "two")
    assert len(backend.items) == 2
    assert vault.get_password(_SERVICE, _account(first)) == "one"
    assert vault.get_password(_SERVICE, _account(second)) == "two"


def test_a_legacy_item_is_copied_in_once_and_removed(tmp_path: Path) -> None:
    backend = _Backend()
    account = _account(tmp_path)
    backend.items[(_SERVICE, account)] = "legacy-secret"
    vault = VaultKeyring(backend)

    assert vault.get_password(_SERVICE, account) == "legacy-secret"

    assert (_SERVICE, account) not in backend.items
    assert json.loads(backend.items[(_SERVICE, _vault_account(tmp_path))])[account] == (
        "legacy-secret"
    )


def test_an_account_that_never_existed_is_probed_once_not_on_every_miss(tmp_path: Path) -> None:
    backend = _Backend()
    account = _account(tmp_path, slot="client_secret")
    assert VaultKeyring(backend).get_password(_SERVICE, account) is None

    # A new process: nothing cached, only what the keychain itself remembers.
    keychain_vault.clear_cache()
    backend.calls.clear()
    assert VaultKeyring(backend).get_password(_SERVICE, account) is None

    assert ("get", account) not in backend.calls


def test_deleting_the_last_secret_leaves_no_item_behind(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    account = _account(tmp_path)
    assert vault.get_password(_SERVICE, _account(tmp_path, slot="other")) is None
    vault.set_password(_SERVICE, account, "v")
    vault.delete_password(_SERVICE, account)
    assert backend.items == {}


def test_a_failed_legacy_cleanup_fails_the_delete(tmp_path: Path) -> None:
    backend = _Backend()
    account = _account(tmp_path)
    backend.items[(_SERVICE, account)] = "legacy"
    real_delete = backend.delete_password

    def fail_on_legacy(service: str, acct: str) -> None:
        if acct == account:
            raise RuntimeError("keychain refused")
        real_delete(service, acct)

    backend.delete_password = fail_on_legacy  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="keychain refused"):
        VaultKeyring(backend).delete_password(_SERVICE, account)


def test_a_failed_vault_delete_is_not_swallowed(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    account = _account(tmp_path)
    vault.set_password(_SERVICE, account, "v")

    def refuse(service: str, acct: str) -> None:
        raise RuntimeError("keychain refused")

    backend.delete_password = refuse  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="keychain refused"):
        vault.delete_password(_SERVICE, account)


def test_a_write_without_the_lock_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(keychain_vault, "_open_lock_file", lambda _directory: None)
    with pytest.raises(OSError, match="keychain lock"):
        VaultKeyring(_Backend()).set_password(_SERVICE, _account(tmp_path), "v")


def test_one_run_reads_the_keychain_item_once(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    accounts = [_account(tmp_path, slot=slot) for slot in ("ms_credentials", "client_id", "x")]
    for account in accounts:
        vault.set_password(_SERVICE, account, "v")
    keychain_vault.clear_cache()
    backend.calls.clear()

    for account in accounts * 3:
        assert vault.get_password(_SERVICE, account) == "v"

    assert backend.calls == [("get", _vault_account(tmp_path))]


def test_delete_removes_the_secret_and_the_empty_item(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    account = _account(tmp_path)
    vault.set_password(_SERVICE, account, "v")

    vault.delete_password(_SERVICE, account)

    assert backend.items == {}
    assert vault.get_password(_SERVICE, account) is None


def test_delete_also_removes_a_legacy_item_that_was_never_migrated(tmp_path: Path) -> None:
    backend = _Backend()
    account = _account(tmp_path)
    backend.items[(_SERVICE, account)] = "legacy"
    VaultKeyring(backend).delete_password(_SERVICE, account)
    assert backend.items == {}


def test_deleting_what_is_not_there_raises_the_backends_not_found(tmp_path: Path) -> None:
    vault = VaultKeyring(_Backend())
    with pytest.raises(_NotFound):
        vault.delete_password(_SERVICE, _account(tmp_path))


def test_delete_keeps_a_sibling_secret(tmp_path: Path) -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    keep, drop = _account(tmp_path, slot="a"), _account(tmp_path, slot="b")
    vault.set_password(_SERVICE, keep, "keep")
    vault.set_password(_SERVICE, drop, "drop")

    vault.delete_password(_SERVICE, drop)

    assert vault.get_password(_SERVICE, keep) == "keep"
    assert vault.get_password(_SERVICE, drop) is None


def test_a_foreign_account_name_goes_straight_to_the_backend() -> None:
    backend = _Backend()
    vault = VaultKeyring(backend)
    vault.set_password(_SERVICE, "not-json", "v")
    assert backend.items == {(_SERVICE, "not-json"): "v"}
    assert vault.get_password(_SERVICE, "not-json") == "v"


def test_the_facade_exposes_the_backends_errors_and_other_attributes() -> None:
    backend = _Backend()
    backend.get_keyring = lambda: "kr"  # type: ignore[attr-defined]
    vault = VaultKeyring(backend)
    assert vault.errors is not None
    assert vault.errors.PasswordDeleteError is _NotFound
    assert vault.get_keyring() == "kr"


def test_a_garbled_vault_reads_as_empty_not_as_a_crash(tmp_path: Path) -> None:
    backend = _Backend()
    backend.items[(_SERVICE, _vault_account(tmp_path))] = "{not json"
    assert VaultKeyring(backend).get_password(_SERVICE, _account(tmp_path)) is None


def test_the_vault_only_applies_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert keychain_vault.vault_applies() is False
    monkeypatch.setattr(sys, "platform", "darwin")
    assert keychain_vault.vault_applies() is True


@pytest.mark.skipif(keychain_vault.fcntl is None, reason="flock is POSIX only")
def test_the_lock_file_is_private(tmp_path: Path) -> None:
    VaultKeyring(_Backend()).set_password(_SERVICE, _account(tmp_path), "v")
    lock = tmp_path / ".keychain.lock"
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- cross-process writes


class _FileBackend:
    """A keyring whose single item lives in a JSON file, shared by several processes."""

    class errors:  # noqa: N801
        PasswordDeleteError = _NotFound

    def __init__(self, path: Path) -> None:
        self.path = path

    def _all(self) -> dict[str, str]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def delete_password(self, service: str, account: str) -> None:
        data = self._all()
        if account not in data:
            raise _NotFound(account)
        del data[account]
        self.path.write_text(json.dumps(data))

    def get_password(self, service: str, account: str) -> str | None:
        return self._all().get(account)

    def set_password(self, service: str, account: str, password: str) -> None:
        data = self._all()
        data[account] = password
        self.path.write_text(json.dumps(data))


def _write_many(config_dir: str, store: str, prefix: str, count: int) -> None:
    vault = VaultKeyring(_FileBackend(Path(store)))
    for i in range(count):
        account = json.dumps([config_dir, "p", f"{prefix}{i}"], separators=(",", ":"))
        vault.set_password(_SERVICE, account, f"{prefix}{i}")


@pytest.mark.skipif(keychain_vault.fcntl is None, reason="flock is POSIX only")
def test_concurrent_processes_do_not_drop_each_others_writes(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    store = tmp_path / "store.json"
    ctx = multiprocessing.get_context("spawn")
    workers: list[Any] = [
        ctx.Process(target=_write_many, args=(str(config_dir), str(store), prefix, 15))
        for prefix in ("a", "b", "c", "d")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0

    keychain_vault.clear_cache()
    vault = VaultKeyring(_FileBackend(store))
    for prefix in ("a", "b", "c", "d"):
        for i in range(15):
            account = json.dumps([str(config_dir), "p", f"{prefix}{i}"], separators=(",", ":"))
            assert vault.get_password(_SERVICE, account) == f"{prefix}{i}"


# ---------------------------------------------------------------- transactions and cache


def test_a_transaction_reads_what_another_process_wrote_not_the_cache(tmp_path: Path) -> None:
    backend = _Backend()
    account = _account(tmp_path)
    mine = VaultKeyring(backend)
    mine.set_password(_SERVICE, account, "old")
    assert mine.get_password(_SERVICE, account) == "old"  # cached

    # Another process rewrites the item behind this process's cache.
    backend.items[(_SERVICE, _vault_account(tmp_path))] = json.dumps(
        {account: "new", "_legacy_checked": [account]}
    )

    assert mine.get_password(_SERVICE, account) == "old"  # still inside the TTL
    with mine.transaction(_SERVICE, account):
        assert mine.get_password(_SERVICE, account) == "new"


def test_a_transaction_can_write_without_waiting_on_its_own_lock(tmp_path: Path) -> None:
    vault = VaultKeyring(_Backend())
    account = _account(tmp_path)
    with vault.transaction(_SERVICE, account):
        vault.set_password(_SERVICE, account, "v")
        vault.delete_password(_SERVICE, account)


def test_a_slow_read_cannot_republish_a_value_deleted_meanwhile(tmp_path: Path) -> None:
    backend = _Backend()
    account = _account(tmp_path)
    vault = VaultKeyring(backend)
    vault.set_password(_SERVICE, account, "secret")
    keychain_vault.clear_cache()
    real_get = backend.get_password
    deleted = False

    def get_then_delete(service: str, acct: str) -> str | None:
        nonlocal deleted
        value = real_get(service, acct)
        if not deleted and acct == _vault_account(tmp_path):
            deleted = True
            VaultKeyring(backend).delete_password(_SERVICE, account)
        return value

    backend.get_password = get_then_delete  # type: ignore[method-assign]
    assert vault.get_password(_SERVICE, account) == "secret"  # the old read returns...
    assert vault.get_password(_SERVICE, account) is None  # ...but is not cached over the delete


def test_waiting_on_a_stuck_thread_lock_times_out(tmp_path: Path, monkeypatch) -> None:
    import threading

    monkeypatch.setattr(keychain_vault, "_LOCK_TIMEOUT_SECONDS", 0.2)
    holder_ready, release = threading.Event(), threading.Event()
    vault = VaultKeyring(_Backend())
    account = _account(tmp_path)

    def hold() -> None:
        with vault.transaction(_SERVICE, account):
            holder_ready.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holder_ready.wait(timeout=5)
    try:
        with pytest.raises(TimeoutError):
            vault.set_password(_SERVICE, account, "v")
    finally:
        release.set()
        thread.join(timeout=5)


def test_bundle_writes_hold_the_vault_lock_and_plain_keyrings_get_none(tmp_path: Path) -> None:
    from blumkin import secret_store

    account = _account(tmp_path, slot="ms_credentials")
    seen: list[bool] = []
    backend = _Backend()
    real_get = backend.get_password

    def spying_get(service: str, acct: str) -> str | None:
        seen.append(keychain_vault._holds_lock(service, str(tmp_path)))
        return real_get(service, acct)

    backend.get_password = spying_get  # type: ignore[method-assign]
    secret_store._bundle_write_kind(VaultKeyring(backend), account, "token_cache", "t")
    assert seen and all(seen)

    plain = _Backend()
    secret_store._bundle_write_kind(plain, account, "token_cache", "t")
    assert json.loads(plain.items[(_SERVICE, account)]) == {"token_cache": "t"}


def test_an_abandoned_helper_takes_the_lock_once_its_caller_has_released_it(
    tmp_path: Path,
) -> None:
    import threading
    import time

    from blumkin import secret_store

    backend = _Backend()
    vault = VaultKeyring(backend)
    account = _account(tmp_path)
    proceed = threading.Event()
    errors: list[BaseException] = []

    def slow_set(service: str, acct: str, value: str) -> None:
        proceed.wait(timeout=5)
        try:
            vault.set_password(service, acct, value)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with vault.transaction(_SERVICE, account):
        with pytest.raises(TimeoutError):
            secret_store._call_keyring_with_timeout(slow_set, _SERVICE, account, "v", timeout=0.1)
    # The caller is out. Another writer now holds the lock: the abandoned helper must wait.
    with vault.transaction(_SERVICE, account):
        proceed.set()
        time.sleep(0.3)
        assert backend.items == {}, "the abandoned helper wrote without the lock"
    deadline = time.monotonic() + 5
    while not backend.items and time.monotonic() < deadline:
        time.sleep(0.05)
    assert errors == []
    assert vault.get_password(_SERVICE, account) == "v"
