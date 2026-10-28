"""A loopback-only DevTools websocket client and the frames it reaches (issue #551)."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from blumkin.browser.cdp import BrowserActionError
from blumkin.browser.cdpws import (
    CdpSocket,
    RawFrame,
    RawFrameError,
    discover_frames,
)

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _Server:
    """A one-connection fake DevTools websocket server driven by a handler per request."""

    def __init__(self, handler: Callable[[dict[str, Any]], list[bytes] | None]) -> None:
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        self.accept_override: str | None = None
        self.after_handshake = b""
        self.omit_upgrade = False
        self.status = "101 Switching Protocols"
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/devtools/page/ABC"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._listener.close()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            key = next(
                line.split(b":", 1)[1].strip()
                for line in data.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key")
            ).decode()
            accept = (
                self.accept_override
                or base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
            )
            upgrade = "" if self.omit_upgrade else "Upgrade: websocket\r\n"
            conn.sendall(
                f"HTTP/1.1 {self.status}\r\n{upgrade}Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
                + self.after_handshake
            )
            if not self.status.startswith("101"):
                return
            while True:
                request = self._read_frame(conn)
                if request is None:
                    return
                self.requests.append(request)
                for frame in self.handler(request) or []:
                    conn.sendall(frame)

    @staticmethod
    def _read_frame(conn: socket.socket) -> dict[str, Any] | None:
        while True:
            header = _recv(conn, 2)
            if header is None:
                return None
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", _recv(conn, 2) or b"")[0]
            elif length == 127:
                length = struct.unpack(">Q", _recv(conn, 8) or b"")[0]
            mask = _recv(conn, 4) or b""
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(_recv(conn, length) or b""))
            opcode = header[0] & 0x0F
            if opcode == 8:
                return None
            if opcode in (9, 10):
                continue
            return json.loads(payload)


def _recv(conn: socket.socket, count: int) -> bytes | None:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def text_frame(payload: dict[str, Any] | str, *, opcode: int = 1, fin: bool = True) -> bytes:
    body = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
    first = (0x80 if fin else 0) | opcode
    if len(body) < 126:
        return bytes([first, len(body)]) + body
    if len(body) < 65536:
        return bytes([first, 126]) + struct.pack(">H", len(body)) + body
    return bytes([first, 127]) + struct.pack(">Q", len(body)) + body


def reply(request: dict[str, Any], result: dict[str, Any]) -> list[bytes]:
    return [text_frame({"id": request["id"], "result": result})]


@pytest.fixture
def server() -> Iterator[Callable[..., _Server]]:
    made: list[_Server] = []

    def make(handler: Callable[[dict[str, Any]], list[bytes] | None]) -> _Server:
        fake = _Server(handler)
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()


def _echo(request: dict[str, Any]) -> list[bytes]:
    return reply(request, {"result": {"type": "string", "value": "ok"}})


def test_a_call_returns_the_result_of_the_matching_request(server: Any) -> None:
    fake = server(_echo)
    sock = CdpSocket.connect(fake.url, timeout_s=5)
    try:
        assert sock.call("Runtime.evaluate", {"expression": "1"}) == {
            "result": {"type": "string", "value": "ok"}
        }
    finally:
        sock.close()
    assert fake.requests[0]["method"] == "Runtime.evaluate"
    assert fake.requests[0]["params"] == {"expression": "1"}


def test_events_and_other_ids_before_the_answer_are_ignored(server: Any) -> None:
    def handler(request: dict[str, Any]) -> list[bytes]:
        return [
            text_frame({"method": "Page.frameNavigated", "params": {}}),
            text_frame({"id": request["id"] + 99, "result": {}}),
            *reply(request, {"ok": True}),
        ]

    sock = CdpSocket.connect(server(handler).url, timeout_s=5)
    try:
        assert sock.call("Page.getFrameTree") == {"ok": True}
    finally:
        sock.close()


def test_a_ping_is_answered_and_a_fragmented_answer_is_joined(server: Any) -> None:
    def handler(request: dict[str, Any]) -> list[bytes]:
        whole = json.dumps({"id": request["id"], "result": {"v": "x" * 200}})
        return [
            text_frame("", opcode=9),
            text_frame(whole[:80], opcode=1, fin=False),
            text_frame(whole[80:150], opcode=0, fin=False),
            text_frame(whole[150:], opcode=0, fin=True),
        ]

    sock = CdpSocket.connect(server(handler).url, timeout_s=5)
    try:
        assert sock.call("Runtime.evaluate", {"expression": "1"}) == {"v": "x" * 200}
    finally:
        sock.close()


def test_an_error_answer_raises_with_the_message(server: Any) -> None:
    def handler(request: dict[str, Any]) -> list[bytes]:
        return [text_frame({"id": request["id"], "error": {"message": "Cannot find context"}})]

    sock = CdpSocket.connect(server(handler).url, timeout_s=5)
    try:
        with pytest.raises(RawFrameError, match="Cannot find context"):
            sock.call("Runtime.evaluate", {"expression": "1"})
    finally:
        sock.close()


def test_only_the_calls_blumkin_needs_are_allowed(server: Any) -> None:
    fake = server(_echo)
    sock = CdpSocket.connect(fake.url, timeout_s=5)
    try:
        for method in ("Page.navigate", "Input.dispatchMouseEvent", "Target.closeTarget"):
            with pytest.raises(RawFrameError, match="not allowed"):
                sock.call(method)
    finally:
        sock.close()
    assert fake.requests == []


def test_a_handshake_with_the_wrong_accept_key_is_refused(server: Any) -> None:
    fake = server(_echo)
    fake.accept_override = "bm90IHRoZSByaWdodCBrZXk="
    with pytest.raises(RawFrameError, match="Sec-WebSocket-Accept"):
        CdpSocket.connect(fake.url, timeout_s=5)


def test_a_handshake_that_does_not_switch_protocols_is_refused(server: Any) -> None:
    fake = server(_echo)
    fake.status = "403 Forbidden"
    with pytest.raises(RawFrameError, match="403"):
        CdpSocket.connect(fake.url, timeout_s=5)


@pytest.mark.parametrize(
    "url",
    ["ws://example.com:9222/devtools/page/A", "wss://127.0.0.1:9222/x", "http://127.0.0.1:9222/x"],
)
def test_only_a_plain_loopback_websocket_url_is_connected_to(url: str) -> None:
    with pytest.raises(RawFrameError, match="loopback"):
        CdpSocket.connect(url, timeout_s=1)


def test_a_silent_server_times_out(server: Any) -> None:
    sock = CdpSocket.connect(server(lambda request: None).url, timeout_s=0.3)
    try:
        with pytest.raises(RawFrameError, match="timed out"):
            sock.call("Runtime.evaluate", {"expression": "1"})
    finally:
        sock.close()


def test_a_message_over_the_size_cap_is_refused(server: Any) -> None:
    def handler(request: dict[str, Any]) -> list[bytes]:
        return [text_frame({"id": request["id"], "result": {"v": "x" * 300}})]

    sock = CdpSocket.connect(server(handler).url, timeout_s=5, max_message_bytes=200)
    try:
        with pytest.raises(RawFrameError, match="larger than"):
            sock.call("Runtime.evaluate", {"expression": "1"})
    finally:
        sock.close()


def _frame_server(server: Any, *, href: str = "http://localhost:1/pane") -> _Server:
    def handler(request: dict[str, Any]) -> list[bytes]:
        if request["method"] == "Page.getFrameTree":
            return reply(request, {"frameTree": {"frame": {"id": "F1", "parentId": "MAIN"}}})
        expression = request["params"]["expression"]
        value: Any = href if expression == "location.href" else f"ran:{expression}"
        return reply(request, {"result": {"type": "string", "value": value}})

    return server(handler)


def test_a_raw_frame_wraps_an_arrow_function_in_a_call_and_leaves_an_expression_alone(
    server: Any,
) -> None:
    fake = _frame_server(server)
    frame = RawFrame(CdpSocket.connect(fake.url, timeout_s=5), target_id="F1", url="")
    try:
        assert frame.evaluate("document.body.innerText") == "ran:document.body.innerText"
        assert frame.evaluate("() => { return 1; }") == "ran:(() => { return 1; })()"
    finally:
        frame.close()
    assert [r["params"]["returnByValue"] for r in fake.requests] == [True, True]


def test_a_raw_frame_url_is_read_live_and_empty_when_the_frame_is_gone(server: Any) -> None:
    fake = _frame_server(server, href="http://localhost:2/moved")
    frame = RawFrame(CdpSocket.connect(fake.url, timeout_s=5), target_id="F1", url="stale")
    assert frame.url == "http://localhost:2/moved"
    frame.close()
    assert frame.url == ""


def test_a_raw_frame_has_no_aria_snapshot_and_says_why(server: Any) -> None:
    frame = RawFrame(CdpSocket.connect(_frame_server(server).url, timeout_s=5), "F1", "")
    try:
        with pytest.raises(BrowserActionError, match="refresh-tab"):
            frame.locator("body").aria_snapshot(timeout=1000)
    finally:
        frame.close()


def test_a_raw_frame_evaluate_error_is_a_raw_frame_error(server: Any) -> None:
    def handler(request: dict[str, Any]) -> list[bytes]:
        return reply(
            request,
            {"exceptionDetails": {"text": "Uncaught", "exception": {"description": "boom"}}},
        )

    frame = RawFrame(CdpSocket.connect(server(handler).url, timeout_s=5), "F1", "")
    try:
        with pytest.raises(RawFrameError, match="boom"):
            frame.evaluate("1")
    finally:
        frame.close()


class _Opened:
    def __init__(self, parent: str, children: tuple[str, ...] = ()) -> None:
        self.parent = parent
        self.children = children
        self.closed = False

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        assert method == "Page.getFrameTree"
        return {
            "frameTree": {
                "frame": {"id": "x", "parentId": self.parent},
                "childFrames": [{"frame": {"id": child}} for child in self.children],
            }
        }

    def close(self) -> None:
        self.closed = True


def _targets() -> list[dict[str, Any]]:
    return [
        {
            "type": "page",
            "id": "MAIN",
            "url": "http://127.0.0.1:1/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/p",
        },
        {
            "type": "iframe",
            "id": "A",
            "url": "http://localhost:2/pane",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/a",
        },
        {
            "type": "iframe",
            "id": "B",
            "url": "http://other.test/x",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/b",
        },
        {
            "type": "iframe",
            "id": "N",
            "url": "http://localhost:3/nested",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/n",
        },
        {
            "type": "background_page",
            "id": "E",
            "url": "chrome-extension://x",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/e",
        },
    ]


def _opener(parents: dict[str, str]) -> Callable[[str], Any]:
    return lambda ws_url: _Opened(parents[ws_url.rsplit("/", 1)[1]])


def test_discovery_keeps_frames_below_this_page_including_nested_ones() -> None:
    found = discover_frames(
        _targets(),
        page_frame_ids={"MAIN"},
        known_urls=set(),
        open_socket=_opener({"a": "MAIN", "b": "ELSEWHERE", "n": "A"}),
        reuse={},
    )
    assert [f.target_id for f in found] == ["A", "N"]


def test_discovery_skips_frames_playwright_already_has_and_non_frame_targets() -> None:
    found = discover_frames(
        _targets(),
        page_frame_ids={"MAIN"},
        known_urls={"http://localhost:2/pane"},
        open_socket=_opener({"a": "MAIN", "b": "MAIN", "n": "A"}),
        reuse={},
    )
    assert [f.target_id for f in found] == ["B"]


def test_discovery_reuses_an_open_frame_and_closes_the_ones_it_does_not_keep() -> None:
    kept = RawFrame(_Opened("MAIN"), target_id="A", url="old")  # type: ignore[arg-type]
    rejected = _Opened("ELSEWHERE")
    reuse = {"A": kept}
    found = discover_frames(
        _targets(),
        page_frame_ids={"MAIN"},
        known_urls=set(),
        open_socket=lambda ws_url: rejected if ws_url.endswith("/b") else _Opened("MAIN"),
        reuse=reuse,
    )
    assert found[0] is kept
    assert rejected.closed is True


def test_discovery_refuses_a_debugger_url_that_is_not_loopback() -> None:
    targets = [
        {
            "type": "iframe",
            "id": "A",
            "url": "http://localhost:2/",
            "webSocketDebuggerUrl": "ws://evil.test:9/a",
        }
    ]
    assert (
        discover_frames(
            targets,
            page_frame_ids={"MAIN"},
            known_urls=set(),
            open_socket=_opener({"a": "MAIN"}),
            reuse={},
        )
        == []
    )


def _raw_frames(*frames: bytes) -> Callable[[dict[str, Any]], list[bytes]]:
    return lambda request: list(frames)


def _connect_and_call(server: Any, handler: Callable[[dict[str, Any]], list[bytes]]) -> Any:
    sock = CdpSocket.connect(server(handler).url, timeout_s=2)
    try:
        return sock.call("Runtime.evaluate", {"expression": "1"})
    finally:
        sock.close()


@pytest.mark.parametrize(
    "frame",
    [
        bytes([0x82, 2]) + b"{}",  # binary data frame
        bytes([0x83, 0]),  # reserved opcode
        bytes([0xC1, 2]) + b"{}",  # a reserved bit set
        bytes([0x80, 2]) + b"{}",  # continuation with nothing to continue
        bytes([0x09, 0]),  # fragmented ping
        bytes([0x89, 126, 0, 200]) + b"x" * 200,  # ping over 125 bytes
    ],
)
def test_frames_that_break_the_websocket_rules_are_refused(server: Any, frame: bytes) -> None:
    with pytest.raises(RawFrameError, match="Expected"):
        _connect_and_call(server, _raw_frames(frame))


def test_a_new_data_frame_in_the_middle_of_a_fragmented_message_is_refused(server: Any) -> None:
    broken = [text_frame("{", fin=False), text_frame("{}")]
    with pytest.raises(RawFrameError, match="fragment"):
        _connect_and_call(server, _raw_frames(*broken))


def test_control_frames_do_not_count_toward_the_message_size_cap(server: Any) -> None:
    def handler(request: dict[str, Any]) -> list[bytes]:
        pings = [text_frame("p" * 100, opcode=9) for _ in range(20)]
        return [*pings, *reply(request, {"ok": True})]

    sock = CdpSocket.connect(server(handler).url, timeout_s=5, max_message_bytes=500)
    try:
        assert sock.call("Page.getFrameTree") == {"ok": True}
    finally:
        sock.close()


def test_a_frame_sent_with_the_handshake_answer_is_not_lost(server: Any) -> None:
    fake = server(_echo)
    fake.after_handshake = text_frame({"method": "Runtime.executionContextCreated", "params": {}})
    sock = CdpSocket.connect(fake.url, timeout_s=5)
    try:
        assert sock.call("Runtime.evaluate", {"expression": "1"}) == {
            "result": {"type": "string", "value": "ok"}
        }
    finally:
        sock.close()


def test_a_handshake_without_the_upgrade_header_is_refused(server: Any) -> None:
    fake = server(_echo)
    fake.omit_upgrade = True
    with pytest.raises(RawFrameError, match="Upgrade"):
        CdpSocket.connect(fake.url, timeout_s=5)


def test_invalid_json_from_the_peer_is_a_raw_frame_error(server: Any) -> None:
    with pytest.raises(RawFrameError, match="JSON"):
        _connect_and_call(server, _raw_frames(text_frame("not json")))


def test_a_failed_call_closes_the_socket_so_the_stream_cannot_desync(server: Any) -> None:
    sock = CdpSocket.connect(server(lambda request: None).url, timeout_s=0.3)
    with pytest.raises(RawFrameError, match="timed out"):
        sock.call("Runtime.evaluate", {"expression": "1"})
    assert sock.closed is True
    with pytest.raises(RawFrameError, match="closed"):
        sock.call("Runtime.evaluate", {"expression": "1"})


def test_a_raw_frame_whose_url_is_not_text_reports_no_address(server: Any) -> None:
    def handler(request: dict[str, Any]) -> list[bytes]:
        return reply(request, {"result": {"type": "object", "value": {"a": 1}}})

    frame = RawFrame(CdpSocket.connect(server(handler).url, timeout_s=5), "F1", "http://stale/")
    try:
        assert frame.url == ""
    finally:
        frame.close()


def test_a_raw_frame_reports_when_its_socket_is_closed(server: Any) -> None:
    frame = RawFrame(CdpSocket.connect(_frame_server(server).url, timeout_s=5), "F1", "")
    assert frame.is_open is True
    frame.close()
    assert frame.is_open is False


def test_discovery_finds_a_frame_nested_under_a_frame_playwright_already_lists() -> None:
    found = discover_frames(
        _targets(),
        page_frame_ids={"MAIN", "SAME-PROCESS-CHILD"},
        known_urls=set(),
        open_socket=_opener({"a": "SAME-PROCESS-CHILD", "b": "ELSEWHERE", "n": "ELSEWHERE"}),
        reuse={},
    )
    assert [f.target_id for f in found] == ["A"]


def test_discovery_does_not_remember_a_frame_it_could_not_ask_as_rejected() -> None:
    class _Failing(_Opened):
        def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            *,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            raise RawFrameError("Expected Page.getFrameTree to succeed, got loading")

    rejected: set[str] = set()
    first = discover_frames(
        _targets(),
        page_frame_ids={"MAIN"},
        known_urls=set(),
        open_socket=lambda ws_url: _Failing("") if ws_url.endswith("/a") else _Opened("ELSEWHERE"),
        reuse={},
        rejected=rejected,
    )
    assert first == []
    assert "A" not in rejected
    assert "B" in rejected
    second = discover_frames(
        _targets(),
        page_frame_ids={"MAIN"},
        known_urls=set(),
        open_socket=_opener({"a": "MAIN", "b": "ELSEWHERE", "n": "ELSEWHERE"}),
        reuse={},
        rejected=rejected,
    )
    assert [f.target_id for f in second] == ["A"]


def test_discovery_does_not_remember_a_socket_that_would_not_open() -> None:
    def flaky(ws_url: str) -> Any:
        raise RawFrameError("Expected to connect, got refused")

    rejected: set[str] = set()
    assert (
        discover_frames(
            _targets(),
            page_frame_ids={"MAIN"},
            known_urls=set(),
            open_socket=flaky,
            reuse={},
            rejected=rejected,
        )
        == []
    )
    assert rejected == set()


def test_discovery_opens_at_most_a_fixed_number_of_frames() -> None:
    many = [
        {
            "type": "iframe",
            "id": f"T{i}",
            "url": f"http://localhost:{i}/",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:9/t{i}",
        }
        for i in range(100)
    ]
    opened: list[str] = []

    def opener(ws_url: str) -> Any:
        opened.append(ws_url)
        return _Opened("MAIN")

    found = discover_frames(
        many, page_frame_ids={"MAIN"}, known_urls=set(), open_socket=opener, reuse={}
    )
    assert len(opened) == len(found) == 32


def test_a_complete_error_answer_keeps_the_socket_open_for_the_next_call(server: Any) -> None:
    answers: list[str] = []

    def handler(request: dict[str, Any]) -> list[bytes]:
        answers.append(request["method"])
        if len(answers) == 1:
            return [
                text_frame(
                    {"id": request["id"], "error": {"message": "Execution context was destroyed"}}
                )
            ]
        return reply(request, {"ok": True})

    sock = CdpSocket.connect(server(handler).url, timeout_s=5)
    try:
        with pytest.raises(RawFrameError, match="destroyed"):
            sock.call("Runtime.evaluate", {"expression": "1"})
        assert sock.closed is False
        assert sock.call("Runtime.evaluate", {"expression": "1"}) == {"ok": True}
    finally:
        sock.close()


def test_a_call_can_be_given_its_own_timeout(server: Any) -> None:
    sock = CdpSocket.connect(server(lambda request: None).url, timeout_s=30)
    try:
        started = time.monotonic()
        with pytest.raises(RawFrameError, match="timed out"):
            sock.call("Runtime.evaluate", {"expression": "1"}, timeout_s=0.3)
        assert time.monotonic() - started < 5
    finally:
        sock.close()


def test_discovery_follows_same_process_children_of_a_frame_it_reads() -> None:
    parents = {
        "a": ("MAIN", ("SAME-PROCESS-X",)),
        "n": ("SAME-PROCESS-X", ()),
        "b": ("ELSEWHERE", ()),
    }
    found = discover_frames(
        _targets(),
        page_frame_ids={"MAIN"},
        known_urls=[],
        open_socket=lambda ws_url: _Opened(*parents[ws_url.rsplit("/", 1)[1]]),
        reuse={},
    )
    assert [f.target_id for f in found] == ["A", "N"]


def test_a_frame_whose_parent_is_a_child_of_another_candidate_is_not_declared_foreign() -> None:
    rejected: set[str] = set()
    discover_frames(
        _targets(),
        page_frame_ids={"MAIN"},
        known_urls=[],
        open_socket=lambda ws_url: _Opened(
            {"a": "MAIN", "b": "ELSEWHERE", "n": "SAME-PROCESS-X"}[ws_url.rsplit("/", 1)[1]],
            ("SAME-PROCESS-X",) if ws_url.endswith("/a") else (),
        ),
        reuse={},
        rejected=rejected,
    )
    assert rejected == {"B"}


def test_known_addresses_are_matched_one_for_one_not_by_address() -> None:
    twins = [
        {
            "type": "iframe",
            "id": "T1",
            "url": "http://localhost:2/pane",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/t1",
        },
        {
            "type": "iframe",
            "id": "T2",
            "url": "http://localhost:2/pane",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9/t2",
        },
    ]
    found = discover_frames(
        twins,
        page_frame_ids={"MAIN"},
        known_urls=["http://localhost:2/pane"],
        open_socket=lambda ws_url: _Opened("MAIN"),
        reuse={},
    )
    assert [f.target_id for f in found] == ["T2"]


def test_a_raw_frame_script_gets_the_evaluate_timeout_not_the_probe_one(server: Any) -> None:
    sock = CdpSocket.connect(server(lambda request: None).url, timeout_s=0.2)
    frame = RawFrame(sock, "F1", "", evaluate_timeout_s=0.4)
    try:
        started = time.monotonic()
        with pytest.raises(RawFrameError, match="timed out"):
            frame.evaluate("1")
        assert time.monotonic() - started >= 0.3
    finally:
        frame.close()
