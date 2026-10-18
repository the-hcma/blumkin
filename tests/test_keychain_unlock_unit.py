"""``keychain_unlock`` modes and the locked-keychain detector (issue #444)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from blumkin import keychain_unlock
from blumkin.config import _keychain_unlock_preference

# Captured before the autouse fixture patches `gui_available` for every test.
_REAL_GUI_AVAILABLE = keychain_unlock.gui_available


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    keychain_unlock._reset_for_tests()
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: True)
    monkeypatch.setattr(keychain_unlock, "gui_available", lambda: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)


def _set_tty(monkeypatch: pytest.MonkeyPatch, *, tty: bool) -> None:
    monkeypatch.setattr(keychain_unlock.sys.stdin, "isatty", lambda: tty)


@pytest.mark.parametrize("mode", ["gui", "off"])
def test_non_cli_modes_never_probe(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(
        keychain_unlock, "keychain_is_locked", lambda: pytest.fail("probed in non-cli mode")
    )
    keychain_unlock.ensure_unlocked(mode)


def test_cli_mode_unlocked_is_noop_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: calls.append(1) or False)
    keychain_unlock.ensure_unlocked("cli")
    keychain_unlock.ensure_unlocked("cli")
    assert calls == [1]


def test_cli_mode_locked_without_tty_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: True)
    _set_tty(monkeypatch, tty=False)
    monkeypatch.setattr(keychain_unlock, "unlock", lambda: pytest.fail("unlock without a TTY"))
    with pytest.raises(keychain_unlock.KeychainLockedError, match="blumkin auth unlock"):
        keychain_unlock.ensure_unlocked("cli")


def test_cli_mode_locked_with_tty_unlocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: True)
    _set_tty(monkeypatch, tty=True)
    monkeypatch.setattr(keychain_unlock, "unlock", lambda: True)
    keychain_unlock.ensure_unlocked("cli")


def test_cli_mode_failed_unlock_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: True)
    _set_tty(monkeypatch, tty=True)
    monkeypatch.setattr(keychain_unlock, "unlock", lambda: False)
    with pytest.raises(keychain_unlock.KeychainLockedError, match="unlock failed"):
        keychain_unlock.ensure_unlocked("cli")


def test_unsupported_platform_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    real_probe = keychain_unlock.keychain_is_locked
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    monkeypatch.setattr(
        keychain_unlock, "keychain_is_locked", lambda: pytest.fail("probed off macOS")
    )
    keychain_unlock.ensure_unlocked("cli")
    assert real_probe() is False


def _fake_run(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    def run(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(keychain_unlock.subprocess, "run", run)


def test_probe_timeout_means_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, subprocess.TimeoutExpired(cmd="security", timeout=2.0))
    assert keychain_unlock.keychain_is_locked() is True


def test_probe_interaction_not_allowed_means_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    done = subprocess.CompletedProcess([], 36, "", "User interaction is not allowed.")
    _fake_run(monkeypatch, done)
    assert keychain_unlock.keychain_is_locked() is True


def test_probe_success_means_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, subprocess.CompletedProcess([], 0, "no-timeout", ""))
    assert keychain_unlock.keychain_is_locked() is False


def test_probe_missing_binary_means_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, FileNotFoundError("security"))
    assert keychain_unlock.keychain_is_locked() is False


def test_unlock_never_passes_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(keychain_unlock.subprocess, "run", run)
    assert keychain_unlock.unlock() is True
    assert seen == [["security", "unlock-keychain"]]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cli", "cli"),
        (" CLI ", "cli"),
        ("off", "off"),
        ("gui", "gui"),
        ("bogus", "gui"),
        (3, "gui"),
    ],
)
def test_config_preference_parsing(raw: Any, expected: str) -> None:
    assert _keychain_unlock_preference({"keychain_unlock": raw}) == expected
    assert _keychain_unlock_preference({}) == "gui"


def test_gui_mode_without_a_display_behaves_like_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_unlock, "gui_available", lambda: False)
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: True)
    _set_tty(monkeypatch, tty=False)
    with pytest.raises(keychain_unlock.KeychainLockedError, match="blumkin auth unlock"):
        keychain_unlock.ensure_unlocked("gui")


def test_off_mode_without_a_display_still_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_unlock, "gui_available", lambda: False)
    monkeypatch.setattr(
        keychain_unlock, "keychain_is_locked", lambda: pytest.fail("probed in off mode")
    )
    keychain_unlock.ensure_unlocked("off")


def test_ssh_env_is_only_a_fallback_when_launchctl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")
    _fake_run(monkeypatch, FileNotFoundError("launchctl"))
    assert _REAL_GUI_AVAILABLE() is False


def test_inherited_ssh_env_does_not_hide_an_aqua_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")
    _fake_run(monkeypatch, subprocess.CompletedProcess([], 0, "Aqua\n", ""))
    assert _REAL_GUI_AVAILABLE() is True


@pytest.mark.parametrize(
    ("manager", "expected"),
    [("Aqua\n", True), ("Background\n", False), ("System\n", False), ("", True)],
)
def test_gui_available_follows_launchctl_session(
    monkeypatch: pytest.MonkeyPatch, manager: str, expected: bool
) -> None:
    _fake_run(monkeypatch, subprocess.CompletedProcess([], 0, manager, ""))
    assert _REAL_GUI_AVAILABLE() is expected


def test_gui_availability_is_probed_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def run(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        return subprocess.CompletedProcess([], 0, "Aqua\n", "")

    monkeypatch.setattr(keychain_unlock.subprocess, "run", run)
    assert _REAL_GUI_AVAILABLE() is True
    assert _REAL_GUI_AVAILABLE() is True
    assert calls == [1]


def test_a_successful_check_expires_so_a_relock_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(keychain_unlock.time, "monotonic", lambda: clock[0])
    probes: list[int] = []
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: probes.append(1) or False)
    keychain_unlock.ensure_unlocked("cli")
    clock[0] += keychain_unlock._RECHECK_AFTER_SECONDS - 1
    keychain_unlock.ensure_unlocked("cli")
    assert probes == [1]
    clock[0] += 2
    keychain_unlock.ensure_unlocked("cli")
    assert probes == [1, 1]


def test_probe_any_nonzero_exit_is_treated_as_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, subprocess.CompletedProcess([], 51, "", "authentication failed"))
    assert keychain_unlock.keychain_is_locked() is True
