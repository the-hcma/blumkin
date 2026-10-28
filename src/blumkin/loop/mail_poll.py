"""Poll mailbox profiles and enqueue matching loop triggers (issue #503).

Trust gates (allowlist + Authentication-Results) run before enqueue. Handled
message ids are recorded so restart/re-poll never repeats a run. Attachment
intake is best-effort after enqueue; failures are counted, not fatal to the tick.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from blumkin.loop.config import LoopSetup
from blumkin.loop.handled_messages import is_handled, mark_handled
from blumkin.loop.mail_intake import save_attachments
from blumkin.loop.mail_profiles import is_profile_paused, list_paused_profiles, pause_profile
from blumkin.loop.mail_sync import (
    clear_sync_state,
    load_sync_state,
    save_sync_state,
)
from blumkin.loop.mail_trust import (
    allowlisted_sender,
    authentication_results_pass,
    subject_matches,
)
from blumkin.loop.notices import (
    is_loop_notice_subject,
    notice_recipient,
    notify_profile_paused_async,
)
from blumkin.loop.queue import QueueEntry, enqueue
from blumkin.loop.state import list_paused
from blumkin.loop.types import TriggerKind, TriggerSpec
from blumkin.mail_sync import MailSyncResetError, MailSyncUnsupportedError

DownloadFn = Callable[..., Awaitable[dict[str, Any]]]
InboxFn = Callable[..., Awaitable[dict[str, Any]]]
MessageFn = Callable[..., Awaitable[dict[str, Any]]]
RefreshFn = Callable[[Any], dict[str, Any]]
SyncFn = Callable[..., Awaitable[dict[str, Any]]]
LoadConfigFn = Callable[..., Any]
ListProfilesFn = Callable[[], list[dict[str, Any]]]

_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class PollResult:
    enqueued: int
    ignored: int
    intake_failed: int
    paused_profiles: tuple[str, ...]
    scanned: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "enqueued": self.enqueued,
            "ignored": self.ignored,
            "intake_failed": self.intake_failed,
            "paused_profiles": list(self.paused_profiles),
            "scanned": self.scanned,
        }


def poll_mail_triggers(
    setup: LoopSetup,
    *,
    top: int = 25,
    inbox: InboxFn | None = None,
    message: MessageFn | None = None,
    download: DownloadFn | None = None,
    refresh: RefreshFn | None = None,
    load_config: LoadConfigFn | None = None,
    list_profiles: ListProfilesFn | None = None,
    sync: SyncFn | None = None,
) -> PollResult:
    """Scan mailboxes for loop triggers; enqueue trusted matches. Sync entry."""
    return asyncio.run(
        _poll_async(
            setup,
            top=top,
            inbox=inbox,
            message=message,
            download=download,
            refresh=refresh,
            load_config=load_config,
            list_profiles=list_profiles,
            sync=sync,
        )
    )


async def _poll_async(
    setup: LoopSetup,
    *,
    top: int,
    inbox: InboxFn | None,
    message: MessageFn | None,
    download: DownloadFn | None,
    refresh: RefreshFn | None,
    load_config: LoadConfigFn | None,
    list_profiles: ListProfilesFn | None,
    sync: SyncFn | None = None,
) -> PollResult:
    triggers = [
        spec
        for name, spec in sorted(setup.triggers.items())
        if spec.kind is TriggerKind.LOOP and name not in list_paused(setup.config_dir)
    ]
    if not triggers:
        return PollResult(
            enqueued=0,
            ignored=0,
            intake_failed=0,
            paused_profiles=tuple(sorted(list_paused_profiles(setup.config_dir))),
            scanned=0,
        )

    load_cfg = load_config or _default_load_config
    list_prof = list_profiles or _default_list_profiles
    refresh_fn = refresh or _default_refresh
    inbox_fn = inbox or _default_inbox
    message_fn = message or _default_message
    download_fn = download or _default_download
    sync_fn = sync or _default_sync

    profiles = _resolve_profiles(triggers, list_prof)
    enqueued = 0
    ignored = 0
    intake_failed = 0
    scanned = 0
    log = logging.getLogger("blumkin.loop")

    for profile_name in profiles:
        if is_profile_paused(setup.config_dir, profile_name):
            continue
        try:
            cfg = load_cfg(profile=profile_name)
            refresh_fn(cfg)
        except Exception as exc:  # noqa: BLE001 — classify below
            from blumkin.auth import AuthTransientError

            if isinstance(exc, AuthTransientError):
                # Brief outage: skip this tick; do not pause the profile.
                continue
            newly = pause_profile(setup.config_dir, profile_name, reason="renewal_failed")
            log.warning(
                "mail profile %s paused after renewal failure (%s)",
                profile_name,
                type(exc).__name__,
            )
            if newly:
                to = _pause_notice_to(triggers, setup.loop.notice_to)
                if to:
                    # Send from the default profile - the paused mailbox cannot draft.
                    await notify_profile_paused_async(
                        profile=profile_name,
                        reason="renewal_failed",
                        to=to,
                    )
            continue

        applicable = [
            spec for spec in triggers if not spec.profiles or profile_name in spec.profiles
        ]
        if not applicable:
            continue
        provider = _provider_name(cfg)
        scope = _scope(applicable, cfg)
        started = datetime.now(UTC)
        # Look back a few minutes: Graph filters on its own received time, not this clock.
        since = started - _CLOCK_SKEW
        state = load_sync_state(setup.config_dir, profile_name, provider=provider, scope=scope)
        batch_items: list[Mapping[str, Any]] = []
        token: str | None = None
        scan = state is None
        if state is not None:
            try:
                batch = await sync_fn(state=state.token, since=since, top=top, config=cfg)
                token, batch_items = _batch(batch)
            except MailSyncResetError:
                # The saved token is no longer valid: start over with a fresh one and one scan.
                log.info("mail sync token for profile %s expired; rescanning", profile_name)
                clear_sync_state(setup.config_dir, profile_name)
                scan = True
            except Exception as exc:  # noqa: BLE001 - keep the token and try again next tick
                log.warning(
                    "mail sync for profile %s failed (%s); keeping the token and retrying",
                    profile_name,
                    type(exc).__name__,
                )
                ignored += 1
                continue
        if scan:
            # Take the token before scanning, so mail that arrives during the scan is in the
            # next incremental read instead of in a gap between the two.
            try:
                batch = await sync_fn(state=None, since=since, top=top, config=cfg)
                token, batch_items = _batch(batch)
            except Exception as exc:  # noqa: BLE001 - no incremental sync: the scan alone works
                log.warning(
                    "mail sync for profile %s unavailable (%s); scanning the newest messages",
                    profile_name,
                    type(exc).__name__,
                )
                token, batch_items = None, []
            try:
                listing = await inbox_fn(top=top, config=cfg)
            except Exception:  # noqa: BLE001 - count and move on
                ignored += 1
                continue
            found = listing.get("items") if isinstance(listing, Mapping) else None
            batch_items = _merge(batch_items, found if isinstance(found, list) else [])
        retry = False
        for spec in applicable:
            for item in batch_items:
                scanned += 1
                outcome = await _consider_message(
                    setup,
                    spec,
                    profile_name=profile_name,
                    item=item,
                    cfg=cfg,
                    message_fn=message_fn,
                    download_fn=download_fn,
                )
                if outcome == "enqueued":
                    enqueued += 1
                elif outcome == "intake_failed":
                    enqueued += 1
                    intake_failed += 1
                else:
                    ignored += 1
                    retry = retry or outcome == "retry"
        # The token moves forward only once every message of the batch was dealt with, so a
        # transient failure is read again on the next poll (handled ids make that safe).
        if token and not retry:
            save_sync_state(
                setup.config_dir,
                profile_name,
                provider=provider,
                token=token,
                mode="scan" if scan else "delta",
                scope=scope,
            )

    return PollResult(
        enqueued=enqueued,
        ignored=ignored,
        intake_failed=intake_failed,
        paused_profiles=tuple(sorted(list_paused_profiles(setup.config_dir))),
        scanned=scanned,
    )


async def _consider_message(
    setup: LoopSetup,
    spec: TriggerSpec,
    *,
    profile_name: str,
    item: Mapping[str, Any],
    cfg: Any,
    message_fn: MessageFn,
    download_fn: DownloadFn,
) -> str:
    message_id = str(item.get("id") or "").strip()
    if not message_id:
        return "ignored"
    if is_handled(setup.config_dir, profile=profile_name, message_id=message_id):
        return "ignored"
    sender = item.get("from_email") or item.get("from")
    if isinstance(sender, dict):
        sender = sender.get("email") or sender.get("address")
    if not allowlisted_sender(str(sender) if sender else None, spec.allowlist):
        return "ignored"
    subject = item.get("subject") if isinstance(item.get("subject"), str) else None
    # Outbound loop notices land in the same inbox; never re-enqueue them.
    if is_loop_notice_subject(subject):
        mark_handled(setup.config_dir, profile=profile_name, message_id=message_id)
        return "ignored"
    if not subject_matches(subject, spec.subject_tag):
        return "ignored"
    try:
        detail_payload = await message_fn(message_id=message_id, config=cfg)
    except Exception:  # noqa: BLE001 - transient: the batch is read again next poll
        return "retry"
    detail = detail_payload.get("message") if isinstance(detail_payload, Mapping) else None
    if not isinstance(detail, Mapping):
        return "retry"
    headers = detail.get("internet_message_headers") or detail.get("headers")
    if not authentication_results_pass(headers if isinstance(headers, list) else None):
        # Record so we do not re-fetch this message on every poll tick.
        mark_handled(setup.config_dir, profile=profile_name, message_id=message_id)
        return "ignored"
    intake_ok = True
    try:
        await save_attachments(
            config_dir=setup.config_dir,
            trigger=spec.name,
            message_id=message_id,
            download=download_fn,
            attachment_types=spec.attachment_types,
            config=cfg,
        )
    except Exception:  # noqa: BLE001 — intake is best-effort; trigger still runs
        intake_ok = False
    # Enqueue, then mark handled so a queue write failure cannot drop the message.
    enqueue(
        setup.config_dir,
        QueueEntry(
            intake_ok=intake_ok,
            message_id=message_id,
            profile=profile_name,
            subject=subject,
            trigger=spec.name,
        ),
    )
    mark_handled(setup.config_dir, profile=profile_name, message_id=message_id)
    return "enqueued" if intake_ok else "intake_failed"


def _batch(batch: Mapping[str, Any]) -> tuple[str | None, list[Mapping[str, Any]]]:
    """The next sync token and the message summaries of one incremental read."""
    if not isinstance(batch, Mapping):
        raise MailSyncUnsupportedError("Expected a mapping from the sync call, got something else")
    token = batch.get("state")
    items = batch.get("items")
    found = [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
    return (token if isinstance(token, str) and token else None), found


def _default_download(**kwargs: Any) -> Awaitable[dict[str, Any]]:
    from blumkin.providers import get_provider

    cfg = kwargs.get("config")
    provider = get_provider(cfg)
    return provider.mail_attachments_download(
        message_id=kwargs["message_id"],
        out=kwargs["out"],
        download_all=True,
    )


def _default_inbox(**kwargs: Any) -> Awaitable[dict[str, Any]]:
    from blumkin.providers import get_provider

    cfg = kwargs.get("config")
    return get_provider(cfg).mail_inbox(top=int(kwargs.get("top") or 25))


def _default_list_profiles() -> list[dict[str, Any]]:
    from blumkin.config import list_profiles

    return list_profiles()


def _default_load_config(*, profile: str) -> Any:
    from blumkin.config import load_config

    return load_config(profile=profile)


def _default_message(**kwargs: Any) -> Awaitable[dict[str, Any]]:
    from blumkin.providers import get_provider

    cfg = kwargs.get("config")
    return get_provider(cfg).mail_get(message_id=kwargs["message_id"])


def _default_refresh(cfg: Any) -> dict[str, Any]:
    provider = getattr(cfg, "provider", None)
    if provider == "google":
        from blumkin.providers import google_auth

        return google_auth.refresh_silent(cfg)
    from blumkin import auth

    return auth.refresh_silent(cfg)


def _default_sync(**kwargs: Any) -> Awaitable[dict[str, Any]]:
    from blumkin.providers import get_provider

    provider = get_provider(kwargs.get("config"))
    method = getattr(provider, "mail_new", None)
    if method is None:
        raise MailSyncUnsupportedError("Expected a provider with mail_new, got none")
    return method(state=kwargs["state"], since=kwargs["since"], top=int(kwargs.get("top") or 50))


def _merge(first: list[Mapping[str, Any]], second: list[Any]) -> list[Mapping[str, Any]]:
    """`first` then `second`, each message once (by id)."""
    seen: set[str] = set()
    merged: list[Mapping[str, Any]] = []
    for item in [*first, *second]:
        if not isinstance(item, Mapping):
            continue
        message_id = str(item.get("id") or "")
        if message_id in seen:
            continue
        seen.add(message_id)
        merged.append(item)
    return merged


def _pause_notice_to(triggers: list[TriggerSpec], fallback: str | None) -> str | None:
    for spec in triggers:
        to = notice_recipient(spec, fallback=None)
        if to:
            return to
    if fallback and fallback.strip():
        return fallback.strip()
    return None


def _provider_name(cfg: Any) -> str:
    provider = getattr(cfg, "provider", "")
    return str(getattr(provider, "value", provider)) or "unknown"


def _resolve_profiles(triggers: list[TriggerSpec], list_profiles: ListProfilesFn) -> list[str]:
    # Any trigger with an empty profiles list means "all configured profiles".
    if any(not spec.profiles for spec in triggers):
        named: set[str] = set()
    else:
        named = set()
        for spec in triggers:
            named.update(spec.profiles)
        if named:
            return sorted(named)
    rows = list_profiles()
    names: list[str] = []
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("name"), str) and row["name"].strip():
            names.append(row["name"].strip())
        elif isinstance(row, Mapping) and isinstance(row.get("profile"), str):
            names.append(row["profile"].strip())
    # Also include explicitly named profiles even if list_profiles is incomplete.
    for spec in triggers:
        named.update(spec.profiles)
    return sorted(set(names) | named)


def _scope(applicable: list[TriggerSpec], cfg: Any) -> str:
    """Fingerprint of what a sync token is taken for: the profile's triggers and account."""
    payload = {
        "email": str(getattr(cfg, "email", "") or ""),
        "triggers": [spec.as_dict() for spec in applicable],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
