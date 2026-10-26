"""Loop mail feeder: trust gates, handled ids, poll enqueue (issue #503)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blumkin.loop.config import LoopConfigError, parse_loop_setup
from blumkin.loop.handled_messages import is_handled, mark_handled
from blumkin.loop.mail_poll import poll_mail_triggers
from blumkin.loop.mail_profiles import is_profile_paused, pause_profile, resume_profile
from blumkin.loop.mail_trust import (
    allowlisted_sender,
    authentication_results_pass,
    subject_matches,
)
from blumkin.loop.queue import peek_queue
from blumkin.loop.types import TriggerKind


def _auth_headers() -> list[dict[str, str]]:
    return [
        {
            "name": "Authentication-Results",
            "value": "mx.example; spf=pass; dkim=pass; dmarc=pass",
        }
    ]


def test_allowlist_and_subject_gates() -> None:
    assert allowlisted_sender("me@example.com", ("me@example.com",))
    assert allowlisted_sender("Me@Example.com", ("me@example.com",))
    assert not allowlisted_sender("evil@example.com", ("me@example.com",))
    assert subject_matches("Receipt [trip]", "[trip]")
    assert not subject_matches("hello", "[trip]")
    assert subject_matches("anything", None)


def test_authentication_results_require_spf_dkim_dmarc() -> None:
    assert authentication_results_pass(_auth_headers())
    assert not authentication_results_pass(
        [{"name": "Authentication-Results", "value": "spf=pass; dkim=fail; dmarc=pass"}]
    )
    assert not authentication_results_pass(None)
    assert not authentication_results_pass([])


def test_authentication_results_uses_first_header_only() -> None:
    # Provider prepends a failing result; a later spoofed pass must not win.
    headers = [
        {"name": "Authentication-Results", "value": "mx; spf=fail; dkim=fail; dmarc=fail"},
        {
            "name": "Authentication-Results",
            "value": "evil; spf=pass; dkim=pass; dmarc=pass",
        },
    ]
    assert not authentication_results_pass(headers)
    # First header alone passes.
    assert authentication_results_pass(
        [{"name": "Authentication-Results", "value": "mx; spf=pass; dkim=pass; dmarc=pass"}]
    )


def test_authentication_results_does_not_combine_across_headers() -> None:
    headers = [
        {"name": "Authentication-Results", "value": "mx; spf=pass"},
        {"name": "Authentication-Results", "value": "evil; dkim=pass; dmarc=pass"},
    ]
    assert not authentication_results_pass(headers)


def test_handled_messages_round_trip(tmp_path: Path) -> None:
    assert not is_handled(tmp_path, profile="work", message_id="m1")
    assert mark_handled(tmp_path, profile="work", message_id="m1") is True
    assert is_handled(tmp_path, profile="work", message_id="m1")
    assert mark_handled(tmp_path, profile="work", message_id="m1") is False
    assert not is_handled(tmp_path, profile="personal", message_id="m1")


def test_mail_profile_pause_resume(tmp_path: Path) -> None:
    assert pause_profile(tmp_path, "work") is True
    assert is_profile_paused(tmp_path, "work")
    assert resume_profile(tmp_path, "work") is True
    assert not is_profile_paused(tmp_path, "work")


def test_loop_trigger_requires_allowlist(tmp_path: Path) -> None:
    with pytest.raises(LoopConfigError, match="allowlist"):
        parse_loop_setup(
            tmp_path,
            {
                "loop": {
                    "triggers": {
                        "receipts": {
                            "task": "expense-report",
                            "instance": "trip-1",
                            "kind": "loop",
                        }
                    }
                }
            },
        )


def test_poll_enqueues_trusted_message(tmp_path: Path) -> None:
    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {
                    "receipts": {
                        "task": "expense-report",
                        "instance": "trip-1",
                        "kind": "loop",
                        "allowlist": ["me@example.com"],
                        "profiles": ["work"],
                        "subject_tag": "[trip]",
                    }
                }
            }
        },
    )
    assert setup.triggers["receipts"].kind is TriggerKind.LOOP

    async def inbox(*, top: int = 25, config: Any = None) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "msg-1",
                    "from_email": "me@example.com",
                    "subject": "Receipt [trip]",
                }
            ]
        }

    async def message(*, message_id: str, config: Any = None) -> dict[str, Any]:
        return {
            "message": {
                "id": message_id,
                "headers": _auth_headers(),
                "from_email": "me@example.com",
                "subject": "Receipt [trip]",
            }
        }

    async def download(**kwargs: Any) -> dict[str, Any]:
        return {"saved": []}

    result = poll_mail_triggers(
        setup,
        inbox=inbox,
        message=message,
        download=download,
        refresh=lambda _cfg: {"ok": True},
        load_config=lambda profile: type("Cfg", (), {"provider": "microsoft", "name": profile})(),
        list_profiles=lambda: [{"name": "work"}],
    )
    assert result.enqueued == 1
    assert peek_queue(tmp_path) == ["receipts"]
    assert is_handled(tmp_path, profile="work", message_id="msg-1")

    # Second poll must not re-enqueue.
    again = poll_mail_triggers(
        setup,
        inbox=inbox,
        message=message,
        download=download,
        refresh=lambda _cfg: {"ok": True},
        load_config=lambda profile: type("Cfg", (), {"provider": "microsoft", "name": profile})(),
        list_profiles=lambda: [{"name": "work"}],
    )
    assert again.enqueued == 0
    assert peek_queue(tmp_path) == ["receipts"]


def test_poll_ignores_untrusted_sender(tmp_path: Path) -> None:
    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {
                    "receipts": {
                        "task": "expense-report",
                        "instance": "trip-1",
                        "kind": "loop",
                        "allowlist": ["me@example.com"],
                        "profiles": ["work"],
                    }
                }
            }
        },
    )

    async def inbox(*, top: int = 25, config: Any = None) -> dict[str, Any]:
        return {"items": [{"id": "msg-2", "from_email": "evil@example.com", "subject": "hi"}]}

    async def message(*, message_id: str, config: Any = None) -> dict[str, Any]:
        return {"message": {"id": message_id, "headers": _auth_headers()}}

    result = poll_mail_triggers(
        setup,
        inbox=inbox,
        message=message,
        download=_empty_saved,
        refresh=lambda _cfg: {"ok": True},
        load_config=lambda profile: type("Cfg", (), {"provider": "microsoft"})(),
        list_profiles=lambda: [{"name": "work"}],
    )
    assert result.enqueued == 0
    assert peek_queue(tmp_path) == []


def test_poll_pauses_profile_on_refresh_failure(tmp_path: Path) -> None:
    setup = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {
                    "receipts": {
                        "task": "expense-report",
                        "instance": "trip-1",
                        "kind": "loop",
                        "allowlist": ["me@example.com"],
                        "profiles": ["work"],
                    }
                }
            }
        },
    )

    def boom(_cfg: Any) -> dict[str, Any]:
        raise RuntimeError("token expired")

    result = poll_mail_triggers(
        setup,
        refresh=boom,
        load_config=lambda profile: type("Cfg", (), {"provider": "microsoft"})(),
        list_profiles=lambda: [{"name": "work"}],
        inbox=_empty_inbox,
        message=_empty_message,
        download=_empty_saved,
    )
    assert result.enqueued == 0
    assert is_profile_paused(tmp_path, "work")
    assert "work" in result.paused_profiles


async def _empty_inbox(**kwargs: Any) -> dict[str, Any]:
    return {"items": []}


async def _empty_message(**kwargs: Any) -> dict[str, Any]:
    return {"message": {}}


async def _empty_saved(**kwargs: Any) -> dict[str, Any]:
    return {"saved": []}
