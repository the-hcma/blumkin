"""Agent spawn/connect/lock lifecycle and version-mismatch recovery (issue #328)."""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from blumkin.agent import client as agent_client
from blumkin.agent import paths as agent_paths
from blumkin.agent import protocol

_REAL_AGENT_UNAVAILABLE_REASON = (
    "blumkin-agent binary not built for this platform (macOS only today - see "
    "hatch_build.py / issue #328's 'Platform support' section); CI on other "
    "platforms exercises the client's retry/error-handling logic only"
)


def _real_agent_binary_available() -> bool:
    return agent_paths.is_supported_platform() and agent_paths.binary_path().is_file()


@pytest.fixture(autouse=True)
def _sandboxed_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test gets its own socket directory - never the real `$TMPDIR` one.

    Deliberately *not* pytest's `tmp_path`: its nested per-test path
    (``.../pytest-of-<user>/pytest-N/test_name.../``) easily exceeds the
    ~104-byte `AF_UNIX` `sun_path` limit once ``agent.sock`` is appended, so
    a short, flat directory straight under `/tmp` is used instead.
    """
    runtime_dir = tempfile.mkdtemp(dir="/tmp")
    monkeypatch.setenv("BLUMKIN_AGENT_RUNTIME_DIR", runtime_dir)
    try:
        yield
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _serve_once(sock_path: Path, handle: Callable[[socket.socket], None]) -> threading.Thread:
    """Accept exactly one connection on `sock_path` and hand it to `handle`."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)

    def _serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            handle(conn)
        finally:
            conn.close()
            listener.close()

    thread = threading.Thread(target=_serve)
    thread.start()
    return thread


def test_a_non_unlock_command_still_times_out_at_the_short_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `unlock` gets the long budget - every other command still fails
    fast against a genuinely wedged agent (see `AgentUnreachableError`'s
    docs on why a long default everywhere would be its own regression).
    """
    monkeypatch.setattr(agent_client, "_SPAWN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(agent_client, "_UNLOCK_TIMEOUT_SECONDS", 5.0)
    sock_path = agent_paths.socket_path()
    release = threading.Event()

    def _wait_then_never_reply(_conn: socket.socket) -> None:
        release.wait(5)

    thread = _serve_once(sock_path, _wait_then_never_reply)
    try:
        with pytest.raises(agent_client.AgentUnreachableError, match="did not reply in time"):
            agent_client._call_once({"cmd": "ping"}, spawn=False)
    finally:
        release.set()
        thread.join(timeout=5)


def test_agent_unreachable_error_is_an_agent_unavailable_error() -> None:
    """Existing `except AgentUnavailableError` call sites still catch it."""
    assert issubclass(agent_client.AgentUnreachableError, agent_client.AgentUnavailableError)


def test_call_does_not_retry_after_mismatch_when_spawn_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`spawn=False` (e.g. `status`/`lock`) must never trigger a respawn."""
    mismatch_response = {"ok": False, "error": "protocol_mismatch"}
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    with patch("blumkin.agent.client._call_once", return_value=mismatch_response) as call_once:
        result = agent_client.call("ping", spawn=False)

    assert result == mismatch_response
    call_once.assert_called_once()


def test_call_once_maps_a_protocol_error_to_agent_unavailable_when_spawn_is_false() -> None:
    """A peer that closes without sending anything raises `protocol.ProtocolError`

    out of `_send` - `_call_once(spawn=False)` must map that to
    `AgentUnavailableError`, not let it escape uncaught (see PR #329 review:
    `blumkin agent status`/`lock` only catch `AgentUnavailableError`).
    """
    sock_path = agent_paths.socket_path()
    thread = _serve_once(sock_path, lambda conn: None)
    try:
        with pytest.raises(agent_client.AgentUnavailableError, match="no agent listening"):
            agent_client._call_once({"cmd": "ping"}, spawn=False)
    finally:
        thread.join(timeout=5)


def test_call_once_maps_a_timeout_to_agent_unreachable_when_spawn_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer that accepts but never replies is "unreachable", not "no agent".

    Distinguishing this from a plain `AgentUnavailableError` matters because
    the peer may well still be alive and holding state a caller like
    `blumkin agent lock` must not silently claim was dropped (see PR #329
    review).
    """
    monkeypatch.setattr(agent_client, "_SPAWN_TIMEOUT_SECONDS", 0.2)
    sock_path = agent_paths.socket_path()
    release = threading.Event()

    def _wait_then_never_reply(_conn: socket.socket) -> None:
        release.wait(5)

    thread = _serve_once(sock_path, _wait_then_never_reply)
    try:
        with pytest.raises(agent_client.AgentUnreachableError, match="did not reply in time"):
            agent_client._call_once({"cmd": "ping"}, spawn=False)
    finally:
        release.set()
        thread.join(timeout=5)


def test_call_retries_once_after_a_protocol_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale agent's `protocol_mismatch` reply triggers exactly one respawn+retry.

    Patches `is_supported_platform` to True so this retry-logic test (unlike
    the real-spawn tests above) exercises `call()` on every CI platform, not
    just the macOS one the real binary is bundled for.
    """
    mismatch_response = {"ok": False, "error": "protocol_mismatch"}
    success_response = {"ok": True, "agent_pid": 12345}
    responses = iter([mismatch_response, success_response])
    calls: list[dict] = []

    def _fake_call_once(request: dict, *, spawn: bool) -> dict:
        calls.append(request)
        return next(responses)

    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    with (
        patch("blumkin.agent.client._call_once", side_effect=_fake_call_once),
        patch("blumkin.agent.client._wait_for_socket_gone") as wait_gone,
    ):
        result = agent_client.call("ping")

    assert result == success_response
    assert len(calls) == 2
    wait_gone.assert_called_once()


def test_call_without_spawn_raises_when_no_agent_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must reach `_call_once`'s real no-agent branch, not just the platform gate.

    Without this monkeypatch, `is_supported_platform()` is False on
    non-macOS CI and the test passes for the wrong reason - a regression in
    the connect-failure handling would go unnoticed (see PR #329 review).
    """
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    with pytest.raises(agent_client.AgentUnavailableError, match="no agent listening"):
        agent_client.call("ping", spawn=False)


@pytest.mark.skipif(not _real_agent_binary_available(), reason=_REAL_AGENT_UNAVAILABLE_REASON)
def test_ensure_agent_running_spawns_a_real_agent_process() -> None:
    agent_client.ensure_agent_running()
    response = agent_client.call("ping", spawn=False)
    assert response["ok"] is True
    assert response["protocol_version"] == 1
    agent_client.call("shutdown")


@pytest.mark.skipif(not _real_agent_binary_available(), reason=_REAL_AGENT_UNAVAILABLE_REASON)
def test_lock_wipes_cached_state_without_shutting_down_the_agent() -> None:
    # `lock` used to be the only way to make a running agent exit (issue
    # #328's foundation layer had nothing else to wipe); now that a real
    # secret cache exists (issue #339/PR #341), `lock` only wipes it - the
    # agent keeps running and answering `ping` afterward.
    agent_client.ensure_agent_running()

    lock_response = agent_client.call("lock", spawn=False)
    assert lock_response["ok"] is True

    ping_response = agent_client.call("ping", spawn=False)
    assert ping_response["ok"] is True

    status_response = agent_client.call("status", spawn=False)
    assert status_response["cached_profiles"] == []

    agent_client.call("shutdown")


def test_lock_without_a_running_agent_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    with pytest.raises(agent_client.AgentUnavailableError, match="no agent listening"):
        agent_client.call("lock", spawn=False)


def test_spawn_raises_a_clear_error_when_the_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the install-without-Rust-toolchain contract (`hatch_build.py`'s
    graceful-degradation docstring): a missing binary must surface as
    `AgentUnavailableError`, not a raw `FileNotFoundError` from `Popen`.
    """
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    monkeypatch.setattr(agent_client, "binary_path", lambda: Path("/nonexistent/blumkin-agent"))
    with pytest.raises(agent_client.AgentUnavailableError, match="binary not found"):
        agent_client.call("ping", spawn=True)


@pytest.mark.skipif(not _real_agent_binary_available(), reason=_REAL_AGENT_UNAVAILABLE_REASON)
def test_status_reports_no_cached_profiles_for_a_freshly_spawned_agent() -> None:
    agent_client.ensure_agent_running()
    response = agent_client.call("status", spawn=False)
    assert response["ok"] is True
    assert response["cached_profiles"] == []
    agent_client.call("shutdown")


def test_unlock_still_waits_past_the_short_default_timeout_for_its_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`unlock` must not be abandoned on the short, non-presence-check budget.

    Regression test for issue #343: a fixed, short recv timeout for every
    command (including `unlock`) abandons a still-showing Touch ID/password
    prompt long before the user can respond, and the retry that follows
    starts a second, concurrent presence check that cancels the first -
    prompts stealing focus from each other forever. Here the peer replies
    only *after* `_SPAWN_TIMEOUT_SECONDS` (the short default) has already
    elapsed but well before `_UNLOCK_TIMEOUT_SECONDS` (the long, `unlock`-
    only budget) - the call must still succeed, not time out.
    """
    monkeypatch.setattr(agent_client, "_SPAWN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(agent_client, "_UNLOCK_TIMEOUT_SECONDS", 5.0)
    sock_path = agent_paths.socket_path()

    def _reply_after_the_short_timeout_elapses(conn: socket.socket) -> None:
        time.sleep(0.4)
        protocol.recv_message(conn)
        protocol.send_message(conn, {"ok": True})

    thread = _serve_once(sock_path, _reply_after_the_short_timeout_elapses)
    try:
        response = agent_client._call_once({"cmd": "unlock", "profile": "work"}, spawn=False)
        assert response == {"ok": True}
    finally:
        thread.join(timeout=5)


def test_wait_for_socket_gone_does_not_delete_the_path_after_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path still present after the deadline is left alone - not unlinked.

    Unlinking here would delete whatever a *different*, live agent had
    already rebound at this same path in the interim, leaving that agent
    running but unreachable (see PR #329 review).
    """
    monkeypatch.setattr(agent_client, "_SPAWN_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(agent_client, "_SPAWN_POLL_INTERVAL_SECONDS", 0.02)
    sock_path = agent_paths.socket_path()
    sock_path.write_text("")

    agent_client._wait_for_socket_gone()

    assert sock_path.exists()


def test_wait_for_socket_gone_returns_as_soon_as_the_path_disappears() -> None:
    sock_path = agent_paths.socket_path()
    sock_path.write_text("")

    def _unlink_shortly_after(_: object = None) -> None:
        time.sleep(0.05)
        sock_path.unlink()

    thread = threading.Thread(target=_unlink_shortly_after)
    thread.start()
    try:
        agent_client._wait_for_socket_gone()
    finally:
        thread.join()

    assert not sock_path.exists()


def test_wait_for_socket_ready_rejects_a_stale_socket_with_no_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path existence is not readiness.

    A plain file at the socket path (standing in for one left behind by a
    crashed agent - `connect()` against it fails immediately with
    `ENOTSOCK`/`ECONNREFUSED`, deterministically, unlike a bound-but-never-
    `listen()`-ed socket which a connect can succeed against via its
    backlog) satisfies a plain `os.path.exists` check immediately; this is
    the assertion that fails under such a check and only passes once the
    probe actually attempts to connect (see PR #329 review).
    """
    monkeypatch.setattr(agent_client, "_SPAWN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(agent_client, "_SPAWN_POLL_INTERVAL_SECONDS", 0.05)
    sock_path = str(agent_paths.socket_path())
    Path(sock_path).write_text("")

    with pytest.raises(agent_client.AgentUnavailableError, match="did not start listening"):
        agent_client._wait_for_socket_ready(sock_path)
