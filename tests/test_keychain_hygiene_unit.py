"""Stale keychain items and the temporary-directory guard (#759)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from blumkin import keychain_gate, keychain_hygiene
from blumkin.cli import main
from blumkin.keychain_hygiene import KeychainRefusedError, StaleItem
from blumkin.keychain_vault import VaultKeyring

_DUMP = """keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>="blumkin"
    0x00000008 <blob>=<NULL>
    "acct"<blob>="work:token_cache"
    "cdat"<timedate>=0x32303236  "20260915200513Z\\000"
    "svce"<blob>="blumkin"
    "type"<uint32>=<NULL>
keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="["/private/var/folders/jd/T/pytest-of-x/pytest-1/t0","default","token_cache"]"
    "svce"<blob>="blumkin"
keychain: "/Users/x/Library/Keychains/login.keychain-db"
class: "genp"
attributes:
    "acct"<blob>="someone@example.com"
    "svce"<blob>="other-app"
"""


def test_the_dump_parser_reads_blumkin_accounts_only() -> None:
    keychain = "/Users/x/Library/Keychains/login.keychain-db"
    assert keychain_hygiene._accounts_from_dump(_DUMP) == [
        ("work:token_cache", keychain),
        ('["/private/var/folders/jd/T/pytest-of-x/pytest-1/t0","default","token_cache"]', keychain),
    ]


def test_the_old_naming_is_stale() -> None:
    assert "old per-kind" in (keychain_hygiene.classify("default:google_token") or "")
    assert "old per-kind" in (keychain_hygiene.classify('"just a string"') or "")


def test_a_temporary_config_dir_is_stale(tmp_path: Path) -> None:
    account = json.dumps([str(tmp_path), "default", "token_cache"])
    assert "temporary" in (keychain_hygiene.classify(account) or "")


def test_a_config_dir_that_is_merely_missing_is_not_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unmounted volume or a moved directory still owns the secrets in its item."""
    monkeypatch.setattr(keychain_hygiene, "is_temporary", lambda _d: False)
    for slot in ("ms_credentials", "vault"):
        account = json.dumps(["/no/such/dir/anywhere", "work", slot])
        assert keychain_hygiene.classify(account) is None


def test_standalone_items_of_a_real_config_dir_are_never_stale_before_they_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the old profile:kind naming, which nothing reads, is stale. A JSON-named item of a
    real config dir may still be the only copy of a secret until it is first read."""
    monkeypatch.setattr(keychain_hygiene, "is_temporary", lambda _d: False)
    for slot in ("ms_credentials", "google_token", "ms_client_id", "google_client_secret"):
        assert keychain_hygiene.classify(json.dumps([str(tmp_path), "work", slot])) is None


def test_a_hex_dumped_account_is_decoded() -> None:
    account = '["/private/tmp/x","default","token_cache"]'
    dump = (
        'keychain: "/x/login.keychain-db"\nattributes:\n'
        f'    "acct"<blob>=0x{account.encode().hex().upper()}  "escaped text"\n'
        '    "svce"<blob>="blumkin"\n'
    )
    assert keychain_hygiene._accounts_from_dump(dump) == [(account, "/x/login.keychain-db")]


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


def test_an_item_of_an_existing_real_config_dir_is_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keychain_hygiene, "is_temporary", lambda _d: False)
    for slot in ("ms_credentials", "vault"):
        account = json.dumps([str(tmp_path), "work", slot])
        assert keychain_hygiene.classify(account) is None


def test_stale_items_lists_each_once_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        keychain_hygiene,
        "list_accounts",
        lambda: [
            ("work:token_cache", "/k/login.keychain-db"),
            ("work:token_cache", "/k/login.keychain-db"),
            ("personal:google_token", "/k/login.keychain-db"),
        ],
    )
    items = keychain_hygiene.stale_items()
    assert items is not None
    assert [i.account for i in items] == ["personal:google_token", "work:token_cache"]


def test_stale_items_is_none_when_the_keychain_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keychain_hygiene, "list_accounts", lambda: None)
    assert keychain_hygiene.stale_items() is None


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


# ------------------------------------------------------------------ the command


def _stale(monkeypatch: pytest.MonkeyPatch, *accounts: str) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        keychain_hygiene,
        "stale_items",
        lambda: [
            StaleItem(account=a, reason="old per-kind naming (profile:kind)") for a in accounts
        ],
    )


def test_prune_lists_without_removing_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _stale(monkeypatch, "work:token_cache")
    removed: list[str] = []
    monkeypatch.setattr(
        keychain_hygiene, "remove_item", lambda a, k=None: removed.append(a) or True
    )

    result = CliRunner().invoke(main, ["auth", "keychain-prune", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [s["account"] for s in payload["stale"]] == ["work:token_cache"]
    assert payload["removed"] == [] and removed == []
    assert payload["next_step"] == "blumkin auth keychain-prune --yes"


def test_prune_yes_removes_each_stale_item_from_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stale(monkeypatch, "work:token_cache", "personal:google_token")
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    removed: list[str] = []
    monkeypatch.setattr(
        keychain_hygiene, "remove_item", lambda a, k=None: removed.append(a) or True
    )

    result = CliRunner().invoke(main, ["auth", "keychain-prune", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert removed == ["work:token_cache", "personal:google_token"]
    assert json.loads(result.stdout)["removed"] == removed


def test_prune_yes_needs_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    _stale(monkeypatch, "work:token_cache")
    monkeypatch.setattr(keychain_gate, "interactive", lambda: False)
    result = CliRunner().invoke(main, ["auth", "keychain-prune", "--yes", "--json"])
    assert result.exit_code == 2
    assert "terminal" in result.output


def test_prune_reports_an_item_it_could_not_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    _stale(monkeypatch, "work:token_cache")
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr(keychain_hygiene, "remove_item", lambda a, k=None: False)
    result = CliRunner().invoke(main, ["auth", "keychain-prune", "--yes", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["failed"] == ["work:token_cache"]


def test_prune_with_nothing_stale_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _stale(monkeypatch)
    result = CliRunner().invoke(main, ["auth", "keychain-prune"])
    assert result.exit_code == 0
    assert "no stale keychain items" in result.output


def test_prune_when_the_keychain_cannot_be_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(keychain_hygiene, "stale_items", lambda: None)
    result = CliRunner().invoke(main, ["auth", "keychain-prune", "--json"])
    assert result.exit_code == 1
    assert "keychain_unavailable" in result.output


def test_prune_is_a_no_op_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    result = CliRunner().invoke(main, ["auth", "keychain-prune", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "not_macos"


def test_the_same_account_in_two_keychains_is_two_items_each_removed_from_its_own() -> None:
    dump = (
        'keychain: "/k/login.keychain-db"\n    "acct"<blob>="work:token_cache"\n'
        '    "svce"<blob>="blumkin"\n'
        'keychain: "/k/other.keychain-db"\n    "acct"<blob>="work:token_cache"\n'
        '    "svce"<blob>="blumkin"\n'
    )
    assert keychain_hygiene._accounts_from_dump(dump) == [
        ("work:token_cache", "/k/login.keychain-db"),
        ("work:token_cache", "/k/other.keychain-db"),
    ]


def test_remove_item_targets_the_keychain_it_was_found_in(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    seen: list[list[str]] = []

    def fake_run(command, **_kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert keychain_hygiene.remove_item("work:token_cache", "/k/other.keychain-db") is True
    assert seen[0][-1] == "/k/other.keychain-db"
    assert seen[0][:5] == ["security", "delete-generic-password", "-s", "blumkin", "-a"]


def test_a_temporary_vault_is_not_stale_when_the_override_says_it_is_used_on_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = json.dumps([str(tmp_path), "work", "vault"])
    assert keychain_hygiene.classify(account) is not None
    monkeypatch.setenv(keychain_hygiene.ALLOW_TEMP_ENV, "1")
    assert keychain_hygiene.classify(account) is None
