"""Loop runner: journal, kill switch, pause, budget and retries (issue #501)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blumkin.loop.config import LoopConfig, LoopSetup, parse_loop_setup
from blumkin.loop.handlers import HandlerResult, register_handler
from blumkin.loop.journal import list_runs
from blumkin.loop.runner import run_trigger
from blumkin.loop.state import pause_trigger
from blumkin.loop.types import RunOutcome, TriggerKind, TriggerSpec


def _setup(tmp_path: Path, **loop_extra: object) -> LoopSetup:
    table: dict = {
        "triggers": {"probe-trigger": {"task": "probe", "instance": "default", "kind": "manual"}}
    }
    table.update(loop_extra)
    return parse_loop_setup(tmp_path, {"loop": table})


def test_foreground_drains_queue(tmp_path: Path) -> None:
    from blumkin.loop.commands import run_foreground
    from blumkin.loop.queue import QueueEntry, enqueue

    setup = _setup(tmp_path)
    enqueue(tmp_path, QueueEntry(trigger="probe-trigger"))
    stop_after = {"n": 0}

    def should_stop() -> bool:
        stop_after["n"] += 1
        return stop_after["n"] > 1

    payload = run_foreground(setup, poll_seconds=0, sleep=lambda _s: None, should_stop=should_stop)
    assert payload["runs"] == 1
    assert list_runs(tmp_path)[0].outcome is RunOutcome.DONE


def test_foreground_requeues_undispatched_on_kill(tmp_path: Path) -> None:
    from blumkin.loop.commands import run_foreground
    from blumkin.loop.queue import QueueEntry, enqueue, peek_queue

    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {
                    "a": {"task": "probe", "instance": "default", "kind": "manual"},
                    "b": {"task": "probe", "instance": "default", "kind": "manual"},
                }
            }
        },
    )
    enqueue(tmp_path, QueueEntry(trigger="a"))
    enqueue(tmp_path, QueueEntry(trigger="b"))
    handlers: dict = {}
    calls = {"n": 0}

    def engage_kill(trigger: TriggerSpec, **_: object) -> HandlerResult:
        calls["n"] += 1
        kill = tmp_path / "loop" / "KILL"
        kill.parent.mkdir(mode=0o700, exist_ok=True)
        kill.write_text("stop\n", encoding="utf-8")
        return HandlerResult(outcome=RunOutcome.DONE, reason="")

    register_handler(handlers, "probe", engage_kill)
    stop_after = {"n": 0}

    def should_stop() -> bool:
        stop_after["n"] += 1
        return stop_after["n"] > 1

    payload = run_foreground(
        setup,
        handlers=handlers,
        poll_seconds=0,
        sleep=lambda _s: None,
        should_stop=should_stop,
    )
    assert payload["runs"] == 1
    assert calls["n"] == 1
    assert [e.trigger for e in peek_queue(tmp_path)] == ["b"]


def test_foreground_requeues_tail_when_dispatch_raises(tmp_path: Path) -> None:
    from blumkin.loop.commands import run_foreground
    from blumkin.loop.config import LoopTriggerNotFoundError
    from blumkin.loop.queue import QueueEntry, enqueue, peek_queue

    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {
                    "a": {"task": "probe", "instance": "default", "kind": "manual"},
                    "b": {"task": "probe", "instance": "other", "kind": "manual"},
                }
            }
        },
    )
    enqueue(tmp_path, QueueEntry(trigger="missing"))
    enqueue(tmp_path, QueueEntry(trigger="b"))
    stop_after = {"n": 0}

    def should_stop() -> bool:
        stop_after["n"] += 1
        return stop_after["n"] > 1

    with pytest.raises(LoopTriggerNotFoundError):
        run_foreground(setup, poll_seconds=0, sleep=lambda _s: None, should_stop=should_stop)
    assert [e.trigger for e in peek_queue(tmp_path)] == ["b"]


def test_list_runs_limit_zero_is_empty(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    run_trigger(setup, "probe-trigger")
    assert list_runs(tmp_path, limit=0) == []
    assert len(list_runs(tmp_path, limit=1)) == 1


def test_run_once_probe_journals_done(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    result = run_trigger(setup, "probe-trigger")
    assert result.skipped is False
    assert result.record is not None
    assert result.record.outcome is RunOutcome.DONE
    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].trigger == "probe-trigger"
    assert runs[0].outcome is RunOutcome.DONE


def test_kill_switch_skips_without_journaling(tmp_path: Path) -> None:
    setup = _setup(tmp_path, kill_switch=True)
    result = run_trigger(setup, "probe-trigger")
    assert result.skipped is True
    assert result.skip_reason == "kill_switch"
    assert list_runs(tmp_path) == []


def test_kill_file_skips(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    kill = tmp_path / "loop" / "KILL"
    kill.parent.mkdir(mode=0o700)
    kill.write_text("stop\n", encoding="utf-8")
    result = run_trigger(setup, "probe-trigger")
    assert result.skipped is True
    assert result.skip_reason == "kill_switch"


def test_paused_trigger_skips(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    pause_trigger(tmp_path, "probe-trigger")
    result = run_trigger(setup, "probe-trigger")
    assert result.skipped is True
    assert result.skip_reason == "paused"


def test_unknown_task_becomes_needs_user(tmp_path: Path) -> None:
    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {"x": {"task": "no-such-task", "instance": "trip-1", "kind": "manual"}}
            }
        },
    )
    result = run_trigger(setup, "x")
    assert result.record is not None
    assert result.record.outcome is RunOutcome.NEEDS_USER
    assert "no handler" in result.record.reason


def test_failed_handler_retries_then_journals_failed(tmp_path: Path) -> None:
    setup = _setup(tmp_path, max_retries=2, retry_backoff_seconds=0)
    calls = {"n": 0}
    handlers: dict = {}

    def always_fail(trigger: TriggerSpec, **_: object) -> HandlerResult:
        calls["n"] += 1
        return HandlerResult(outcome=RunOutcome.FAILED, reason="boom")

    register_handler(handlers, "probe", always_fail)
    sleeps: list[float] = []
    result = run_trigger(setup, "probe-trigger", handlers=handlers, sleep=sleeps.append)
    assert calls["n"] == 3
    assert len(sleeps) == 2
    assert result.record is not None
    assert result.record.outcome is RunOutcome.FAILED
    assert result.record.reason == "boom"


def test_needs_user_does_not_retry(tmp_path: Path) -> None:
    setup = _setup(tmp_path, max_retries=3, retry_backoff_seconds=0)
    calls = {"n": 0}
    handlers: dict = {}

    def handler(trigger: TriggerSpec, **_: object) -> HandlerResult:
        calls["n"] += 1
        return HandlerResult(outcome=RunOutcome.NEEDS_USER, reason="ask")

    register_handler(handlers, "probe", handler)
    result = run_trigger(setup, "probe-trigger", handlers=handlers, sleep=lambda _s: None)
    assert calls["n"] == 1
    assert result.record is not None
    assert result.record.outcome is RunOutcome.NEEDS_USER


def test_handler_exception_journals_fixed_reason(tmp_path: Path) -> None:
    setup = _setup(tmp_path, max_retries=0)
    handlers: dict = {}

    def boom(trigger: TriggerSpec, **_: object) -> HandlerResult:
        raise RuntimeError("secret receipt amount 12.34")

    register_handler(handlers, "probe", boom)
    result = run_trigger(setup, "probe-trigger", handlers=handlers, sleep=lambda _s: None)
    assert result.record is not None
    assert result.record.outcome is RunOutcome.FAILED
    assert result.record.reason == "handler raised"
    assert "12.34" not in result.record.reason


def test_run_budget_exhausted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = LoopSetup(
        config_dir=tmp_path,
        loop=LoopConfig(run_budget_seconds=1.0, max_retries=5, retry_backoff_seconds=10.0),
        triggers={
            "t": TriggerSpec(
                instance="default",
                kind=TriggerKind.MANUAL,
                name="t",
                task="probe",
            )
        },
    )
    clock = {"t": 100.0}

    monkeypatch.setattr("blumkin.loop.runner.time.monotonic", lambda: clock["t"])

    handlers: dict = {}
    calls = {"n": 0}

    def fail_and_advance(trigger: TriggerSpec, **_: object) -> HandlerResult:
        calls["n"] += 1
        clock["t"] += 0.5
        return HandlerResult(outcome=RunOutcome.FAILED, reason="again")

    register_handler(handlers, "probe", fail_and_advance)

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    result = run_trigger(setup, "t", handlers=handlers, sleep=sleep)
    assert result.record is not None
    assert result.record.outcome is RunOutcome.FAILED
    assert result.record.reason == "run budget exhausted"
    assert calls["n"] == 1


def _resolver_setup(tmp_path: Path, resolver: str = "newest_open") -> LoopSetup:
    return parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {
                    "receipts": {
                        "task": "expense-report",
                        "kind": "loop",
                        "allowlist": ["me@example.com"],
                        "instance_resolver": resolver,
                        "subject_tag": "[expense]",
                    }
                }
            }
        },
    )


def test_resolved_instance_and_context_reach_the_handler(tmp_path: Path) -> None:
    from blumkin.browser.ledger import begin_instance
    from blumkin.loop.types import RunContext

    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    seen: list[tuple[TriggerSpec, RunContext | None]] = []

    def handler(trigger: TriggerSpec, *, context: RunContext | None = None) -> HandlerResult:
        seen.append((trigger, context))
        return HandlerResult(outcome=RunOutcome.DONE, reason="ok")

    handlers: dict = {}
    register_handler(handlers, "expense-report", handler)
    result = run_trigger(
        _resolver_setup(tmp_path),
        "receipts",
        context=RunContext(message_id="m1", profile="work", subject="[expense] taxi"),
        handlers=handlers,
    )
    assert result.record is not None
    assert result.record.outcome is RunOutcome.DONE
    assert result.record.instance == "trip-lisbon"
    trigger, context = seen[0]
    assert trigger.instance == "trip-lisbon"
    assert context is not None
    assert (context.config_dir, context.instance) == (tmp_path, "trip-lisbon")
    assert (context.message_id, context.profile) == ("m1", "work")


def test_unresolved_instance_is_needs_user_without_calling_the_handler(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(trigger: TriggerSpec, **_: object) -> HandlerResult:
        calls.append(trigger.instance)
        return HandlerResult(outcome=RunOutcome.DONE, reason="")

    handlers: dict = {}
    register_handler(handlers, "expense-report", handler)
    result = run_trigger(_resolver_setup(tmp_path), "receipts", handlers=handlers)
    assert calls == []
    assert result.record is not None
    assert result.record.outcome is RunOutcome.NEEDS_USER
    assert result.record.instance == "unresolved"
    assert result.record.reason.startswith("instance not resolved:")


def test_newest_open_with_several_open_calls_the_handler_on_the_latest(tmp_path: Path) -> None:
    from blumkin.browser.ledger import begin_instance

    begin_instance(tmp_path, "expense-report", "trip-z")
    begin_instance(tmp_path, "expense-report", "trip-a")
    seen: list[str] = []

    def handler(trigger: TriggerSpec, **_: object) -> HandlerResult:
        seen.append(trigger.instance)
        return HandlerResult(outcome=RunOutcome.DONE, reason="")

    handlers: dict = {}
    register_handler(handlers, "expense-report", handler)
    result = run_trigger(_resolver_setup(tmp_path), "receipts", handlers=handlers)
    assert seen == ["trip-a"]
    assert result.record is not None
    assert result.record.outcome is RunOutcome.DONE
    assert result.record.instance == "trip-a"


def test_subject_trip_picks_the_named_open_trip(tmp_path: Path) -> None:
    from blumkin.browser.ledger import begin_instance
    from blumkin.loop.types import RunContext

    begin_instance(tmp_path, "expense-report", "trip-a")
    begin_instance(tmp_path, "expense-report", "trip-b")
    seen: list[str] = []

    def handler(trigger: TriggerSpec, **_: object) -> HandlerResult:
        seen.append(trigger.instance)
        return HandlerResult(outcome=RunOutcome.DONE, reason="")

    handlers: dict = {}
    register_handler(handlers, "expense-report", handler)
    run_trigger(
        _resolver_setup(tmp_path, "subject_trip"),
        "receipts",
        context=RunContext(subject="[Expense] trip-b dinner"),
        handlers=handlers,
    )
    assert seen == ["trip-b"]


def test_foreground_passes_queued_mail_context_to_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import blumkin.loop.commands as commands_mod
    from blumkin.browser.ledger import begin_instance
    from blumkin.loop.commands import run_foreground
    from blumkin.loop.queue import QueueEntry, enqueue
    from blumkin.loop.types import RunContext

    # The mail poll would read the real mailbox config; the queue is what is under test.
    monkeypatch.setattr(commands_mod, "poll_mail_triggers", lambda _setup: None)
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    setup = _resolver_setup(tmp_path, "subject_trip")
    enqueue(
        tmp_path,
        QueueEntry(
            trigger="receipts",
            message_id="msg-1",
            profile="work",
            subject="[expense] trip-lisbon taxi",
        ),
    )
    seen: list[RunContext | None] = []

    def handler(trigger: TriggerSpec, *, context: RunContext | None = None) -> HandlerResult:
        seen.append(context)
        return HandlerResult(outcome=RunOutcome.DONE, reason="ok")

    handlers: dict = {}
    register_handler(handlers, "expense-report", handler)
    stop_after = {"n": 0}

    def should_stop() -> bool:
        stop_after["n"] += 1
        return stop_after["n"] > 1

    payload = run_foreground(
        setup,
        handlers=handlers,
        poll_seconds=0,
        sleep=lambda _s: None,
        should_stop=should_stop,
    )
    assert payload["runs"] == 1
    (context,) = seen
    assert context is not None
    assert (context.message_id, context.profile, context.instance) == (
        "msg-1",
        "work",
        "trip-lisbon",
    )
