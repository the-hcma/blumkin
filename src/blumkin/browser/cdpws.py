"""Frames a fresh Playwright connection cannot see, read over a loopback DevTools websocket.

A cross-origin iframe is its own DevTools target. A connection made after the frame already
exists never learns of it through Playwright (issue #551), but Chrome still lists it at
`/json/list` with a debugger socket of its own. This module speaks just enough of that socket
to evaluate a script in the frame: a stdlib websocket client for loopback only, a fixed list of
allowed calls, a size cap and a timeout on every read, and the filter that keeps only frames
that belong to the page being driven.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import socket
import struct
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit

from blumkin.browser.cdp import BrowserActionError

DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

_ALLOWED_METHODS = frozenset({"Page.getFrameTree", "Runtime.evaluate"})
_ARROW_CALL = re.compile(r"\s*\(\s*\)\s*=>")
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LOOPBACK_HOSTS = ("127.0.0.1", "::1")


class RawFrameError(BrowserActionError):
    """A frame read over its own DevTools socket failed."""


class _Caller(Protocol):
    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


class CdpSocket:
    """A synchronous DevTools JSON-RPC client over a loopback websocket."""

    def __init__(self, sock: socket.socket, timeout_s: float, max_message_bytes: int) -> None:
        self._closed = False
        self._max = max_message_bytes
        self._next_id = 0
        self._sock = sock
        self._timeout_s = timeout_s

    @classmethod
    def connect(
        cls,
        ws_url: str,
        *,
        timeout_s: float,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> CdpSocket:
        """Open `ws_url` (a plain `ws://` address on this machine) and complete the handshake."""
        parsed = _loopback_websocket(ws_url)
        if parsed is None:
            raise RawFrameError(f"Expected a loopback ws:// DevTools URL, got {ws_url!r}")
        host, port, path = parsed
        try:
            sock = socket.create_connection((host, port), timeout=timeout_s)
        except OSError as exc:
            raise RawFrameError(f"Expected to connect to {host}:{port}, got {exc}") from exc
        try:
            _handshake(sock, host, port, path, timeout_s)
        except BaseException:
            sock.close()
            raise
        return cls(sock, timeout_s, max_message_bytes)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one allowed request and return its result, skipping events and other answers."""
        if method not in _ALLOWED_METHODS:
            raise RawFrameError(
                f"Expected a DevTools call blumkin allows, got {method!r}: not allowed"
            )
        if self._closed:
            raise RawFrameError("Expected an open DevTools socket, got a closed one")
        self._next_id += 1
        request = {"id": self._next_id, "method": method, "params": params or {}}
        deadline = time.monotonic() + self._timeout_s
        self._send(json.dumps(request).encode())
        while True:
            message = json.loads(self._read_message(deadline))
            if not isinstance(message, dict) or message.get("id") != request["id"]:
                continue
            error = message.get("error")
            if error:
                detail = error.get("message", "unknown error") if isinstance(error, dict) else error
                raise RawFrameError(f"Expected {method} to succeed, got {detail}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            self._sock.sendall(_encode_frame(8, b""))
        with contextlib.suppress(OSError):
            self._sock.close()

    def _read_exact(self, count: int, deadline: float) -> bytes:
        data = b""
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RawFrameError("Expected a DevTools answer, got none: timed out")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(count - len(data))
            except TimeoutError as exc:
                raise RawFrameError("Expected a DevTools answer, got none: timed out") from exc
            except OSError as exc:
                raise RawFrameError(f"Expected a DevTools answer, got {exc}") from exc
            if not chunk:
                raise RawFrameError("Expected a DevTools answer, got a closed socket")
            data += chunk
        return data

    def _read_message(self, deadline: float) -> str:
        parts: list[bytes] = []
        size = 0
        while True:
            first, second = self._read_exact(2, deadline)
            fin, opcode = bool(first & 0x80), first & 0x0F
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", self._read_exact(2, deadline))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._read_exact(8, deadline))
            if second & 0x80:
                raise RawFrameError("Expected an unmasked server frame, got a masked one")
            size += length
            if size > self._max:
                raise RawFrameError(f"DevTools sent a message larger than {self._max} bytes")
            payload = self._read_exact(length, deadline) if length else b""
            if opcode == 8:
                raise RawFrameError("Expected a DevTools answer, got a closed socket")
            if opcode == 9:
                self._send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def _send(self, payload: bytes, *, opcode: int = 1) -> None:
        try:
            self._sock.sendall(_encode_frame(opcode, payload))
        except OSError as exc:
            raise RawFrameError(f"Expected to send to DevTools, got {exc}") from exc


class RawFrame:
    """One frame read over its own DevTools socket; it answers like a Playwright frame."""

    def __init__(self, sock: _Caller, target_id: str, url: str) -> None:
        self.target_id = target_id
        self._closed = False
        self._parent: str | None = None
        self._sock = sock
        self._url = url

    @property
    def parent_id(self) -> str:
        """The id of the frame this one is embedded in, read once."""
        if self._parent is None:
            tree = self._sock.call("Page.getFrameTree")
            frame = tree.get("frameTree", {}).get("frame", {})
            self._parent = str(frame.get("parentId", ""))
        return self._parent

    @property
    def url(self) -> str:
        """The frame's address, read from the frame now ("" once it is gone)."""
        if self._closed:
            return ""
        try:
            value = self.evaluate("location.href")
        except RawFrameError:
            return ""
        return value if isinstance(value, str) else self._url

    def close(self) -> None:
        self._closed = True
        self._sock.close()

    def evaluate(self, script: str) -> Any:
        """Run an expression, or an `() => ...` function, in the frame and return its value."""
        expression = f"({script})()" if _ARROW_CALL.match(script) else script
        result = self._sock.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
        )
        details = result.get("exceptionDetails")
        if details:
            exception = details.get("exception", {}) if isinstance(details, dict) else {}
            text = exception.get("description") or details.get("text") or "an exception"
            raise RawFrameError(f"Expected the script to run in the frame, got {text}")
        return result.get("result", {}).get("value")

    def locator(self, selector: str) -> _NoAriaSnapshot:
        return _NoAriaSnapshot()


class _NoAriaSnapshot:
    def aria_snapshot(self, *, timeout: float) -> str:
        raise BrowserActionError(
            "Expected an ARIA snapshot, got none: this frame is read over its own DevTools "
            "socket, which has no snapshot; run `blumkin browser refresh-tab` while attached "
            "or read its text instead"
        )


class ExistingFrames:
    """The frames of the driven page that Playwright does not list, with their sockets kept."""

    def __init__(
        self,
        *,
        endpoint_port: int,
        fetch_targets: Callable[[], Sequence[Mapping[str, Any]]],
        main_frame_id: Callable[[], str],
        open_socket: Callable[[str], _Caller],
    ) -> None:
        self._endpoint_port = endpoint_port
        self._fetch_targets = fetch_targets
        self._frames: dict[str, RawFrame] = {}
        self._main_frame_id = main_frame_id
        self._open_socket = open_socket
        self._rejected: set[str] = set()

    def close(self) -> None:
        for frame in self._frames.values():
            with contextlib.suppress(RawFrameError):
                frame.close()
        self._frames = {}

    def frames(self, known_urls: set[str]) -> list[RawFrame]:
        """The page's frames that are not in `known_urls`, each with its socket open."""
        try:
            targets = [
                target
                for target in self._fetch_targets()
                if _debugger_port(target) == self._endpoint_port
            ]
            found = discover_frames(
                targets,
                main_frame_id=self._main_frame_id(),
                known_urls=known_urls,
                open_socket=self._open_socket,
                reuse=self._frames,
                rejected=self._rejected,
            )
        except OSError, ValueError, RawFrameError:
            return []
        kept = {frame.target_id for frame in found}
        for target_id in [key for key in self._frames if key not in kept]:
            with contextlib.suppress(RawFrameError):
                self._frames.pop(target_id).close()
        self._frames.update({frame.target_id: frame for frame in found})
        return found


def discover_frames(
    targets: Iterable[Mapping[str, Any]],
    *,
    main_frame_id: str,
    known_urls: set[str],
    open_socket: Callable[[str], _Caller],
    reuse: dict[str, RawFrame],
    rejected: set[str] | None = None,
) -> list[RawFrame]:
    """Frame targets embedded (directly or through other frames) in the page `main_frame_id`.

    Targets whose address Playwright already lists are skipped, as are debugger URLs that are
    not on this machine. A frame is opened once and reused through `reuse`; a frame that turns
    out to belong to another page is closed and remembered in `rejected`.
    """
    declined = rejected if rejected is not None else set()
    candidates = [
        target
        for target in targets
        if target.get("type") == "iframe"
        and str(target.get("id", "")) not in declined
        and str(target.get("url", "")) not in known_urls
        and _loopback_websocket(str(target.get("webSocketDebuggerUrl", ""))) is not None
    ]
    opened: dict[str, RawFrame] = {}
    for target in candidates:
        target_id = str(target["id"])
        if target_id in reuse:
            opened[target_id] = reuse[target_id]
            continue
        try:
            opened[target_id] = RawFrame(
                open_socket(str(target["webSocketDebuggerUrl"])),
                target_id,
                str(target.get("url", "")),
            )
        except RawFrameError:
            declined.add(target_id)
    kept_ids = {main_frame_id}
    kept: set[str] = set()
    changed = True
    while changed:
        changed = False
        for target_id, frame in opened.items():
            if target_id in kept:
                continue
            try:
                parent = frame.parent_id
            except RawFrameError:
                parent = ""
            if parent in kept_ids:
                kept.add(target_id)
                kept_ids.add(target_id)
                changed = True
    for target_id, frame in opened.items():
        if target_id not in kept and target_id not in reuse:
            declined.add(target_id)
            with contextlib.suppress(RawFrameError):
                frame.close()
    return [opened[str(target["id"])] for target in candidates if str(target["id"]) in kept]


def _debugger_port(target: Mapping[str, Any]) -> int | None:
    parsed = _loopback_websocket(str(target.get("webSocketDebuggerUrl", "")))
    return parsed[1] if parsed is not None else None


def _encode_frame(opcode: int, payload: bytes) -> bytes:
    """A final, masked client frame."""
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, 0x80 | length])
    elif length < 65536:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
    mask = os.urandom(4)
    return header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def _handshake(sock: socket.socket, host: str, port: int, path: str, timeout_s: float) -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    address = f"[{host}]" if ":" in host else host
    sock.settimeout(timeout_s)
    try:
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {address}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RawFrameError("Expected a websocket handshake answer, got a closed socket")
            response += chunk
            if len(response) > 65536:
                raise RawFrameError("Expected a short websocket handshake answer, got a long one")
    except TimeoutError as exc:
        raise RawFrameError("Expected a websocket handshake answer, got none: timed out") from exc
    except OSError as exc:
        raise RawFrameError(f"Expected a websocket handshake, got {exc}") from exc
    head = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    if len(lines[0].split(" ", 2)) < 2 or lines[0].split(" ", 2)[1] != "101":
        raise RawFrameError(f"Expected HTTP 101 Switching Protocols, got {lines[0]!r}")
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in lines[1:])
    }
    expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    if headers.get("sec-websocket-accept") != expected:
        raise RawFrameError("Expected a matching Sec-WebSocket-Accept, got a different one")


def _loopback_websocket(url: str) -> tuple[str, int, str] | None:
    """(host, port, path) of a plain ws:// URL on this machine, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "ws"
        or parts.hostname not in _LOOPBACK_HOSTS
        or port is None
        or parts.username is not None
        or parts.password is not None
        or any(char.isspace() or char == "\\" for char in url)
    ):
        return None
    path = parts.path or "/"
    return parts.hostname, port, f"{path}?{parts.query}" if parts.query else path
