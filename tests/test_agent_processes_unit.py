"""Orphaned agent / stale `mcp serve` process discovery (issue #408)."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from blumkin.agent import client as agent_client
from blumkin.agent import processes as agent_processes
from blumkin.agent import protocol

# This module tests `agent_processes`'s real implementation directly - keep
# references to it from before `conftest.py`'s autouse
# `_stub_agent_process_discovery` fixture stubs it out to `lambda: []` for
# every other test file (so they are not at the mercy of the host machine's
# actual running processes).
_REAL_DISCOVER_AGENT_INSTANCES = agent_processes.discover_agent_instances
_REAL_MCP_SERVE_PROCESSES = agent_processes.mcp_serve_processes


@pytest.fixture(autouse=True)
def _restore_real_process_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_processes, "discover_agent_instances", _REAL_DISCOVER_AGENT_INSTANCES)
    monkeypatch.setattr(agent_processes, "mcp_serve_processes", _REAL_MCP_SERVE_PROCESSES)


@pytest.fixture(autouse=True)
def _sandboxed_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Same rationale as `test_agent_client_server_unit.py`'s fixture of the
    same name: a short, flat directory under `/tmp`, never the real one."""
    runtime_dir = tempfile.mkdtemp(dir="/tmp")
    monkeypatch.setenv("BLUMKIN_AGENT_RUNTIME_DIR", runtime_dir)
    try:
        yield
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _serve_once(sock_path: Path, handle: Callable[[socket.socket], None]) -> threading.Thread:
    """Spawn a background thread that accepts exactly one connection.

    `daemon=True` and a `listener` reference on the returned thread: a test
    that deliberately never connects (proving a candidate gets skipped, say)
    would otherwise leave `listener.accept()` blocked forever - close
    `thread.listener` in that test's teardown to unblock it, and mark the
    thread daemon regardless so a still-blocked accept can never hang the
    whole interpreter at shutdown (PR #409 review: this exact leak stalled
    unrelated tests later in the same session, and CI's own hermetic pytest
    job, once diagnosed).
    """
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

    thread = threading.Thread(target=_serve, daemon=True)
    thread.listener = listener  # type: ignore[attr-defined]
    thread.start()
    return thread


def _reply_once(response: dict) -> Callable[[socket.socket], None]:
    def _handle(conn: socket.socket) -> None:
        protocol.recv_message(conn, timeout=5)
        protocol.send_message(conn, response)

    return _handle


# ------------------------------------------------------------------ agent_client.call_at


def test_call_at_returns_the_response_from_an_explicit_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp_dir:
        sock_path = Path(tmp_dir) / "legacy.sock"
        thread = _serve_once(sock_path, _reply_once({"ok": True, "agent_pid": 4242}))
        try:
            response = agent_client.call_at(sock_path, "status")
        finally:
            thread.join(timeout=5)
    assert response == {"ok": True, "agent_pid": 4242}


def test_call_at_returns_none_when_nothing_is_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    assert agent_client.call_at(tmp_path / "nothing-here.sock", "status") is None


def test_call_at_never_spawns_and_never_retries_a_protocol_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike `call`, `call_at` must not spawn a fresh agent nor retry - it
    is only ever used to probe/stop sockets this build does not own."""
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp_dir:
        sock_path = Path(tmp_dir) / "legacy.sock"
        thread = _serve_once(sock_path, _reply_once({"ok": False, "error": "protocol_mismatch"}))
        try:
            with patch("blumkin.agent.client._spawn") as spawn:
                response = agent_client.call_at(sock_path, "shutdown")
                spawn.assert_not_called()
        finally:
            thread.join(timeout=5)
    assert response == {"ok": False, "error": "protocol_mismatch"}


def test_call_at_returns_none_when_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: False)
    assert agent_client.call_at(tmp_path / "x.sock", "status") is None


# ------------------------------------------------------------------ discover_agent_instances


def test_discover_agent_instances_finds_the_primary_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_candidate_sockets` is pinned to exactly one entry so the assertions
    below depend only on what this test wires up, not on whatever fallback
    sockets (a real, live `blumkin-agent` on this dev machine's own
    `$TMPDIR`, say) happen to also be reachable (PR #409 review)."""
    from blumkin.agent.paths import socket_path

    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    monkeypatch.setattr(agent_processes, "is_supported_platform", lambda: True)
    sock_path = socket_path()
    monkeypatch.setattr(agent_processes, "_candidate_sockets", lambda: [sock_path])
    thread = _serve_once(
        sock_path, _reply_once({"ok": True, "agent_pid": 111, "agent_version": "1.10.0"})
    )
    try:
        instances = agent_processes.discover_agent_instances()
    finally:
        thread.join(timeout=5)
    assert len(instances) == 1
    assert instances[0].is_primary is True
    assert instances[0].pid == 111
    assert instances[0].version == "1.10.0"


def test_discover_agent_instances_combines_version_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AgentInstance.version` must match `<version> (<commit>)`, the same
    shape `blumkin upgrade`'s `before`/`after` reads use - a bare package
    version never compares equal to that, which made an already-current,
    just-respawned agent indistinguishable from a stale one (PR #409
    review)."""
    from blumkin.agent.paths import socket_path

    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    monkeypatch.setattr(agent_processes, "is_supported_platform", lambda: True)
    sock_path = socket_path()
    monkeypatch.setattr(agent_processes, "_candidate_sockets", lambda: [sock_path])
    thread = _serve_once(
        sock_path,
        _reply_once(
            {
                "ok": True,
                "agent_pid": 111,
                "agent_version": "1.10.0",
                "agent_commit": "abc123456789",
            }
        ),
    )
    try:
        instances = agent_processes.discover_agent_instances()
    finally:
        thread.join(timeout=5)
    assert instances[0].version == "1.10.0 (abc123456789)"


def test_discover_agent_instances_finds_a_legacy_socket_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact split from issue #408's "Observed scenario": a legacy agent
    on a fallback base is reachable, but not at this build's primary socket.
    `_candidate_sockets` is pinned to exactly the primary (not listening)
    plus this one legacy path, so the result depends only on what this test
    wires up (PR #409 review)."""
    from blumkin.agent.paths import socket_path

    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    monkeypatch.setattr(agent_processes, "is_supported_platform", lambda: True)
    primary_sock = socket_path()  # deliberately never bound - nothing listens here
    legacy_base = tempfile.mkdtemp(dir="/tmp")
    legacy_sock = Path(legacy_base) / f"blumkin-agent-{os.getuid()}" / "agent.sock"
    legacy_sock.parent.mkdir(parents=True)
    os.chmod(legacy_sock.parent, 0o700)
    monkeypatch.setattr(agent_processes, "_candidate_sockets", lambda: [primary_sock, legacy_sock])
    thread = _serve_once(
        legacy_sock, _reply_once({"ok": True, "agent_pid": 222, "agent_version": "1.9.2"})
    )
    try:
        instances = agent_processes.discover_agent_instances()
    finally:
        thread.join(timeout=5)
        shutil.rmtree(legacy_base, ignore_errors=True)
    assert len(instances) == 1
    assert instances[0].is_primary is False
    assert instances[0].pid == 222
    assert instances[0].version == "1.9.2"


def test_discover_agent_instances_skips_a_candidate_dir_it_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A world/group-writable `blumkin-agent-<uid>` directory (a same-uid
    check alone is not enough - it must also refuse loose permissions, since
    a planted directory could still match this uid) must never be connected
    to, even if something is listening on it and would happily answer
    `status` (PR #409 review: this is the trust boundary
    `agent.paths._ensure_private_owned_dir` enforces for the primary
    directory - candidates must not bypass it)."""
    monkeypatch.setattr(agent_client, "is_supported_platform", lambda: True)
    monkeypatch.setattr(agent_processes, "is_supported_platform", lambda: True)
    planted_base = tempfile.mkdtemp(dir="/tmp")
    planted_sock = Path(planted_base) / f"blumkin-agent-{os.getuid()}" / "agent.sock"
    planted_sock.parent.mkdir(parents=True)
    os.chmod(planted_sock.parent, 0o777)  # world-writable: refuse this, even same-uid
    monkeypatch.setattr(agent_processes, "_candidate_sockets", lambda: [planted_sock])
    thread = _serve_once(
        planted_sock, _reply_once({"ok": True, "agent_pid": 666, "agent_version": "9.9.9"})
    )
    try:
        instances = agent_processes.discover_agent_instances()
    finally:
        # This test's whole point is that nothing ever connects, so
        # `listener.accept()` would otherwise block forever - close it to
        # unblock the thread before joining (PR #409 review follow-up).
        thread.listener.close()  # type: ignore[attr-defined]
        thread.join(timeout=5)
        shutil.rmtree(planted_base, ignore_errors=True)
    assert instances == []


def test_discover_agent_instances_is_empty_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_processes, "is_supported_platform", lambda: False)
    assert agent_processes.discover_agent_instances() == []


# ------------------------------------------------------------------ _candidate_sockets


def test_candidate_sockets_includes_the_tmpdir_fallback_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of issue #408 - finding a pre-#403, `$TMPDIR`-based
    legacy agent this build's own `runtime_base_dir()` no longer resolves
    to - depends on this exact ladder; a prior test only ever patched
    `_candidate_sockets` out, so a regression here (e.g. dropping `TMPDIR`)
    could not have failed it (PR #409 review)."""
    monkeypatch.setattr(agent_processes, "runtime_base_dir", lambda: Path("/sandboxed-base"))
    monkeypatch.setenv("TMPDIR", "/tmpdir-base")
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    candidates = agent_processes._candidate_sockets()
    uid = os.getuid()
    assert candidates[0] == Path("/sandboxed-base") / f"blumkin-agent-{uid}" / "agent.sock"
    assert Path("/tmpdir-base") / f"blumkin-agent-{uid}" / "agent.sock" in candidates


def test_orphaned_agent_instances_excludes_the_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    from blumkin.agent.paths import socket_path

    primary = socket_path()
    monkeypatch.setattr(
        agent_processes,
        "discover_agent_instances",
        lambda: [
            agent_processes.AgentInstance(
                socket_path=primary, pid=1, version="1.10.0", is_primary=True
            ),
            agent_processes.AgentInstance(
                socket_path=Path("/tmp/other/agent.sock"),
                pid=2,
                version="1.9.2",
                is_primary=False,
            ),
        ],
    )
    orphaned = agent_processes.orphaned_agent_instances()
    assert len(orphaned) == 1
    assert orphaned[0].pid == 2


# ------------------------------------------------------------------ mcp_serve_processes


def test_mcp_serve_processes_parses_ps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    ps_output = (
        "  123 Wed Oct 25 12:34:56 2024     /usr/bin/python3 -m blumkin mcp serve\n"
        "  456 Wed Oct 25 12:35:00 2024     /usr/bin/ps -axo pid=,lstart=,command=\n"
        "  789 Wed Oct 25 12:36:00 2024     /Users/x/.local/bin/blumkin mcp serve --profile work\n"
    )

    def _fake_run(*_args, **_kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr="")

    monkeypatch.setattr(agent_processes.sys, "platform", "darwin")
    monkeypatch.setattr(agent_processes.os, "getpid", lambda: 999)
    monkeypatch.setattr(agent_processes.subprocess, "run", _fake_run)
    processes = agent_processes.mcp_serve_processes()
    pids = {proc.pid for proc in processes}
    assert pids == {123, 789}
    match = next(proc for proc in processes if proc.pid == 123)
    assert match.started_at == datetime(2024, 10, 25, 12, 34, 56)


def test_mcp_serve_processes_rejects_a_substring_only_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command line that merely *contains* the words "blumkin", "mcp", and
    "serve" - without actually being a `blumkin ... mcp serve` invocation -
    must never be reported (PR #409 review: a prior substring-only check
    would have matched this)."""
    ps_output = "  321 Wed Oct 25 12:34:56 2024     /bin/zsh -c grep blumkin mcp serve\n"

    def _fake_run(*_args, **_kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr="")

    monkeypatch.setattr(agent_processes.sys, "platform", "darwin")
    monkeypatch.setattr(agent_processes.os, "getpid", lambda: 999)
    monkeypatch.setattr(agent_processes.subprocess, "run", _fake_run)
    assert agent_processes.mcp_serve_processes() == []


def test_mcp_serve_processes_accepts_interpreter_options_before_dash_m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python permits interpreter options (e.g. `-u`) before `-m`, so
    `python3 -u -m blumkin mcp serve` is still a real serve process, not a
    rejected false match (PR #409 review)."""
    ps_output = "  321 Wed Oct 25 12:34:56 2024     /usr/bin/python3 -u -m blumkin mcp serve\n"

    def _fake_run(*_args, **_kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr="")

    monkeypatch.setattr(agent_processes.sys, "platform", "darwin")
    monkeypatch.setattr(agent_processes.os, "getpid", lambda: 999)
    monkeypatch.setattr(agent_processes.subprocess, "run", _fake_run)
    assert {proc.pid for proc in agent_processes.mcp_serve_processes()} == {321}


def test_mcp_serve_processes_accepts_an_operand_taking_interpreter_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-X`/`-W` take their value as a *separate* following token (`-X dev`),
    unlike `-u` - the operand itself doesn't start with `-`, so it must be
    skipped as a pair or it is mistaken for the end of the options run
    (PR #409 review)."""
    ps_output = "  321 Wed Oct 25 12:34:56 2024     /usr/bin/python3 -X dev -m blumkin mcp serve\n"

    def _fake_run(*_args, **_kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr="")

    monkeypatch.setattr(agent_processes.sys, "platform", "darwin")
    monkeypatch.setattr(agent_processes.os, "getpid", lambda: 999)
    monkeypatch.setattr(agent_processes.subprocess, "run", _fake_run)
    assert {proc.pid for proc in agent_processes.mcp_serve_processes()} == {321}


def test_mcp_serve_processes_excludes_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    ps_output = "  999 Wed Oct 25 12:34:56 2024     /usr/bin/blumkin mcp serve\n"

    def _fake_run(*_args, **_kwargs):
        import subprocess

        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=ps_output, stderr="")

    monkeypatch.setattr(agent_processes.sys, "platform", "darwin")
    monkeypatch.setattr(agent_processes.os, "getpid", lambda: 999)
    monkeypatch.setattr(agent_processes.subprocess, "run", _fake_run)
    assert agent_processes.mcp_serve_processes() == []


def test_mcp_serve_processes_is_empty_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_processes.sys, "platform", "linux")
    assert agent_processes.mcp_serve_processes() == []


def test_mcp_serve_processes_is_empty_when_ps_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args, **_kwargs):
        raise OSError("no ps")

    monkeypatch.setattr(agent_processes.sys, "platform", "darwin")
    monkeypatch.setattr(agent_processes.subprocess, "run", _fake_run)
    assert agent_processes.mcp_serve_processes() == []


def test_as_dict_shapes_for_json_output() -> None:
    instance = agent_processes.AgentInstance(
        socket_path=Path("/tmp/x/agent.sock"), pid=1, version="1.10.0", is_primary=True
    )
    assert instance.as_dict() == {
        "socket_path": "/tmp/x/agent.sock",
        "pid": 1,
        "version": "1.10.0",
        "is_primary": True,
    }
    proc = agent_processes.McpServeProcess(
        pid=2, command="blumkin mcp serve", started_at=datetime(2024, 1, 1, 0, 0, 0)
    )
    assert proc.as_dict() == {
        "pid": 2,
        "command": "blumkin mcp serve",
        "started_at": "2024-01-01T00:00:00",
    }
