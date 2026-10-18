"""``keychain_unlock`` modes and the locked-keychain detector (issue #444)."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from typing import Any

import pytest

from blumkin import keychain_unlock
from blumkin.config import _keychain_unlock_preference

# Captured before the autouse fixture patches `gui_available` for every test.
_REAL_GUI_AVAILABLE = keychain_unlock.gui_available
_REAL_NATIVE_LOCK_STATE = keychain_unlock._native_lock_state


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    keychain_unlock._reset_for_tests()
    monkeypatch.setattr(keychain_unlock, "_is_macos", lambda: True)
    monkeypatch.setattr(keychain_unlock, "_native_lock_state", lambda: None)
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
    monkeypatch.setattr(keychain_unlock, "_is_macos", lambda: False)
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    monkeypatch.setattr(
        keychain_unlock, "keychain_is_locked", lambda: pytest.fail("probed off macOS")
    )
    keychain_unlock.ensure_unlocked("cli")
    assert real_probe() is False


@pytest.mark.parametrize(("mode", "gui"), [("cli", True), ("gui", False)])
def test_macos_without_security_fails_closed(
    monkeypatch: pytest.MonkeyPatch, mode: str, gui: bool
) -> None:
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    monkeypatch.setattr(keychain_unlock, "gui_available", lambda: gui)
    with pytest.raises(keychain_unlock.KeychainLockedError, match="`security` tool was not found"):
        keychain_unlock.ensure_unlocked(mode)


@pytest.mark.parametrize(("mode", "gui"), [("off", False), ("gui", True)])
def test_macos_without_security_is_noop_when_unlock_is_not_in_play(
    monkeypatch: pytest.MonkeyPatch, mode: str, gui: bool
) -> None:
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    monkeypatch.setattr(keychain_unlock, "gui_available", lambda: gui)
    keychain_unlock.ensure_unlocked(mode)


def test_macos_without_security_counts_as_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    assert keychain_unlock.keychain_is_locked() is True


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


def test_probe_unrunnable_security_is_not_reported_unlocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_run(monkeypatch, FileNotFoundError("security"))
    assert keychain_unlock.keychain_lock_state() is None
    assert keychain_unlock.keychain_is_locked() is True


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


def test_probe_and_unlock_both_target_the_default_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # keyring's SecItemAdd / SecItemCopyMatching take no keychain selector, so they use
    # the default keychain; naming no path makes `security` resolve that same keychain.
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(keychain_unlock.subprocess, "run", run)
    keychain_unlock.keychain_lock_state()
    keychain_unlock.unlock()
    assert seen == [["security", "show-keychain-info"], ["security", "unlock-keychain"]]


def test_unlock_timeout_counts_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, subprocess.TimeoutExpired(cmd="security", timeout=120.0))
    assert keychain_unlock.unlock() is False


def test_available_reflects_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    assert keychain_unlock.available() is True
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    assert keychain_unlock.available() is False


def test_auth_unlock_reports_missing_security_instead_of_unlocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from blumkin.cli import main

    monkeypatch.setattr(keychain_unlock.sys, "platform", "darwin")
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    result = CliRunner().invoke(main, ["auth", "unlock"])
    assert result.exit_code != 0
    assert "security" in result.output and "already unlocked" not in result.output


def test_lock_state_is_tri_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch, subprocess.CompletedProcess([], 0, "no-timeout", ""))
    assert keychain_unlock.keychain_lock_state() is False
    _fake_run(monkeypatch, subprocess.CompletedProcess([], 36, "", "locked"))
    assert keychain_unlock.keychain_lock_state() is True
    _fake_run(monkeypatch, FileNotFoundError("security"))
    assert keychain_unlock.keychain_lock_state() is None
    monkeypatch.setattr(keychain_unlock, "_supported", lambda: False)
    assert keychain_unlock.keychain_lock_state() is None


def test_auth_unlock_json_reports_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from click.testing import CliRunner

    from blumkin.cli import main

    monkeypatch.setattr(keychain_unlock.sys, "platform", "darwin")
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: False)
    result = CliRunner().invoke(main, ["auth", "unlock", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"ok": True, "status": "already_unlocked"}


def test_auth_unlock_json_errors_are_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from click.testing import CliRunner

    from blumkin.cli import main

    monkeypatch.setattr(keychain_unlock.sys, "platform", "darwin")
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: True)
    result = CliRunner().invoke(main, ["auth", "unlock", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False and payload["error"] == "keychain_locked"

    class TtyRunner(CliRunner):
        @contextlib.contextmanager
        def isolation(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            with super().isolation(*args, **kwargs) as streams:
                monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
                yield streams

    monkeypatch.setattr(keychain_unlock, "unlock", lambda: False)
    result = TtyRunner().invoke(main, ["auth", "unlock", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"] == "keychain_unlock_failed"


def test_a_locked_finding_is_remembered_so_one_command_probes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(keychain_unlock.time, "monotonic", lambda: clock[0])
    probes: list[int] = []
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: probes.append(1) or True)
    _set_tty(monkeypatch, tty=False)
    for _ in range(3):
        with pytest.raises(keychain_unlock.KeychainLockedError, match="blumkin auth unlock"):
            keychain_unlock.ensure_unlocked("cli")
    assert probes == [1]
    clock[0] += keychain_unlock._LOCKED_CACHE_SECONDS + 1
    with pytest.raises(keychain_unlock.KeychainLockedError):
        keychain_unlock.ensure_unlocked("cli")
    assert probes == [1, 1]


def test_a_failed_unlock_is_remembered_too(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: True)
    _set_tty(monkeypatch, tty=True)
    monkeypatch.setattr(keychain_unlock, "unlock", lambda: calls.append(1) or False)
    for _ in range(2):
        with pytest.raises(keychain_unlock.KeychainLockedError):
            keychain_unlock.ensure_unlocked("cli")
    assert calls == [1]


def test_an_unlock_clears_the_locked_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(keychain_unlock.time, "monotonic", lambda: clock[0])
    locked = [True]
    monkeypatch.setattr(keychain_unlock, "keychain_is_locked", lambda: locked[0])
    _set_tty(monkeypatch, tty=False)
    with pytest.raises(keychain_unlock.KeychainLockedError):
        keychain_unlock.ensure_unlocked("cli")
    clock[0] += keychain_unlock._LOCKED_CACHE_SECONDS + 1
    locked[0] = False
    keychain_unlock.ensure_unlocked("cli")
    assert keychain_unlock.explain_if_locked("Prefix", "default") == "default"


def test_explain_if_locked_only_reflects_a_recent_locked_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        keychain_unlock, "keychain_is_locked", lambda: pytest.fail("explain must not probe")
    )
    assert keychain_unlock.explain_if_locked("Prefix", "default") == "default"
    monkeypatch.setattr(keychain_unlock, "_locked_at", keychain_unlock.time.monotonic())
    text = keychain_unlock.explain_if_locked("Prefix", "default")
    assert text.startswith("Prefix: ") and "blumkin auth unlock" in text


class _FakeFramework:
    """Stands in for a ``ctypes.CDLL``: functions write through ``byref`` like the C API.

    Plain closures (not bound methods) so ``argtypes`` can be assigned, as on real ctypes
    function pointers.
    """

    def __init__(self, *, copy_rc: int = 0, status_rc: int = 0, status: int = 0) -> None:
        self.released: list[Any] = []

        def copy_default(ref: Any) -> int:
            ref._obj.value = 4242
            return copy_rc

        def get_status(_keychain: Any, ref: Any) -> int:
            ref._obj.value = status
            return status_rc

        def release(keychain: Any) -> None:
            self.released.append(keychain.value)

        self.SecKeychainCopyDefault = copy_default
        self.SecKeychainGetStatus = get_status
        self.CFRelease = release


@pytest.mark.parametrize(
    ("status", "expected"),
    [(0b001, False), (0b111, False), (0b110, True), (0, True)],
)
def test_native_lock_state_reads_the_unlock_bit(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: bool
) -> None:
    fake = _FakeFramework(status=status)
    monkeypatch.setattr(keychain_unlock.ctypes, "CDLL", lambda _path: fake)
    assert _REAL_NATIVE_LOCK_STATE() is expected
    assert fake.released == [4242]


@pytest.mark.parametrize(
    "fake", [_FakeFramework(copy_rc=-1), _FakeFramework(status_rc=-25295)], ids=["copy", "status"]
)
def test_native_lock_state_is_unknown_when_the_api_fails(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeFramework
) -> None:
    monkeypatch.setattr(keychain_unlock.ctypes, "CDLL", lambda _path: fake)
    assert _REAL_NATIVE_LOCK_STATE() is None


def test_native_lock_state_is_unknown_when_the_framework_will_not_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_path: str) -> Any:
        raise OSError("no framework")

    monkeypatch.setattr(keychain_unlock.ctypes, "CDLL", fail)
    assert _REAL_NATIVE_LOCK_STATE() is None


def test_native_lock_state_is_unknown_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_unlock, "_is_macos", lambda: False)
    monkeypatch.setattr(
        keychain_unlock.ctypes, "CDLL", lambda _path: pytest.fail("loaded off macOS")
    )
    assert _REAL_NATIVE_LOCK_STATE() is None


@pytest.mark.parametrize("native", [True, False])
def test_lock_state_prefers_the_native_answer_and_never_runs_security(
    monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    monkeypatch.setattr(keychain_unlock, "_native_lock_state", lambda: native)
    monkeypatch.setattr(
        keychain_unlock.subprocess, "run", lambda *_a, **_k: pytest.fail("ran security")
    )
    assert keychain_unlock.keychain_lock_state() is native
    assert keychain_unlock.keychain_is_locked() is native


def test_lock_state_falls_back_to_security_when_native_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keychain_unlock, "_native_lock_state", lambda: None)
    _fake_run(monkeypatch, subprocess.CompletedProcess([], 0, "no-timeout", ""))
    assert keychain_unlock.keychain_lock_state() is False
