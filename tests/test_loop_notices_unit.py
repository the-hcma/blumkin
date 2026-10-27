"""Loop reply-channel notices (issue #505)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blumkin.loop.config import parse_loop_setup
from blumkin.loop.handlers import HandlerResult, register_handler
from blumkin.loop.notices import (
    format_profile_paused,
    format_run_notice,
    is_loop_notice_subject,
    notice_recipient,
    notify_run,
)
from blumkin.loop.runner import run_trigger
from blumkin.loop.types import RunOutcome, RunRecord, TriggerKind, TriggerSpec


def test_format_run_notice_needs_user_has_action_line() -> None:
    record = RunRecord(
        at="2026-01-01T00:00:00Z",
        instance="default",
        outcome=RunOutcome.NEEDS_USER,
        reason="browser unsigned-in; total $12.34",
        run_id="abc",
        task="expense-report",
        trigger="expense-mail",
        trigger_kind=TriggerKind.LOOP,
    )
    subject, body = format_run_notice(record)
    assert subject == "blumkin loop: expense-mail needs_user"
    assert "outcome: needs_user" in body
    assert "reason: browser unsigned-in; total [amount]" in body
    assert "Action needed" in body
    assert "—" not in body
    assert "$" not in body


def test_is_loop_notice_subject() -> None:
    assert is_loop_notice_subject("blumkin loop: receipts done")
    assert is_loop_notice_subject("Blumkin Loop: x")
    assert not is_loop_notice_subject("receipts for trip")
    assert not is_loop_notice_subject(None)


def test_format_profile_paused() -> None:
    subject, body = format_profile_paused(profile="work", reason="renewal_failed")
    assert "work" in subject
    assert "renewal_failed" in body
    assert "loop init --profile work" in body


def test_notice_recipient_prefers_allowlist() -> None:
    trigger = TriggerSpec(
        allowlist=("ops@example.com", "other@example.com"),
        instance="default",
        kind=TriggerKind.LOOP,
        name="t",
        task="probe",
    )
    assert notice_recipient(trigger, fallback="fallback@example.com") == "ops@example.com"
    bare = TriggerSpec(instance="default", kind=TriggerKind.CRON, name="c", task="probe")
    assert notice_recipient(bare, fallback="fallback@example.com") == "fallback@example.com"
    assert notice_recipient(bare) is None


def test_notify_run_drafts_and_sends() -> None:
    trigger = TriggerSpec(
        allowlist=("ops@example.com",),
        instance="default",
        kind=TriggerKind.LOOP,
        name="t",
        task="probe",
    )
    record = RunRecord(
        at="2026-01-01T00:00:00Z",
        instance="default",
        outcome=RunOutcome.DONE,
        reason="",
        run_id="rid",
        task="probe",
        trigger="t",
        trigger_kind=TriggerKind.LOOP,
    )
    sent: dict[str, Any] = {}

    async def draft(**kwargs: Any) -> dict[str, Any]:
        sent["draft"] = kwargs
        return {"id": "draft-1"}

    async def send(*, draft_id: str) -> dict[str, Any]:
        sent["send"] = draft_id
        return {"sent": draft_id}

    assert notify_run(record, trigger, draft=draft, send=send) is True
    assert sent["draft"]["to"] == ["ops@example.com"]
    assert sent["draft"]["subject"] == "blumkin loop: t done"
    assert sent["send"] == "draft-1"


def test_runner_sends_notice_after_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "notice_to": "ops@example.com",
                "triggers": {
                    "probe-trigger": {
                        "task": "probe",
                        "instance": "default",
                        "kind": "manual",
                    }
                },
            }
        },
    )
    handlers: dict = {}
    sent: list[str] = []

    def needs_user(trigger: TriggerSpec, **_: object) -> HandlerResult:
        return HandlerResult(outcome=RunOutcome.NEEDS_USER, reason="site drift")

    register_handler(handlers, "probe", needs_user)

    async def draft(**kwargs: Any) -> dict[str, Any]:
        sent.append(str(kwargs.get("subject")))
        return {"id": "d1"}

    async def send(*, draft_id: str) -> dict[str, Any]:
        return {"sent": draft_id}

    import blumkin.loop.notices as notices_mod
    import blumkin.loop.runner as runner_mod

    def capturing(record: RunRecord, trigger: TriggerSpec, **kwargs: Any) -> bool:
        return notices_mod.notify_run(record, trigger, draft=draft, send=send, **kwargs)

    monkeypatch.setattr(runner_mod, "notify_run", capturing)
    result = run_trigger(setup, "probe-trigger", handlers=handlers)
    assert result.record is not None
    assert result.record.outcome is RunOutcome.NEEDS_USER
    assert sent == ["blumkin loop: probe-trigger needs_user"]


def test_parse_notice_to(tmp_path: Path) -> None:
    setup = parse_loop_setup(tmp_path, {"loop": {"notice_to": "ops@example.com"}})
    assert setup.loop.notice_to == "ops@example.com"
