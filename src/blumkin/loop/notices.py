"""Outbound notices for loop runs and feeder events (issue #505).

Short mail to an allowlisted address summarizing what was done or is waiting.
Bodies use hyphens (not em dashes), carry ids and outcomes only, and never
include receipt amounts. Failures to send are logged; they do not fail the run.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from blumkin.loop.types import RunOutcome, RunRecord, TriggerSpec

DraftFn = Callable[..., Awaitable[dict[str, Any]]]
SendFn = Callable[..., Awaitable[dict[str, Any]]]

NOTICE_SUBJECT_PREFIX = "blumkin loop:"
NOTICE_SEND_TIMEOUT_SECONDS = 30.0

_AMOUNT = re.compile(
    r"(?i)(?:[$€£¥]|usd|cad|eur)\s*\d[\d,]*(?:\.\d+)?"
    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"
    r"|\b\d+\.\d{2}\b"
)
_log = logging.getLogger("blumkin.loop")


def format_profile_paused(*, profile: str, reason: str) -> tuple[str, str]:
    subject = f"{NOTICE_SUBJECT_PREFIX} mailbox profile {profile} paused"
    body = (
        f"Mailbox profile {profile} was paused ({reason}).\n"
        f"Other profiles keep running. Re-run: blumkin loop init --profile {profile}\n"
    )
    return subject, body


def format_run_notice(record: RunRecord) -> tuple[str, str]:
    """Subject and body for a completed trigger run (ids and outcome only)."""
    outcome = record.outcome.value
    subject = f"{NOTICE_SUBJECT_PREFIX} {record.trigger} {outcome}"
    lines = [
        f"trigger: {record.trigger}",
        f"task: {record.task}",
        f"instance: {record.instance}",
        f"outcome: {outcome}",
        f"run_id: {record.run_id}",
    ]
    cleaned = sanitize_notice_reason(record.reason)
    if cleaned:
        lines.append(f"reason: {cleaned}")
    if record.outcome is RunOutcome.NEEDS_USER:
        lines.append("Action needed: check blumkin loop status, then resume or run-once.")
    body = "\n".join(lines) + "\n"
    return subject, body


def is_loop_notice_subject(subject: str | None) -> bool:
    """True when ``subject`` looks like an outbound loop notice (skip on re-poll)."""
    if not subject:
        return False
    return subject.strip().lower().startswith(NOTICE_SUBJECT_PREFIX)


def notice_recipient(trigger: TriggerSpec, *, fallback: str | None = None) -> str | None:
    """Prefer the first allowlisted address; otherwise an optional loop-wide fallback."""
    if trigger.allowlist:
        return trigger.allowlist[0]
    if fallback and fallback.strip():
        return fallback.strip()
    return None


def notice_send_profile(trigger: TriggerSpec) -> str | None:
    """Mailbox profile to send from when the trigger pins one; else the default profile."""
    if trigger.profiles:
        return trigger.profiles[0]
    return None


async def notify_profile_paused_async(
    *,
    profile: str,
    reason: str,
    to: str,
    draft: DraftFn | None = None,
    send: SendFn | None = None,
    load_config: Any | None = None,
) -> bool:
    """Send a pause notice from the default profile (not the failed mailbox)."""
    subject, body = format_profile_paused(profile=profile, reason=reason)
    try:
        return await _send_async(
            to=to,
            subject=subject,
            body=body,
            profile=None,
            draft=draft,
            send=send,
            load_config=load_config,
        )
    except Exception as exc:  # noqa: BLE001 - never fail the loop for a notice
        _log.warning("loop notice failed: %s", type(exc).__name__)
        return False


def notify_run(
    record: RunRecord,
    trigger: TriggerSpec,
    *,
    profile: str | None = None,
    fallback_to: str | None = None,
    draft: DraftFn | None = None,
    send: SendFn | None = None,
    load_config: Any | None = None,
) -> bool:
    """Send a run notice. Returns True when a message was sent."""
    to = notice_recipient(trigger, fallback=fallback_to)
    if not to:
        return False
    send_profile = profile if profile is not None else notice_send_profile(trigger)
    subject, body = format_run_notice(record)
    return _send(
        to=to,
        subject=subject,
        body=body,
        profile=send_profile,
        draft=draft,
        send=send,
        load_config=load_config,
    )


def sanitize_notice_reason(reason: str) -> str:
    """Drop currency-like tokens so amounts never leave the journal reason into mail."""
    text = reason.strip()
    if not text:
        return ""
    text = _AMOUNT.sub("[amount]", text)
    return text[:200]


def _send(
    *,
    to: str,
    subject: str,
    body: str,
    profile: str | None,
    draft: DraftFn | None,
    send: SendFn | None,
    load_config: Any | None,
) -> bool:
    try:
        return asyncio.run(
            _send_async(
                to=to,
                subject=subject,
                body=body,
                profile=profile,
                draft=draft,
                send=send,
                load_config=load_config,
            )
        )
    except Exception as exc:  # noqa: BLE001 - never fail the loop for a notice
        _log.warning("loop notice failed: %s", type(exc).__name__)
        return False


async def _send_async(
    *,
    to: str,
    subject: str,
    body: str,
    profile: str | None,
    draft: DraftFn | None,
    send: SendFn | None,
    load_config: Any | None,
) -> bool:
    from blumkin.config import load_config as default_load
    from blumkin.providers import get_provider

    load = load_config or default_load
    cfg = load(profile=profile) if profile else load()
    provider = get_provider(cfg)
    draft_fn = draft or provider.mail_draft
    send_fn = send or provider.mail_send_draft

    async def _draft_and_send() -> bool:
        created = await draft_fn(to=[to], subject=subject, body=body, body_type="text")
        draft_id = str(created.get("id") or created.get("draft_id") or "").strip()
        if not draft_id:
            raise ValueError("draft response missing id")
        await send_fn(draft_id=draft_id)
        return True

    return await asyncio.wait_for(_draft_and_send(), timeout=NOTICE_SEND_TIMEOUT_SECONDS)
