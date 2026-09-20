"""Client side of the blumkin-agent protocol: connect-or-spawn, call, retry.

Foundation layer (issue #328): every call here is a short-lived Unix domain
socket connection - connect (spawning the agent first if nothing answers),
send one request, read one response, close. No secret material flows over
this yet (see `blumkin.agent.protocol`); this module exists so the spawn
lifecycle and version-mismatch handling can be built and tested against the
foundation commands (`ping`/`status`/`lock`) before any real secret-serving
command is added on top of it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from typing import Any

from blumkin.agent import protocol
from blumkin.agent.paths import binary_path, is_supported_platform, socket_path


class AgentUnavailableError(Exception):
    """The agent could not be reached or spawned."""


class AgentUnreachableError(AgentUnavailableError):
    """A process answered the connect but did not reply in time.

    Distinct from the base `AgentUnavailableError` (nothing listening at
    all): this means an agent process may well still be alive - merely
    wedged, SIGSTOPped, or busy for longer than `_SPAWN_TIMEOUT_SECONDS` in
    its single-threaded accept loop - so callers that only check
    `spawn=False` reachability (`blumkin agent status`/`lock`) must not
    conflate this with "no agent running" (see PR #329 review: reporting
    `locked: true` here would tell an operator their cached state was
    dropped when the still-alive agent never actually saw the request).
    """


def call(cmd: str, *, extra: dict[str, Any] | None = None, spawn: bool = True) -> dict[str, Any]:
    """Send one request to the agent, spawning it first if `spawn` and none answers.

    A `protocol_mismatch` response (the agent is older than this client,
    e.g. still resident from before a `pipx upgrade`) is handled
    automatically: the mismatched agent shuts itself down as soon as it
    replies (see `AgentServer._dispatch`), so this just waits for its
    socket to clear, spawns a fresh one from the *current* interpreter, and
    retries the request once - an upgraded `blumkin` never needs the
    operator to manually "restart the agent" (see the "Installation &
    upgrade" section of issue #328).
    """
    if not is_supported_platform():
        raise AgentUnavailableError("blumkin-agent is not supported on this platform yet")
    request: dict[str, Any] = {"cmd": cmd, "protocol_version": protocol.PROTOCOL_VERSION}
    if extra:
        request.update(extra)

    response = _call_once(request, spawn=spawn)
    if response.get("error") == "protocol_mismatch" and spawn:
        _wait_for_socket_gone()
        response = _call_once(request, spawn=True)
    return response


def ensure_agent_running() -> None:
    """Spawn the agent if nothing currently answers on its socket."""
    call("ping")


#: How long `ensure_agent_running` waits for a freshly spawned agent to
#: start accepting connections before giving up.
_SPAWN_POLL_INTERVAL_SECONDS = 0.05
_SPAWN_TIMEOUT_SECONDS = 5.0

#: How long the client waits for an `unlock` reply specifically. Must cover
#: the agent's LocalAuthentication budget (`PRESENCE_TIMEOUT` = 120s in
#: `rust-agent/src/presence.rs`) plus a small protocol margin - a bare
#: `_SPAWN_TIMEOUT_SECONDS` (5s) recv timeout here abandons a still-showing
#: Touch ID/password prompt long before the user can respond to it. The
#: abandoned request keeps running server-side (nothing here cancels it),
#: so a caller that then retries opens a *second* connection, which the
#: agent dispatches on its own thread and therefore starts a *second*,
#: concurrent `evaluatePolicy` call - and macOS's LocalAuthentication
#: cancels whichever evaluation was already in flight the moment a new one
#: starts in the same process ("Canceled by another authentication"). The
#: result observed in issue #343: prompts stealing focus from each other
#: every ~5s, forever, with the user never getting a chance to actually
#: authenticate. Matching this to the server's own budget means the client
#: only ever needs to send one `unlock` and wait for the one real answer.
_UNLOCK_TIMEOUT_SECONDS = 125.0


def _response_timeout_seconds(request: dict[str, Any]) -> float:
    """The recv budget for `request`'s reply - `unlock` alone needs the
    long, presence-check-sized budget; every other command answers almost
    immediately and should keep the short default so a genuinely wedged
    agent is still detected quickly (see `AgentUnreachableError`'s docs).
    """
    if request.get("cmd") == "unlock":
        return _UNLOCK_TIMEOUT_SECONDS
    return _SPAWN_TIMEOUT_SECONDS


def _call_once(request: dict[str, Any], *, spawn: bool) -> dict[str, Any]:
    sock_path = str(socket_path())
    try:
        return _send(sock_path, request)
    except TimeoutError as exc:
        # Never spawn a replacement here: a live-but-unresponsive agent can
        # keep a new daemon's liveness probe busy for its own ~6s grace
        # window, but `_wait_for_socket_ready` only waits 5s - the request
        # would just fail anyway, after wastefully racing a second process
        # against the first (see PR #329 review).
        raise AgentUnreachableError(
            f"agent at {sock_path} accepted the connection but did not reply in time"
        ) from exc
    except OSError:
        if not spawn:
            raise AgentUnavailableError(f"no agent listening at {sock_path}")
    except protocol.ProtocolError:
        if not spawn:
            raise AgentUnavailableError(f"no agent listening at {sock_path}")
    try:
        _spawn()
    except OSError as exc:
        raise AgentUnavailableError(f"could not start blumkin-agent: {exc}") from exc
    _wait_for_socket_ready(sock_path)
    try:
        return _send(sock_path, request)
    except OSError as exc:
        raise AgentUnavailableError(
            f"spawned blumkin-agent but could not connect at {sock_path}: {exc}"
        ) from exc
    except protocol.ProtocolError as exc:
        raise AgentUnavailableError(
            f"spawned blumkin-agent but it closed the connection before replying: {exc}"
        ) from exc


def _can_connect(sock_path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(_SPAWN_POLL_INTERVAL_SECONDS)
        probe.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _send(sock_path: str, request: dict[str, Any]) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # The *connect* timeout stays short (a listening socket accepts
        # near-instantly; a long budget here would only slow down
        # detecting a genuinely wedged/dead agent) - only the *response*
        # wait is widened, and only for the one command that can
        # legitimately take a while to answer (see
        # `_response_timeout_seconds`'s docs).
        connection.settimeout(_SPAWN_TIMEOUT_SECONDS)
        connection.connect(sock_path)
        protocol.send_message(connection, request)
        connection.settimeout(_response_timeout_seconds(request))
        return protocol.recv_message(connection, timeout=_response_timeout_seconds(request))
    finally:
        connection.close()


def _spawn() -> None:
    """Start a detached agent process: the compiled `blumkin-agent` binary.

    `start_new_session=True` (POSIX `setsid`) so the agent outlives this
    short-lived CLI invocation - it is tied to the login session, not to
    the terminal/parent process that happened to spawn it, mirroring
    `gpg-agent`'s own auto-start behavior.
    """
    binary = binary_path()
    if not binary.is_file():
        raise AgentUnavailableError(
            f"blumkin-agent binary not found at {binary} - the install did not build it "
            "(is a Rust toolchain available? see the 'Installation & upgrade' section of "
            "issue #328)"
        )
    subprocess.Popen(
        [str(binary)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _wait_for_socket_gone() -> None:
    """Wait for a shut-down agent to release its socket before respawning.

    `AgentServer._cleanup` always unlinks its socket file on the way out
    (whether exiting due to `shutdown`, a protocol mismatch, or its own
    idle timeout - `lock` no longer exits the process, see PR #341), so
    polling for the path to disappear is sufficient.

    If the deadline is reached, the path is left alone rather than unlinked:
    a re-check-then-unlink here cannot tell a mismatched agent that is just
    slow to exit from a *different*, live agent that has already rebound
    the same path (see PR #329 review) - deleting the wrong process's
    socket would leave that agent alive but unreachable. `server.rs::bind`
    already runs its own connect-probe before binding and removes a
    genuinely stale socket there, so the subsequent `_spawn()` +
    `_wait_for_socket_ready()` below safely handles a socket that is still
    present at this point.
    """
    sock_path = str(socket_path())
    deadline = time.monotonic() + _SPAWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not os.path.exists(sock_path):
            return
        time.sleep(_SPAWN_POLL_INTERVAL_SECONDS)


def _wait_for_socket_ready(sock_path: str) -> None:
    """Wait until a freshly spawned agent actually accepts connections.

    Path existence alone is not readiness: a socket file left behind by a
    crashed/killed agent (exactly the state `server.rs::bind` documents and
    handles via its own connect-probe) satisfies a plain
    `os.path.exists` check immediately, before the freshly spawned agent has
    unlinked/rebound it - so this probes connectability instead (see PR
    #329 review).
    """
    deadline = time.monotonic() + _SPAWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _can_connect(sock_path):
            return
        time.sleep(_SPAWN_POLL_INTERVAL_SECONDS)
    raise AgentUnavailableError(f"agent did not start listening at {sock_path} in time")
