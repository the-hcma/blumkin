"""Wire protocol for talking to the blumkin-agent background process.

Foundation layer (issue #328): version handshake, message framing, and a
handful of lifecycle commands (``ping``/``status``/``lock``/``shutdown``)
only. No secret material is defined on this protocol yet - that lands in a
follow-up layer once the macOS presence check (`LocalAuthentication`) and
the actual secret cache exist. Building the wire format first, independent
of any real secret, keeps it testable without touching the keychain at all.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any

#: Bumped whenever the wire format changes in a way an older/newer agent and
#: client cannot safely interoperate with. The client always sends this
#: alongside its own package version; a mismatch tells the client its
#: request was refused because the *agent* is stale (e.g. still resident in
#: memory from before a `pipx upgrade` replaced the venv's code) so the
#: client can ask it to shut down and spawn a fresh one - see the
#: "Installation & upgrade" section of issue #328.
PROTOCOL_VERSION = 1

_ENCODING = "utf-8"
#: Generous but bounded: every message here is a small control/status
#: object today, and even once secret payloads flow over this protocol in a
#: later layer, MSAL token caches / Google credential JSON are a few KB at
#: most. Bounding rules out a runaway/misbehaving peer exhausting memory.
_MAX_MESSAGE_BYTES = 1_000_000


class ProtocolError(Exception):
    """Malformed, oversized, or truncated message on the wire."""


def recv_message(sock: socket.socket, *, timeout: float | None = None) -> dict[str, Any]:
    """Read one newline-delimited JSON message.

    Reads exactly one message per call; a caller wanting request/response
    semantics over a short-lived connection (the only pattern this
    foundation layer supports - see :mod:`blumkin.agent.client`) calls this
    once per connection.

    `timeout` bounds the *whole* message, not each individual `recv()`:
    `socket.settimeout` only limits a single call, and every call that
    returns bytes resets that clock, so a peer trickling one byte at a time
    without ever sending the terminating newline could otherwise wedge this
    loop indefinitely regardless of `timeout` (see PR #329 review, and the
    equivalent fix already made to the Rust agent's mirror in
    `rust-agent/src/protocol.rs`).
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    buf = bytearray()
    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(f"no complete message received within {timeout}s")
            sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            if not buf:
                raise ProtocolError("connection closed before any data was received")
            raise ProtocolError("connection closed mid-message")
        buf.extend(chunk)
        if len(buf) > _MAX_MESSAGE_BYTES:
            raise ProtocolError(f"message too large ({len(buf)} bytes)")
        newline_index = buf.find(b"\n")
        if newline_index == -1:
            continue
        line = bytes(buf[:newline_index])
        try:
            parsed = json.loads(line.decode(_ENCODING))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"malformed message: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProtocolError("message must be a JSON object")
        return parsed


def send_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    """Send one newline-delimited JSON message.

    Newline-delimited rather than length-prefixed: every message is a
    single JSON object, and `json.dumps` always escapes a raw ``\\n``
    inside a string value as the two characters ``\\`` ``n`` - so the
    literal newline byte this function appends can never appear earlier in
    the encoded payload, making it an unambiguous message terminator.
    """
    line = json.dumps(payload, sort_keys=True) + "\n"
    data = line.encode(_ENCODING)
    if len(data) > _MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large ({len(data)} bytes)")
    sock.sendall(data)
