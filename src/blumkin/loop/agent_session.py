"""Run the operator's agent in a pty so it works through the loop's waiting items (issue #538).

blumkin calls no model: the configured agent command is started, told to run the blumkin loop,
and watched until the items that were waiting are closed (finished or handed back). Everything
sent to the agent and everything it prints goes to a private, timestamped log.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pexpect

from blumkin.browser.profiles import BrowserProfileError, ensure_private_dir
from blumkin.loop.agent_task import pending_items
from blumkin.loop.config import LoopSetup
from blumkin.loop.kill_switch import is_engaged

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CHECK_INTERVAL_SECONDS = 1.0
_DEFAULT_TERM = "xterm-256color"
_EXIT_GRACE_SECONDS = 5.0
_PARTIAL_LINE_LIMIT = 2000
_READ_SIZE = 4096
_READY_LIMIT_SECONDS = 60.0
_SUBMIT_DELAY_SECONDS = 0.2


class AgentSessionError(RuntimeError):
    """The session could not be started (configuration, kill switch)."""


@dataclass(frozen=True, slots=True)
class SessionResult:
    """What a session did; `ok` is false when items are still waiting."""

    handled: tuple[str, ...]
    log: Path | None
    message: str
    ok: bool
    remaining: tuple[str, ...]
    started: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "handled": list(self.handled),
            "log": str(self.log) if self.log else None,
            "message": self.message,
            "ok": self.ok,
            "remaining": list(self.remaining),
            "started": self.started,
        }


def run_agent_session(setup: LoopSetup) -> SessionResult:
    """Start the agent when items are waiting, send the loop prompt and wait for them to close."""
    if is_engaged(setup.config_dir, setup.loop):
        raise AgentSessionError("The loop kill switch is engaged, so no agent was started")
    waiting = tuple(item.id for item in pending_items(setup.config_dir))
    if not waiting:
        return SessionResult((), None, "nothing waiting", True, (), False)
    command = setup.loop.agent_command
    if not command:
        raise AgentSessionError(
            "Expected loop.agent_command in config.toml (the command that starts your agent), "
            "got none"
        )
    if not hasattr(pexpect, "spawn"):
        raise AgentSessionError("Expected a platform with pseudo-terminals, got none")
    with _session_lock(setup.config_dir):
        log = _Log.create(setup.config_dir)
        try:
            failure = _drive(setup, command, waiting, log)
        finally:
            log.close()
    remaining = tuple(i for i in waiting if i in {x.id for x in pending_items(setup.config_dir)})
    handled = tuple(i for i in waiting if i not in remaining)
    if failure is None and remaining:
        failure = "The agent ended while items were still waiting"
    if failure is not None:
        return SessionResult(handled, log.path, failure, False, remaining, True)
    return SessionResult(handled, log.path, f"{len(handled)} handled", True, remaining, True)


class _Log:
    """The session log: one timestamped line per prompt sent and per line the agent printed."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._handle = os.fdopen(fd, "w", encoding="utf-8")
        self._partial = ""

    @classmethod
    def create(cls, config_dir: Path) -> _Log:
        try:
            directory = ensure_private_dir(config_dir / "loop" / "agent-logs")
        except BrowserProfileError as exc:
            raise AgentSessionError(str(exc)) from exc
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{stamp}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return cls(path, fd)

    def close(self) -> None:
        self.flush()
        self._handle.close()

    def flush(self) -> None:
        """Write out an unfinished line (the agent went quiet, or the session ended)."""
        tail, self._partial = self._partial, ""
        self._write("<<", tail)

    def received(self, text: str) -> None:
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            self._write("<<", line)
        if len(self._partial) > _PARTIAL_LINE_LIMIT:
            self.flush()

    def sent(self, text: str) -> None:
        self.flush()
        self._write(">>", text)

    def _write(self, marker: str, text: str) -> None:
        cleaned = _ANSI.sub("", text).replace("\r", "")
        if not cleaned.strip():
            return
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        self._handle.write(f"{stamp} {marker} {cleaned}\n")
        self._handle.flush()


def _drive(
    setup: LoopSetup, command: tuple[str, ...], waiting: tuple[str, ...], log: _Log
) -> str | None:
    """Run the conversation; returns why it failed, or None when every waiting item closed."""
    settings = setup.loop
    deadline = time.monotonic() + settings.agent_timeout_seconds
    environment = {**os.environ}
    environment.setdefault("TERM", _DEFAULT_TERM)
    try:
        child = pexpect.spawn(
            os.path.expanduser(command[0]),
            list(command[1:]),
            codec_errors="replace",
            dimensions=(50, 200),
            echo=False,
            encoding="utf-8",
            env=environment,
        )
    except (OSError, pexpect.ExceptionPexpect) as exc:
        return f"Expected to start {command[0]}, got {exc}"
    try:
        ready = _read_until_quiet(
            child, log, settings.agent_quiet_seconds, _ready_deadline(deadline)
        )
        if ready == "eof":
            return "The agent exited before the prompt was sent"
        _submit(child, log, settings.agent_prompt)
        outcome = _watch(child, log, setup, waiting, deadline)
        if outcome != "closed":
            return outcome
        _submit(child, log, settings.agent_exit_input)
        _read_until_eof(child, log, _EXIT_GRACE_SECONDS)
        return None
    finally:
        _stop(child)


def _read_until_eof(child: pexpect.spawn, log: _Log, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            log.received(child.read_nonblocking(_READ_SIZE, timeout=0.2))
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            return


def _read_until_quiet(child: pexpect.spawn, log: _Log, quiet: float, deadline: float) -> str:
    """Read until the agent has been silent for `quiet` seconds: "quiet", "eof" or "deadline"."""
    while time.monotonic() < deadline:
        try:
            log.received(child.read_nonblocking(_READ_SIZE, timeout=quiet))
        except pexpect.TIMEOUT:
            return "quiet"
        except pexpect.EOF:
            return "eof"
    return "deadline"


def _ready_deadline(deadline: float) -> float:
    return min(deadline, time.monotonic() + _READY_LIMIT_SECONDS)


@contextlib.contextmanager
def _session_lock(config_dir: Path) -> Iterator[None]:
    """One session at a time: a second `loop agent` would drive the same waiting items."""
    try:
        directory = ensure_private_dir(config_dir / "loop")
    except BrowserProfileError as exc:
        raise AgentSessionError(str(exc)) from exc
    fd = os.open(directory / "agent.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentSessionError("A loop agent session is already running") from exc
        yield
    finally:
        os.close(fd)


def _still_waiting(setup: LoopSetup, waiting: tuple[str, ...]) -> bool:
    open_ids = {item.id for item in pending_items(setup.config_dir)}
    return any(item in open_ids for item in waiting)


def _stop(child: pexpect.spawn) -> None:
    """End the agent and anything it started (the pty child leads its own process group)."""
    pid = child.pid
    if child.isalive():
        if pid is not None:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)
        child.terminate(force=True)
        if pid is not None:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)
    child.close()


def _submit(child: pexpect.spawn, log: _Log, text: str) -> None:
    """Type `text` and press Enter as a separate key press, so a TUI does not take it as a paste."""
    log.sent(text)
    child.send(text)
    time.sleep(_SUBMIT_DELAY_SECONDS)
    child.send("\r")


def _watch(
    child: pexpect.spawn, log: _Log, setup: LoopSetup, waiting: tuple[str, ...], deadline: float
) -> str:
    """Read until the waiting items close ("closed") or return why the session failed.

    Items are re-checked about once a second whatever the agent prints, so a spinner that
    never goes quiet does not hide that the work is done. Once they are closed the agent gets
    one quiet period to finish its last words.
    """
    quiet = setup.loop.agent_quiet_seconds
    closed_at: float | None = None
    last_check = 0.0
    last_output = time.monotonic()
    while time.monotonic() < deadline:
        try:
            log.received(child.read_nonblocking(_READ_SIZE, timeout=min(quiet, 0.5)))
            last_output = time.monotonic()
        except pexpect.TIMEOUT:
            log.flush()
        except pexpect.EOF:
            if _still_waiting(setup, waiting):
                return "The agent exited while items were still waiting"
            return "closed"
        now = time.monotonic()
        if closed_at is None and now - last_check >= _CHECK_INTERVAL_SECONDS:
            last_check = now
            if not _still_waiting(setup, waiting):
                closed_at = now
        if closed_at is not None and (now - last_output >= quiet or now - closed_at >= quiet):
            return "closed"
    return "The agent timed out with items still waiting"
