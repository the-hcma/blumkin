"""CLI-facing operations for `blumkin loop` (issue #501 / #502)."""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from typing import Any

from blumkin.loop.config import LoopSetup
from blumkin.loop.cron import due_fires, mark_due_consumed
from blumkin.loop.handlers import HandlerMap
from blumkin.loop.journal import list_runs, recent_by_trigger
from blumkin.loop.kill_switch import is_engaged, kill_switch_path
from blumkin.loop.queue import drain_queue, requeue_front
from blumkin.loop.runner import RunResult, run_trigger
from blumkin.loop.state import is_paused, list_paused, pause_trigger, resume_trigger
from blumkin.loop.types import TriggerSpec


@dataclass(frozen=True, slots=True)
class TriggerStatus:
    last_run: dict[str, Any] | None
    name: str
    paused: bool
    trigger: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_run": self.last_run,
            "name": self.name,
            "paused": self.paused,
            "trigger": self.trigger,
        }


def list_triggers(setup: LoopSetup) -> list[dict[str, Any]]:
    """Configured triggers with pause flag, sorted by name."""
    paused = list_paused(setup.config_dir)
    rows: list[dict[str, Any]] = []
    for name in sorted(setup.triggers):
        spec = setup.triggers[name]
        rows.append({**spec.as_dict(), "paused": name in paused})
    return rows


def pause(setup: LoopSetup, name: str) -> dict[str, Any]:
    setup.trigger(name)  # raise if unknown
    changed = pause_trigger(setup.config_dir, name)
    return {"name": name, "paused": True, "changed": changed}


def resume(setup: LoopSetup, name: str) -> dict[str, Any]:
    from datetime import UTC, datetime

    setup.trigger(name)
    changed = resume_trigger(setup.config_dir, name)
    if changed:
        # Missed dues while paused are not caught up unless a later command asks.
        mark_due_consumed(setup.config_dir, name, datetime.now(UTC))
    return {"name": name, "paused": False, "changed": changed}


def run_once(
    setup: LoopSetup,
    name: str,
    *,
    handlers: HandlerMap | None = None,
    honor_pause: bool = True,
) -> dict[str, Any]:
    """Execute one trigger now (manual dry run / enqueue-equivalent)."""
    result = run_trigger(setup, name, handlers=handlers, honor_pause=honor_pause)
    return result.as_dict()


def status(setup: LoopSetup) -> dict[str, Any]:
    """Queue overview: kill switch, paused triggers, last run per trigger, recent journal."""
    paused = sorted(list_paused(setup.config_dir))
    latest = recent_by_trigger(setup.config_dir)
    triggers: list[TriggerStatus] = []
    for name in sorted(setup.triggers):
        spec: TriggerSpec = setup.triggers[name]
        last = latest.get(name)
        triggers.append(
            TriggerStatus(
                last_run=last.as_dict() if last else None,
                name=name,
                paused=name in paused or is_paused(setup.config_dir, name),
                trigger=spec.as_dict(),
            )
        )
    recent = [r.as_dict() for r in list_runs(setup.config_dir, limit=20)]
    return {
        "kill_switch": is_engaged(setup.config_dir, setup.loop),
        "kill_switch_path": str(kill_switch_path(setup.config_dir, setup.loop)),
        "paused": paused,
        "recent_runs": recent,
        "triggers": [t.as_dict() for t in triggers],
    }


def run_foreground(
    setup: LoopSetup,
    *,
    handlers: HandlerMap | None = None,
    poll_seconds: float = 5.0,
    sleep: Any = time.sleep,
    should_stop: Any | None = None,
) -> dict[str, Any]:
    """Foreground runner process for the systemd user service.

    Drains the work queue and due cron triggers (catch-up at most once per due
    time), dispatching each through `run_trigger`. Mail feeders land in #503.
    `run-once` still runs inline for a manual dry run. Exits on SIGTERM/SIGINT
    or `should_stop`.
    """
    stop = {"flag": False}

    def _handle(_signum: int, _frame: Any) -> None:
        stop["flag"] = True

    previous_term = signal.signal(signal.SIGTERM, _handle)
    previous_int = signal.signal(signal.SIGINT, _handle)
    ticks = 0
    runs = 0
    try:
        while not stop["flag"] and not (should_stop is not None and should_stop()):
            ticks += 1
            if is_engaged(setup.config_dir, setup.loop):
                sleep(poll_seconds)
                continue
            pending = drain_queue(setup.config_dir)
            consumed = 0
            try:
                for name in pending:
                    if stop["flag"] or is_engaged(setup.config_dir, setup.loop):
                        break
                    consumed += 1
                    result = run_trigger(setup, name, handlers=handlers, honor_pause=True)
                    if not result.skipped:
                        runs += 1
            finally:
                if consumed < len(pending):
                    requeue_front(setup.config_dir, pending[consumed:])
            paused = list_paused(setup.config_dir)
            for fire in due_fires(setup.triggers, config_dir=setup.config_dir, paused=paused):
                if stop["flag"] or is_engaged(setup.config_dir, setup.loop):
                    break
                result = run_trigger(setup, fire.trigger.name, handlers=handlers, honor_pause=True)
                if not result.skipped:
                    mark_due_consumed(setup.config_dir, fire.trigger.name, fire.due_at)
                    runs += 1
            sleep(poll_seconds)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return {"ok": True, "ticks": ticks, "runs": runs, "stopped": True}


__all__ = [
    "RunResult",
    "TriggerStatus",
    "list_triggers",
    "pause",
    "resume",
    "run_foreground",
    "run_once",
    "status",
]
