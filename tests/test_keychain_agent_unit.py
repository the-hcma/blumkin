"""The keychain reached through blumkin-agent (#773)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from blumkin import keychain_agent, keychain_gate, keychain_unlock, secret_store
from blumkin.agent import client as agent_client
from blumkin.keychain_agent import AgentKeyring, KeychainAgentError, KeychainAgentTimeoutError

# The conftest guard replaces `secret_store._keyring_module` for every test; keep the real one.
_REAL_KEYRING_MODULE = secret_store._keyring_module


class _Errors:
    class PasswordDeleteError(Exception):
        pass


class _Direct:
    errors = _Errors

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_keyring(self) -> str:
        return "real"

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", account))
        return "direct-value"

    def set_password(self, service: str, account: str, password: str) -> None:
        self.calls.append(("set", account))

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", account))


class _Agent:
    """A stand-in for ``agent_client.call`` that records requests."""

    def __init__(self, reply: dict[str, Any] | Exception) -> None:
        self.reply = reply
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, cmd: str, *, extra: dict[str, Any] | None = None, spawn: bool = True):
        self.requests.append((cmd, dict(extra or {})))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture(autouse=True)
def _darwin_and_a_gate_that_refuses_background(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The marker lives in the config dir: never read or write a developer's real one.
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(keychain_gate, "_identity", "build-a")
    monkeypatch.setattr(keychain_agent, "_in_use", False)
    monkeypatch.setattr(keychain_agent, "_fell_back_ever", False)
    monkeypatch.delenv(keychain_gate.GATE_ENV, raising=False)
    monkeypatch.delenv(keychain_agent.DISABLE_ENV, raising=False)  # conftest switches it off
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(keychain_gate, "interactive", lambda: False)
    keychain_unlock._reset_for_tests()


def _use(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] | Exception) -> _Agent:
    agent = _Agent(reply)
    monkeypatch.setattr(agent_client, "call", agent)
    return agent


def test_a_read_goes_to_the_agent_and_never_the_direct_keyring(monkeypatch) -> None:
    agent = _use(monkeypatch, {"ok": True, "found": True, "value": "the vault"})
    direct = _Direct()

    assert AgentKeyring(direct).get_password("blumkin", "acct") == "the vault"

    assert agent.requests == [("keychain_get", {"account": "acct", "service": "blumkin"})]
    assert direct.calls == []


def test_a_missing_item_reads_as_none(monkeypatch) -> None:
    _use(monkeypatch, {"ok": True, "found": False})
    assert AgentKeyring(_Direct()).get_password("blumkin", "acct") is None


def test_a_write_and_a_delete_go_to_the_agent(monkeypatch) -> None:
    agent = _use(monkeypatch, {"ok": True, "deleted": True})
    keyring = AgentKeyring(_Direct())

    keyring.set_password("blumkin", "acct", "v")
    keyring.delete_password("blumkin", "acct")

    assert [cmd for cmd, _ in agent.requests] == ["keychain_set", "keychain_delete"]
    assert agent.requests[0][1] == {"account": "acct", "service": "blumkin", "value": "v"}


def test_deleting_what_is_not_there_raises_the_keyrings_not_found(monkeypatch) -> None:
    _use(monkeypatch, {"ok": True, "deleted": False})
    with pytest.raises(_Errors.PasswordDeleteError):
        AgentKeyring(_Direct()).delete_password("blumkin", "acct")


@pytest.mark.parametrize(
    "error",
    ["presence_denied", "presence_timed_out", "presence_busy", "keychain_error", "invalid_request"],
)
def test_a_refusal_from_a_running_agent_is_an_error_not_a_fallback(monkeypatch, error) -> None:
    _use(monkeypatch, {"ok": False, "error": error, "message": "no"})
    direct = _Direct()

    with pytest.raises(KeychainAgentError, match=error):
        AgentKeyring(direct).get_password("blumkin", "acct")

    assert direct.calls == []


def test_an_agent_that_connected_but_did_not_answer_in_time_is_not_a_reason_to_fall_back(
    monkeypatch,
) -> None:
    _use(monkeypatch, agent_client.AgentUnreachableError("no reply"))
    direct = _Direct()

    with pytest.raises(KeychainAgentTimeoutError, match="prompt may be waiting"):
        AgentKeyring(direct).get_password("blumkin", "acct")

    assert direct.calls == []


def test_a_request_the_agent_may_have_handled_is_never_answered_by_the_direct_keyring(
    monkeypatch,
) -> None:
    _use(monkeypatch, agent_client.AgentReplyLostError("connection closed mid-reply"))
    direct = _Direct()
    keyring = AgentKeyring(direct)

    for call in (
        lambda: keyring.set_password("blumkin", "acct", "v"),
        lambda: keyring.delete_password("blumkin", "acct"),
        lambda: keyring.get_password("blumkin", "acct"),
    ):
        with pytest.raises(KeychainAgentTimeoutError, match="already have been handled"):
            call()

    assert direct.calls == []


def _auto_cfg(tmp_path: Path):
    from types import SimpleNamespace

    return SimpleNamespace(
        config_dir=tmp_path,
        profile="work",
        token_storage="auto",
        keychain_unlock="off",
        token_cache_path=tmp_path / "msal_token_cache.json",
        auth_record_path=tmp_path / "auth_record.json",
        google_token_path=tmp_path / "google_token.json",
    )


def test_a_declined_presence_prompt_on_a_write_is_not_answered_with_a_plaintext_file(
    monkeypatch, tmp_path: Path
) -> None:
    keyring = AgentKeyring(_Direct())
    keychain_agent.mark_in_use()
    monkeypatch.setattr(secret_store, "_backend_for", lambda _cfg: "keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: keyring)
    _use(monkeypatch, {"ok": False, "error": "presence_denied", "message": "declined"})

    with pytest.raises(secret_store.SecretWriteError, match="presence_denied"):
        secret_store._write_text_direct(_auto_cfg(tmp_path), "google_token", "secret")  # type: ignore[arg-type]

    assert not (tmp_path / "google_token.json").exists()


def test_a_declined_presence_prompt_on_a_logout_is_reported_not_swallowed(
    monkeypatch, tmp_path: Path
) -> None:
    keyring = AgentKeyring(_Direct())
    keychain_agent.mark_in_use()
    monkeypatch.setattr(secret_store, "_backend_for", lambda _cfg: "keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: keyring)
    # Every request is declined, the existence probe included.
    _use(monkeypatch, {"ok": False, "error": "presence_denied", "message": "declined"})

    with pytest.raises(secret_store.SecretWriteError, match="cannot delete"):
        secret_store._delete_keyring_entry(_auto_cfg(tmp_path), "google_token")  # type: ignore[arg-type]


def test_the_direct_fallback_of_auth_keychain_keeps_its_patient_budget(monkeypatch) -> None:
    import time
    from types import SimpleNamespace

    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(secret_store, "_AUTHORIZE_TIMEOUT_SECONDS", 1.0)
    budgets: list[float | None] = []

    def slow_direct(func, *args):
        budgets.append(secret_store._direct_budget_override_s)
        time.sleep(0.2)  # a person answering a prompt: longer than the ordinary budget

    class _Ring:
        def get_password(self, service: str, account: str) -> str:
            return secret_store._bound_direct_keyring_call(slow_direct, None) or ""

    cfg = SimpleNamespace(
        config_dir=Path("/x"), profile="work", token_storage="auto", keychain_unlock="off"
    )
    monkeypatch.setattr(secret_store, "_backend_for", lambda _cfg: "keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: _Ring())
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)

    assert secret_store.touch_keychain(cfg) is True  # type: ignore[arg-type]

    assert budgets and all(b == 1.0 for b in budgets)
    assert secret_store._direct_budget_override_s is None  # restored afterwards


def test_only_the_blumkin_service_is_asked_of_the_agent(monkeypatch) -> None:
    agent = _use(monkeypatch, {"ok": True, "found": False})
    with pytest.raises(KeychainAgentError, match="blumkin"):
        AgentKeyring(_Direct()).get_password("other-app", "acct")
    assert agent.requests == []


def test_with_no_agent_a_background_process_is_refused_by_the_gate(monkeypatch) -> None:
    _use(monkeypatch, agent_client.AgentUnavailableError("no agent"))
    direct = _Direct()

    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        AgentKeyring(direct).get_password("blumkin", "acct")

    assert direct.calls == []


def test_with_no_agent_an_authorized_process_uses_the_direct_keyring(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(keychain_gate, "_identity", "build-a")
    keychain_gate.authorize()
    _use(monkeypatch, agent_client.AgentUnavailableError("no agent"))
    direct = _Direct()

    assert AgentKeyring(direct).get_password("blumkin", "acct") == "direct-value"
    assert direct.calls == [("get", "acct")]


def test_attributes_the_wrapper_does_not_define_come_from_the_real_keyring() -> None:
    assert AgentKeyring(_Direct()).get_keyring() == "real"


def test_a_missing_agent_binary_or_another_platform_means_no_agent(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(keychain_agent, "binary_path", lambda: tmp_path / "missing")
    assert keychain_agent.applies() is False
    binary = tmp_path / "blumkin-agent"
    binary.write_text("x")
    monkeypatch.setattr(keychain_agent, "binary_path", lambda: binary)
    assert keychain_agent.applies() is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert keychain_agent.applies() is False


def test_the_environment_switches_the_agent_path_off(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "blumkin-agent"
    binary.write_text("x")
    monkeypatch.setattr(keychain_agent, "binary_path", lambda: binary)
    assert keychain_agent.applies() is True
    monkeypatch.setenv(keychain_agent.DISABLE_ENV, "0")
    assert keychain_agent.applies() is False


def test_the_gate_asks_nothing_of_a_process_that_goes_through_the_agent(
    monkeypatch, tmp_path: Path
) -> None:
    binary = tmp_path / "blumkin-agent"
    binary.write_text("x")
    monkeypatch.setattr(keychain_agent, "binary_path", lambda: binary)
    keychain_agent.mark_in_use()

    keychain_gate.require()  # a background process, nothing authorized: the agent has it
    assert keychain_gate.state() == "via_agent"
    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        keychain_gate.require(direct=True)  # the fallback that touches the keychain itself


def test_an_agent_binary_that_shows_up_later_does_not_waive_the_gate_for_direct_access(
    monkeypatch, tmp_path: Path
) -> None:
    """A process that already settled on the direct keyring keeps the gate in front of it."""
    binary = tmp_path / "blumkin-agent"
    binary.write_text("x")
    monkeypatch.setattr(keychain_agent, "binary_path", lambda: binary)
    assert keychain_agent.applies() is True
    assert keychain_agent.in_use() is False  # this process never put the agent in front

    with pytest.raises(keychain_unlock.KeychainNotAuthorizedError):
        keychain_gate.require()


def test_the_secret_store_puts_the_vault_on_the_agent_backend(monkeypatch, tmp_path: Path) -> None:
    from blumkin import keychain_vault

    binary = tmp_path / "blumkin-agent"
    binary.write_text("x")
    monkeypatch.setattr(keychain_agent, "binary_path", lambda: binary)
    monkeypatch.setattr(keychain_vault, "vault_applies", lambda: True)
    backend = type("Backend", (), {"__module__": "keyring.backends.macOS"})()
    stub = type(sys)("keyring")
    stub.get_keyring = lambda: backend
    monkeypatch.setitem(sys.modules, "keyring", stub)
    monkeypatch.setattr(secret_store, "_keyring_checked", False)
    monkeypatch.setattr(secret_store, "_keyring_mod", None)

    module = _REAL_KEYRING_MODULE()

    assert isinstance(module, keychain_vault.VaultKeyring)
    assert isinstance(module._backend, AgentKeyring)
    assert keychain_agent.in_use() is True


def test_auth_keychain_through_the_agent_records_no_python_authorization(
    monkeypatch, tmp_path: Path
) -> None:
    import json
    from types import SimpleNamespace

    from click.testing import CliRunner

    from blumkin.cli import main

    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr("blumkin.cli.list_profiles", lambda: [{"name": "work"}])
    monkeypatch.setattr(
        "blumkin.cli.load_config", lambda profile=None: SimpleNamespace(profile=profile)
    )

    def touched(_cfg) -> bool:
        keychain_agent.mark_in_use()
        return True

    monkeypatch.setattr(secret_store, "touch_keychain", touched)

    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "authorized_via_agent"
    assert not keychain_gate.is_authorized()


def test_through_the_agent_a_call_may_wait_on_a_prompt_longer_than_five_seconds(
    monkeypatch,
) -> None:
    import time

    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)

    def slow() -> str:
        time.sleep(0.2)
        return "answered"

    with pytest.raises(TimeoutError):
        secret_store._call_keyring_with_timeout(slow)  # direct keyring: the short budget

    keychain_agent.mark_in_use()
    assert secret_store._call_keyring_with_timeout(slow) == "answered"
    assert secret_store._AGENT_IO_TIMEOUT_SECONDS > agent_client._UNLOCK_TIMEOUT_SECONDS


def test_a_run_that_fell_back_once_still_remembers_it_after_the_agent_answers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    keyring = AgentKeyring(_Direct())
    _use(monkeypatch, agent_client.AgentUnavailableError("no agent"))
    keyring.get_password("blumkin", "first-profile")
    _use(monkeypatch, {"ok": True, "found": False})
    keyring.get_password("blumkin", "second-profile")

    assert keychain_agent.fell_back_ever() is True  # this run authorized the direct keychain


def test_the_call_that_falls_back_runs_the_direct_keyring_under_the_short_budget(
    monkeypatch,
) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    _use(monkeypatch, agent_client.AgentUnavailableError("no agent"))
    bounded: list[str] = []

    def bound(func, *args):
        bounded.append(func.__name__)
        return func(*args)

    keyring = AgentKeyring(_Direct(), bound=bound)
    keyring.get_password("blumkin", "acct")
    keyring.set_password("blumkin", "acct", "v")
    keyring.delete_password("blumkin", "acct")

    assert bounded == ["get_password", "set_password", "delete_password"]


def test_the_secret_store_bounds_the_direct_fallback_with_its_short_budget(monkeypatch) -> None:
    import time

    monkeypatch.setattr(secret_store, "_KEYRING_IO_TIMEOUT_SECONDS", 0.05)
    keychain_agent.mark_in_use()  # the long budget is in force for the surrounding call

    with pytest.raises(TimeoutError):
        secret_store._bound_direct_keyring_call(lambda: time.sleep(0.2))


def test_a_fallback_to_the_direct_keyring_is_remembered(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    _use(monkeypatch, agent_client.AgentUnavailableError("no agent"))
    assert keychain_agent.fell_back_ever() is False

    AgentKeyring(_Direct()).get_password("blumkin", "acct")

    assert keychain_agent.fell_back_ever() is True


def test_auth_keychain_records_the_python_build_when_the_agent_was_out_of_reach(
    monkeypatch,
) -> None:
    import json
    from types import SimpleNamespace

    from click.testing import CliRunner

    from blumkin.cli import main

    monkeypatch.setattr(keychain_gate, "interactive", lambda: True)
    monkeypatch.setattr("blumkin.cli.list_profiles", lambda: [{"name": "work"}])
    monkeypatch.setattr(
        "blumkin.cli.load_config", lambda profile=None: SimpleNamespace(profile=profile)
    )

    def touched_directly(_cfg) -> bool:
        keychain_agent.mark_in_use()
        monkeypatch.setattr(keychain_agent, "_fell_back_ever", True)  # the agent was unreachable
        return True

    monkeypatch.setattr(secret_store, "touch_keychain", touched_directly)

    result = CliRunner().invoke(main, ["auth", "keychain", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "authorized"
    assert keychain_gate.is_authorized()


def test_an_agent_timeout_is_a_timeout_error_so_no_fallback_write_follows_it() -> None:
    """The secret store skips its plaintext fallback after a TimeoutError (#287)."""
    assert issubclass(KeychainAgentTimeoutError, TimeoutError)
    assert issubclass(KeychainAgentTimeoutError, KeychainAgentError)


def test_a_write_the_agent_did_not_answer_in_time_is_not_followed_by_a_file_write(
    monkeypatch, tmp_path: Path
) -> None:
    """End to end: token_storage auto, an unanswered keychain_set, no plaintext copy."""
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        config_dir=tmp_path,
        profile="work",
        token_storage="auto",
        keychain_unlock="off",
        token_cache_path=tmp_path / "msal_token_cache.json",
        auth_record_path=tmp_path / "auth_record.json",
        google_token_path=tmp_path / "google_token.json",
    )
    keyring = AgentKeyring(_Direct())
    keychain_agent.mark_in_use()  # the secret store put the agent in front of its keychain access
    monkeypatch.setattr(secret_store, "_backend_for", lambda _cfg: "keyring")
    monkeypatch.setattr(secret_store, "_keyring_module", lambda: keyring)
    _use(monkeypatch, agent_client.AgentUnreachableError("no reply"))

    with pytest.raises(secret_store.SecretWriteTimeoutError):
        secret_store._write_text_direct(cfg, "google_token", "secret")  # type: ignore[arg-type]

    assert not (tmp_path / "google_token.json").exists()
