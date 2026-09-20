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
import sys
from pathlib import Path
from typing import Any

import pytest

from blumkin import secret_store
from blumkin.config import load_config

# Captured at import time - `tests/conftest.py::_disable_agent_secret_cache` is
# autouse and monkeypatches this away for every test (including the ones in
# this file), so a test that wants to exercise the *real* function must
# restore it explicitly via `monkeypatch.setattr` rather than call
# `secret_store._agent_enabled` directly (that would just call the stub).
_REAL_AGENT_ENABLED = secret_store._agent_enabled


class _FakeAgent:
    """Minimal in-memory stand-in for ``blumkin.agent.client.call``, keyed by profile."""

    def __init__(self) -> None:
        self.cached: dict[str, str] = {}
        self.unlock_calls: list[dict[str, Any]] = []
        self.lock_calls: list[dict[str, Any]] = []
        self.get_secret_calls = 0

    def call(
        self, cmd: str, *, extra: dict[str, Any] | None = None, spawn: bool = True
    ) -> dict[str, Any]:
        extra = extra or {}
        profile = str(extra.get("profile"))
        if cmd == "get_secret":
            self.get_secret_calls += 1
            if profile not in self.cached:
                return {"ok": False, "error": "not_cached"}
            return {"ok": True, "secret": self.cached[profile]}
        if cmd == "unlock":
            self.unlock_calls.append(extra)
            self.cached[profile] = extra["secret"]
            return {"ok": True}
        if cmd == "lock":
            self.lock_calls.append(extra)
            self.cached.pop(profile, None)
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
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('[profiles.default]\nclient_id = "test-client"\n')
    return load_config()


def test_agent_enabled_is_false_when_token_reverify_after_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the real `_agent_enabled`, restored past the autouse stub."""
    monkeypatch.setattr(secret_store, "_agent_enabled", _REAL_AGENT_ENABLED)
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\ntoken_reverify_after = 0\n'
    )
    cfg = load_config()

    assert secret_store._agent_enabled(cfg) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="_agent_enabled is macOS-only")
def test_agent_enabled_is_true_on_macos_with_the_default_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_store, "_agent_enabled", _REAL_AGENT_ENABLED)
    cfg = _load(tmp_path, monkeypatch)

    assert secret_store._agent_enabled(cfg) is True


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
    assert fake_agent.unlock_calls[0]["profile"] == secret_store._agent_profile_key(cfg)


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


def test_agent_profile_key_differs_across_config_dirs_for_the_same_profile_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two config dirs sharing a profile name must not collide on one cache entry."""
    cfg_a = _load(tmp_path / "config-a", monkeypatch)
    cfg_b = _load(tmp_path / "config-b", monkeypatch)

    assert cfg_a.profile == cfg_b.profile == "default"
    assert secret_store._agent_profile_key(cfg_a) != secret_store._agent_profile_key(cfg_b)
    # Stable for the same config dir/profile across separate calls.
    assert secret_store._agent_profile_key(cfg_a) == secret_store._agent_profile_key(cfg_a)


def test_write_text_sends_the_configured_ttl_in_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "test-client"\ntoken_reverify_after = "30m"\n'
    )
    cfg = load_config()

    secret_store.write_text(cfg, "google_token", "secret-value")

    assert fake_agent.unlock_calls[0]["ttl_seconds"] == 30 * 60


def test_delete_invalidates_the_agent_cache_for_that_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    """Regression test: a logout must not leave a stale credential agent-cached (PR #346 review)."""
    cfg = _load(tmp_path, monkeypatch)
    secret_store.write_text(cfg, "google_token", "secret-value")
    assert secret_store._agent_profile_key(cfg) in fake_agent.cached

    secret_store.delete(cfg, "google_token")

    assert secret_store._agent_profile_key(cfg) not in fake_agent.cached
    value, _ = secret_store.read_text_and_backend(cfg, "google_token")
    assert value is None, "a deleted secret must not still be served from the agent cache"


def test_read_ms_bundle_and_backend_unlocks_and_then_reuses_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    cfg = _load(tmp_path, monkeypatch)
    secret_store._write_text_direct(cfg, "auth_record", "auth-record-value")
    secret_store._write_text_direct(cfg, "token_cache", "token-cache-value")

    first = secret_store.read_ms_bundle_and_backend(cfg)
    second = secret_store.read_ms_bundle_and_backend(cfg)

    expected = {"auth_record": "auth-record-value", "token_cache": "token-cache-value"}
    assert first == second == (expected, "file")
    assert len(fake_agent.unlock_calls) == 1, "the second read must be served from the cache"


def test_read_text_and_backend_treats_a_partial_ms_bundle_as_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: _FakeAgent
) -> None:
    """A partial cached bundle must not shadow the direct backend (PR #346 review)."""
    cfg = _load(tmp_path, monkeypatch)
    # Prime a bundle with only one of the two bundled kinds present, as could
    # happen between `auth.create_credential`'s two sequential writes.
    profile_key = secret_store._agent_profile_key(cfg)
    fake_agent.cached[profile_key] = json.dumps(
        {"backend": "file", "slot": "ms_credentials", "values": {"auth_record": "stale-only"}}
    )
    secret_store._write_text_direct(cfg, "auth_record", "auth-record-value")
    secret_store._write_text_direct(cfg, "token_cache", "token-cache-value")

    value, backend = secret_store.read_text_and_backend(cfg, "token_cache")

    assert (value, backend) == ("token-cache-value", "file")
