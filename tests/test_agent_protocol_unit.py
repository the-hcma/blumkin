"""Wire framing edge cases for `blumkin.agent.protocol` (issue #328)."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from blumkin.agent import protocol


def _connected_pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


def test_send_then_recv_roundtrips_a_message() -> None:
    left, right = _connected_pair()
    try:
        protocol.send_message(left, {"cmd": "ping", "protocol_version": 1})
        assert protocol.recv_message(right) == {"cmd": "ping", "protocol_version": 1}
    finally:
        left.close()
        right.close()


def test_recv_raises_on_connection_closed_before_any_data() -> None:
    left, right = _connected_pair()
    try:
        left.close()
        with pytest.raises(protocol.ProtocolError, match="closed before"):
            protocol.recv_message(right)
    finally:
        right.close()


def test_recv_raises_on_connection_closed_mid_message() -> None:
    left, right = _connected_pair()
    try:
        left.sendall(b'{"cmd": "pi')
        left.close()
        with pytest.raises(protocol.ProtocolError, match="mid-message"):
            protocol.recv_message(right)
    finally:
        right.close()


def test_recv_raises_on_malformed_json() -> None:
    left, right = _connected_pair()
    try:
        left.sendall(b"not json\n")
        with pytest.raises(protocol.ProtocolError, match="malformed message"):
            protocol.recv_message(right)
    finally:
        left.close()
        right.close()


def test_recv_raises_when_message_is_not_a_json_object() -> None:
    left, right = _connected_pair()
    try:
        left.sendall(b"[1, 2, 3]\n")
        with pytest.raises(protocol.ProtocolError, match="must be a JSON object"):
            protocol.recv_message(right)
    finally:
        left.close()
        right.close()


def test_recv_raises_on_oversized_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "_MAX_MESSAGE_BYTES", 8)
    left, right = _connected_pair()
    try:
        left.sendall(b'{"cmd": "ping-but-way-too-long"}\n')
        with pytest.raises(protocol.ProtocolError, match="too large"):
            protocol.recv_message(right)
    finally:
        left.close()
        right.close()


def test_send_raises_on_oversized_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "_MAX_MESSAGE_BYTES", 8)
    left, right = _connected_pair()
    try:
        with pytest.raises(protocol.ProtocolError, match="too large"):
            protocol.send_message(left, {"cmd": "ping-but-way-too-long"})
    finally:
        left.close()
        right.close()


def test_recv_honors_timeout() -> None:
    left, right = _connected_pair()
    try:
        with pytest.raises(TimeoutError):
            protocol.recv_message(right, timeout=0.05)
    finally:
        left.close()
        right.close()


def test_recv_timeout_bounds_the_whole_message_not_each_read() -> None:
    """A peer trickling data without ever completing the message must not

    wedge `recv_message` past its overall `timeout`, even though every
    individual `recv()` that returns bytes would otherwise reset a
    per-call `SO_RCVTIMEO` clock (see PR #329 review).

    The trickle keeps going (via `release`) past the 0.2s deadline for as
    long as the test needs: a fixed, short trickle would let the pre-fix
    per-read-timeout implementation also raise (just later, once the
    trickle itself stops) and stay under a loose elapsed-time bound,
    without actually pinning the whole-message-deadline behavior (see PR
    #329 review).
    """
    left, right = _connected_pair()
    release = threading.Event()

    def _trickle() -> None:
        try:
            left.sendall(b"{")
        except OSError:
            return
        while not release.is_set():
            release.wait(0.03)
            try:
                left.sendall(b"x")
            except OSError:
                return

    thread = threading.Thread(target=_trickle)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises((protocol.ProtocolError, TimeoutError)):
            protocol.recv_message(right, timeout=0.2)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
    finally:
        release.set()
        thread.join(timeout=5)
        left.close()
        right.close()
