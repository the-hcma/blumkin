"""Run the operator's agent in a pty so it works through the loop's waiting items (issue #538).

blumkin calls no model: the configured agent command is started, told to run the blumkin loop,
and watched until the items that were waiting are closed (finished or handed back). Everything
sent to the agent and everything it prints goes to a private, timestamped log.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pexpect

from blumkin.loop.agent_task import pending_items
from blumkin.loop.config import LoopSetup
from blumkin.loop.kill_switch import is_engaged

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_EXIT_GRACE_SECONDS = 5.0
_READ_SIZE = 4096
_READY_LIMIT_SECONDS = 60.0


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
    """The session log: one timestamped line per prompt sent and per chunk the agent printed."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._handle = os.fdopen(fd, "w", encoding="utf-8")

    @classmethod
    def create(cls, config_dir: Path) -> _Log:
        directory = config_dir / "loop" / "agent-logs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{stamp}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return cls(path, fd)

    def close(self) -> None:
        self._handle.close()

    def note(self, marker: str, text: str) -> None:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        for line in text.splitlines() or [""]:
            self._handle.write(f"{stamp} {marker} {line}\n")
        self._handle.flush()

    def sent(self, text: str) -> None:
        self.note(">>", text)

    def received(self, text: str) -> None:
        cleaned = _ANSI.sub("", text).replace("\r", "")
        if cleaned.strip():
            self.note("<<", cleaned.rstrip("\n"))


def _drive(
    setup: LoopSetup, command: tuple[str, ...], waiting: tuple[str, ...], log: _Log
) -> str | None:
    """Run the conversation; returns why it failed, or None when every waiting item closed."""
    settings = setup.loop
    deadline = time.monotonic() + settings.agent_timeout_seconds
    try:
        child = pexpect.spawn(
            command[0], list(command[1:]), encoding="utf-8", echo=False, dimensions=(50, 200)
        )
    except pexpect.ExceptionPexpect as exc:
        return f"Expected to start {command[0]}, got {exc}"
    try:
        ready = _read_until_quiet(
            child, log, settings.agent_quiet_seconds, _ready_deadline(deadline)
        )
        if ready == "eof":
            return "The agent exited before the prompt was sent"
        log.sent(settings.agent_prompt)
        child.sendline(settings.agent_prompt)
        outcome = _watch(child, log, setup, waiting, deadline)
        if outcome != "closed":
            return outcome
        log.sent(settings.agent_exit_input)
        child.sendline(settings.agent_exit_input)
        _read_until_eof(child, log, _EXIT_GRACE_SECONDS)
        return None
    finally:
        if child.isalive():
            child.terminate(force=True)
        child.close()


def _ready_deadline(deadline: float) -> float:
    return min(deadline, time.monotonic() + _READY_LIMIT_SECONDS)


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


def _watch(
    child: pexpect.spawn, log: _Log, setup: LoopSetup, waiting: tuple[str, ...], deadline: float
) -> str:
    """Read until the waiting items close ("closed") or return why the session failed."""
    quiet = setup.loop.agent_quiet_seconds
    while time.monotonic() < deadline:
        state = _read_until_quiet(child, log, quiet, deadline)
        if state == "eof":
            if _still_waiting(setup, waiting):
                return "The agent exited while items were still waiting"
            return "closed"
        if state == "quiet" and not _still_waiting(setup, waiting):
            return "closed"
    return "The agent timed out with items still waiting"


def _still_waiting(setup: LoopSetup, waiting: tuple[str, ...]) -> bool:
    open_ids = {item.id for item in pending_items(setup.config_dir)}
    return any(item in open_ids for item in waiting)
