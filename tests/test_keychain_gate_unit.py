"""Ask for macOS keychain access once, from a terminal, never from the background (#759)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from blumkin import keychain_gate, keychain_unlock, secret_store
from blumkin.cli import main
from blumkin.exit_codes import EXIT_AUTH
from blumkin.skills.errors import classify_exception


@pytest.fixture(autouse=True)
def _gate_on_a_mac_with_an_empty_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(keychain_gate.GATE_ENV, raising=False)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(keychain_gate, "_noticed", False)
    monkeypatch.setattr(keychain_gate, "_identity", "build-a")
    keychain_unlock._reset_for_tests()


def _background(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: False)


def test_a_background_process_is_refused_without_touching_the_keychain(monkeypatch) -> None:
    _background(monkeypatch)
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError, match="blumkin auth keychain"):
        keychain_gate.require()


def test_the_refusal_is_a_locked_keychain_error_so_callers_already_handle_it(
    monkeypatch,
) -> None:
    _background(monkeypatch)
    with pytest.raises(keychain_unlock.KeychainLockedError):
        keychain_unlock.ensure_unlocked("gui")


def test_errors_explain_why_after_a_refusal(monkeypatch) -> None:
    _background(monkeypatch)
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        keychain_gate.require()
    text = keychain_unlock.explain_if_locked("cannot refresh", "log in again")
    assert text.startswith("cannot refresh:") and "blumkin auth keychain" in text


def test_a_person_at_a_terminal_is_let_through_with_a_notice_once(monkeypatch, capsys) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    keychain_gate.require()
    keychain_gate.require()
    err = capsys.readouterr().err
    assert err.count("macOS may ask") == 1


def test_an_authorized_build_is_let_through_without_a_terminal(monkeypatch) -> None:
    _background(monkeypatch)
    keychain_gate.authorize()
    keychain_gate.require()
    assert keychain_gate.state() == "authorized"


def test_a_different_python_build_needs_authorizing_again(monkeypatch) -> None:
    _background(monkeypatch)
    keychain_gate.authorize()
    monkeypatch.setattr(keychain_gate, "_identity", "build-b")
    assert keychain_gate.state() == "needs_authorization"
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        keychain_gate.require()


def test_the_marker_is_private_and_names_the_build(tmp_path: Path) -> None:
    path = keychain_gate.authorize()
    assert path == tmp_path / keychain_gate.MARKER_NAME
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["identity"] == "build-a"


def test_a_garbled_marker_means_not_authorized(tmp_path: Path) -> None:
    (tmp_path / keychain_gate.MARKER_NAME).write_text("{nope")
    assert keychain_gate.is_authorized() is False


def test_the_gate_is_off_on_other_platforms_and_when_switched_off(monkeypatch) -> None:
    _background(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    keychain_gate.require()
    assert keychain_gate.state() == "not_applicable"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv(keychain_gate.GATE_ENV, "off")
    keychain_gate.require()


def test_the_identity_digest_follows_the_python_binary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(keychain_gate, "_identity", None)
    first = tmp_path / "py"
    first.write_bytes(b"one")
    monkeypatch.setattr(sys, "executable", str(first))
    one = keychain_gate.identity()
    monkeypatch.setattr(keychain_gate, "_identity", None)
    first.write_bytes(b"two")
    assert keychain_gate.identity() != one


# ------------------------------------------------------------------ errors and the command


def test_a_refusal_is_keychain_not_authorized_with_the_auth_exit_code() -> None:
    err = keychain_unlock.KeychainNotAuthorizedError("no terminal")
    info = classify_exception(err)
    assert (info.slug, info.exit_code) == ("keychain_not_authorized", EXIT_AUTH)

    wrapped = secret_store.SecretWriteError("cannot write token_cache: no terminal")
    wrapped.__cause__ = err
    assert classify_exception(wrapped).slug == "keychain_not_authorized"


def test_auth_keychain_refuses_without_a_terminal(monkeypatch) -> None:
    _background(monkeypatch)
    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])
    assert result.exit_code == 2
    assert "terminal" in result.output


def test_auth_keychain_authorizes_after_reading_every_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    touched: list[str] = []
    monkeypatch.setattr("blumkin.cli.list_profiles", lambda: [{"name": "work"}, {"name": "me"}])
    monkeypatch.setattr(
        "blumkin.cli.load_config", lambda profile=None: SimpleNamespace(profile=profile)
    )
    monkeypatch.setattr(
        secret_store, "touch_keychain", lambda cfg: touched.append(cfg.profile) or True
    )

    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])

    assert result.exit_code == 0, result.output
    assert touched == ["work", "me"]
    payload = json.loads(result.stdout)
    assert payload["status"] == "authorized" and payload["profiles"] == ["work", "me"]
    assert keychain_gate.is_authorized()


def test_auth_keychain_does_not_authorize_when_the_keychain_refuses(monkeypatch) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr("blumkin.cli.list_profiles", lambda: [{"name": "work"}])
    monkeypatch.setattr(
        "blumkin.cli.load_config", lambda profile=None: SimpleNamespace(profile=profile)
    )

    def refuse(_cfg) -> None:
        raise TimeoutError("keyring backend call timed out after 180.0s")

    monkeypatch.setattr(secret_store, "touch_keychain", refuse)
    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])
    assert result.exit_code == EXIT_AUTH
    assert "keychain_not_authorized" in result.output
    assert not keychain_gate.is_authorized()


def test_auth_keychain_is_a_no_op_off_macos(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "not_macos"


def test_touch_keychain_reads_each_secret_with_a_patient_timeout(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, float | None]] = []

    class _Ring:
        def get_password(self, service: str, account: str) -> None:
            return None

    def fake_call(func, service, account, timeout=None):
        calls.append((account, timeout))

    cfg = SimpleNamespace(
        config_dir=tmp_path, profile="work", token_storage="auto", keychain_unlock="off"
    )
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr(secret_store, "_backend_for", lambda _cfg: "keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _Ring())
    monkeypatch.setattr(secret_store, "_call_keyring_with_timeout", fake_call)

    secret_store.touch_keychain(cfg)  # type: ignore[arg-type]

    assert len(calls) == 4
    assert all(t == secret_store._AUTHORIZE_TIMEOUT_SECONDS for _a, t in calls)


def test_doctor_style_state_is_not_applicable_when_the_gate_is_switched_off(monkeypatch) -> None:
    monkeypatch.setenv(keychain_gate.GATE_ENV, "off")
    assert keychain_gate.state() == "not_applicable"


def test_auth_keychain_authorizes_nothing_when_no_profile_uses_the_keychain(monkeypatch) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr("blumkin.cli.list_profiles", lambda: [{"name": "work"}])
    monkeypatch.setattr(
        "blumkin.cli.load_config", lambda profile=None: SimpleNamespace(profile=profile)
    )
    monkeypatch.setattr(secret_store, "touch_keychain", lambda cfg: False)
    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "nothing_to_authorize"
    assert not keychain_gate.is_authorized()


def test_authorizing_clears_the_refusal_diagnostic(monkeypatch) -> None:
    _background(monkeypatch)
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        keychain_gate.require()
    keychain_gate.authorize()
    assert keychain_unlock.explain_if_locked("x", "default") == "default"


def test_touch_keychain_loads_the_shared_item_through_the_vault(monkeypatch, tmp_path) -> None:
    import json as _json

    from blumkin.keychain_vault import VaultKeyring, clear_cache

    class _Backend:
        class errors:  # noqa: N801
            PasswordDeleteError = KeyError

        def __init__(self) -> None:
            self.items: dict[tuple[str, str], str] = {}
            self.gets: list[str] = []

        def delete_password(self, service: str, account: str) -> None:
            del self.items[(service, account)]

        def get_password(self, service: str, account: str) -> str | None:
            self.gets.append(account)
            return self.items.get((service, account))

        def set_password(self, service: str, account: str, password: str) -> None:
            self.items[(service, account)] = password

    clear_cache()
    backend = _Backend()
    cfg = SimpleNamespace(
        config_dir=tmp_path, profile="work", token_storage="auto", keychain_unlock="off"
    )
    vault_account = _json.dumps([str(tmp_path.resolve()), "vault"], separators=(",", ":"))
    backend.items[("blumkin", vault_account)] = _json.dumps({"x": "y"})
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr(secret_store, "_backend_for", lambda _cfg: "keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: VaultKeyring(backend))

    assert secret_store.touch_keychain(cfg) is True  # type: ignore[arg-type]

    # One read of the shared item serves every account of the profile: nothing else is asked.
    assert backend.gets == [vault_account]


def test_auth_keychain_hint_gives_the_plain_command_and_the_agent_form(monkeypatch) -> None:
    _background(monkeypatch)
    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])
    hint = json.loads(result.output.strip().splitlines()[-1])["hint"]
    assert hint.startswith("run `blumkin auth keychain`")
    assert "`! blumkin auth keychain`" in hint


def test_a_long_lived_process_forgets_a_refusal_once_another_terminal_authorized(
    monkeypatch,
) -> None:
    _background(monkeypatch)
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        keychain_gate.require()
    assert keychain_unlock.explain_if_locked("x", "default") != "default"
    keychain_gate.authorize()  # what the other terminal's command writes
    keychain_unlock.note_unauthorized("stale")  # this process never saw it
    keychain_gate.require()
    assert keychain_unlock.explain_if_locked("x", "default") == "default"


def test_a_terminal_without_a_gui_session_cannot_answer_the_prompt(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(keychain_unlock, "gui_available", lambda: False)
    assert keychain_gate.interactive() is False
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError, match="GUI login session"):
        keychain_gate.require()
    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])
    assert result.exit_code == 2
    assert "GUI login session" in result.output


def test_a_terminal_in_a_gui_session_can(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(keychain_unlock, "gui_available", lambda: True)
    assert keychain_gate.interactive() is True


def test_a_refusal_is_not_reported_as_a_missing_login(monkeypatch) -> None:
    from blumkin.providers import google_auth

    _background(monkeypatch)
    assert "Silent token refresh failed" in google_auth._silent_refresh_failed_message()
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        keychain_gate.require()
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError, match="blumkin auth keychain"):
        google_auth._silent_refresh_failed_message()
    info = classify_exception(keychain_unlock.KeychainNotAuthorizedError("x"))
    assert info.slug == "keychain_not_authorized"


def test_auth_keychain_reports_a_keyring_error_that_is_not_an_oserror(monkeypatch) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr("blumkin.cli.list_profiles", lambda: [{"name": "work"}])
    monkeypatch.setattr(
        "blumkin.cli.load_config", lambda profile=None: SimpleNamespace(profile=profile)
    )

    class _KeyringLocked(Exception):  # keyring.errors.KeyringLocked is not an OSError either
        pass

    def deny(_cfg) -> None:
        raise _KeyringLocked("User denied the prompt")

    monkeypatch.setattr(secret_store, "touch_keychain", deny)
    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])
    assert result.exit_code == EXIT_AUTH
    assert "keychain_not_authorized" in result.output
    assert not keychain_gate.is_authorized()
