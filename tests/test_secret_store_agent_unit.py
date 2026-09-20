"""blumkin-agent caching wired into blumkin.secret_store reads/writes (issue #339).

``tests/conftest.py::_disable_agent_secret_cache`` forces ``_agent_enabled``
False for every other test, so nothing here or elsewhere spawns the real
compiled ``blumkin-agent`` binary (that gap once caused a real Touch ID/device
password prompt storm during ordinary hermetic test runs). These tests
re-enable the agent path deliberately and install a small fake in-memory
``agent_client.call`` instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from blumkin import secret_store
from blumkin.config import load_config


class _FakeAgent:
    """Minimal in-memory stand-in for ``blumkin.agent.client.call``."""

    def __init__(self) -> None:
        self.cached: dict[str, str] | None = None
        self.unlock_calls: list[dict[str, Any]] = []
        self.get_secret_calls = 0

    def call(
        self, cmd: str, *, extra: dict[str, Any] | None = None, spawn: bool = True
    ) -> dict[str, Any]:
        extra = extra or {}
        if cmd == "get_secret":
            self.get_secret_calls += 1
            if self.cached is None:
                return {"ok": False, "error": "not_cached"}
            return {"ok": True, "secret": self.cached["work"]}
        if cmd == "unlock":
            self.unlock_calls.append(extra)
            self.cached = {"work": extra["secret"]}
            return {"ok": True}
        raise AssertionError(f"unexpected command {cmd!r}")


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> _FakeAgent:
    """Enable agent-mode and install a fake in-memory agent client."""
    fake = _FakeAgent()
    monkeypatch.setattr(secret_store, "_agent_enabled", lambda cfg: True)
    monkeypatch.setattr(secret_store.agent_client, "call", fake.call)
    return fake


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "test-client"\n')
    return load_config()


def test_agent_enabled_is_false_when_token_reverify_after_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the real `_agent_enabled` (the default fixture patches it away elsewhere)."""
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\ntoken_reverify_after = 0\n'
    )
    cfg = load_config()

    assert secret_store._agent_enabled(cfg) is False


def test_read_text_and_backend_unlocks_the_agent_on_a_cache_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    # Bypass agent priming on write so this test controls exactly one miss.
    monkeypatch.setattr(secret_store, "_agent_cache_missing", lambda cfg, kind: False)
    secret_store._write_text_direct(cfg, "google_token", "secret-value")

    value, backend = secret_store.read_text_and_backend(cfg, "google_token")

    assert value == "secret-value"
    assert backend == "file"
    assert len(fake_agent.unlock_calls) == 1
    assert fake_agent.unlock_calls[0]["profile"] == cfg.profile


def test_read_text_and_backend_reuses_a_cached_value_without_a_second_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_agent_cache_missing", lambda cfg, kind: False)
    secret_store._write_text_direct(cfg, "google_token", "secret-value")

    first = secret_store.read_text_and_backend(cfg, "google_token")
    second = secret_store.read_text_and_backend(cfg, "google_token")

    assert first == second == ("secret-value", "file")
    assert len(fake_agent.unlock_calls) == 1, "the second read must be served from the cache"


def test_read_text_and_backend_falls_back_to_the_direct_backend_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_store, "_agent_enabled", lambda cfg: False)
    cfg = _load(tmp_path, monkeypatch)
    secret_store._write_text_direct(cfg, "google_token", "secret-value")

    value, backend = secret_store.read_text_and_backend(cfg, "google_token")

    assert (value, backend) == ("secret-value", "file")


def test_write_text_primes_a_missing_agent_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    cfg = _load(tmp_path, monkeypatch)

    secret_store.write_text(cfg, "google_token", "secret-value")

    assert len(fake_agent.unlock_calls) == 1
    payload = json.loads(fake_agent.unlock_calls[0]["secret"])
    assert payload["values"]["google_token"] == "secret-value"


def test_write_text_does_not_reprime_an_already_live_agent_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    cfg = _load(tmp_path, monkeypatch)

    secret_store.write_text(cfg, "google_token", "first-value")
    secret_store.write_text(cfg, "google_token", "second-value")

    assert len(fake_agent.unlock_calls) == 1, (
        "an already-cached profile must not force a second Touch ID prompt "
        "just to persist a routine token refresh"
    )


def test_a_disabled_agent_never_calls_agent_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the hermetic default (agent disabled) must never touch the real agent."""

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("agent_client.call must not be invoked when the agent is disabled")

    monkeypatch.setattr(secret_store.agent_client, "call", _fail)
    cfg = _load(tmp_path, monkeypatch)

    secret_store.write_text(cfg, "google_token", "secret-value")
    value, backend = secret_store.read_text_and_backend(cfg, "google_token")

    assert (value, backend) == ("secret-value", "file")
