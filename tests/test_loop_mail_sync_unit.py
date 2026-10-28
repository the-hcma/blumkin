"""Incremental mail polling: per-profile sync tokens and the fallback scan (issue #525)."""

from __future__ import annotations

import json
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from blumkin.loop.commands import status
from blumkin.loop.config import LoopSetup, parse_loop_setup
from blumkin.loop.handled_messages import is_handled
from blumkin.loop.mail_poll import poll_mail_triggers
from blumkin.loop.mail_sync import (
    clear_sync_state,
    list_sync_states,
    load_sync_state,
    save_sync_state,
)
from blumkin.loop.queue import peek_queue
from blumkin.mail_sync import MailSyncResetError


def _setup(tmp_path: Path, *, triggers: int = 1) -> LoopSetup:
    table: dict[str, Any] = {}
    for n in range(triggers):
        table[f"receipts{n}"] = {
            "task": "expense-report",
            "instance": f"trip-{n}",
            "kind": "loop",
            "allowlist": ["me@example.com"],
            "profiles": ["work"],
            "subject_tag": f"[trip{n}]" if triggers > 1 else "[trip]",
        }
    return parse_loop_setup(tmp_path, {"loop": {"triggers": table}})


def _item(n: int, tag: str = "[trip]") -> dict[str, str]:
    return {"id": f"msg-{n}", "from_email": "me@example.com", "subject": f"Receipt {n} {tag}"}


class _Mailbox:
    """A fake provider: `inbox` is the newest-N view, `sync` is the incremental one."""

    def __init__(self) -> None:
        self.inbox_items: list[dict[str, str]] = []
        self.new_items: list[dict[str, str]] = []
        self.inbox_calls = 0
        self.sync_calls: list[dict[str, Any]] = []
        self.sync_error: Exception | None = None
        self.sync_errors_once: list[Exception] = []
        self.next_token = "token-1"
        self.message_calls: list[str] = []
        self.message_fail: set[str] = set()

    async def inbox(self, *, top: int = 25, config: Any = None) -> dict[str, Any]:
        self.inbox_calls += 1
        return {"items": list(self.inbox_items[:top])}

    async def sync(
        self, *, state: str | None, since: datetime, top: int = 50, config: Any = None
    ) -> dict[str, Any]:
        self.sync_calls.append({"state": state, "since": since})
        if self.sync_errors_once:
            raise self.sync_errors_once.pop(0)
        if self.sync_error is not None:
            raise self.sync_error
        items, self.new_items = list(self.new_items), []
        return {"items": items, "state": self.next_token}

    async def message(self, *, message_id: str, config: Any = None) -> dict[str, Any]:
        self.message_calls.append(message_id)
        if message_id in self.message_fail:
            raise OSError("graph hiccup")
        return {
            "message": {
                "id": message_id,
                "headers": [
                    {
                        "name": "Authentication-Results",
                        "value": "mx.example; spf=pass; dkim=pass; dmarc=pass",
                    }
                ],
            }
        }

    async def download(self, **kwargs: Any) -> dict[str, Any]:
        return {"saved": []}


def _poll(setup: LoopSetup, box: _Mailbox, *, with_sync: bool = True, email: str = ""):
    return poll_mail_triggers(
        setup,
        inbox=box.inbox,
        sync=box.sync if with_sync else None,
        message=box.message,
        download=box.download,
        refresh=lambda _cfg: {"ok": True},
        load_config=lambda profile: type(
            "Cfg", (), {"provider": "microsoft", "name": profile, "email": email}
        )(),
        list_profiles=lambda: [{"name": "work"}],
    )


def test_a_sync_state_round_trips_privately_and_never_appears_in_a_listing(tmp_path: Path) -> None:
    save_sync_state(
        tmp_path, "work", provider="microsoft", token="SENTINEL-token-1234", mode="delta"
    )
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "SENTINEL-token-1234"
    files = list((tmp_path / "loop" / "mail-sync").glob("*.json"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700
    listing = json.dumps(list_sync_states(tmp_path))
    assert "SENTINEL-token-1234" not in listing
    assert "work" in listing


def test_a_state_for_another_provider_or_a_damaged_file_counts_as_no_state(tmp_path: Path) -> None:
    save_sync_state(tmp_path, "work", provider="google", token="t", mode="delta")
    assert load_sync_state(tmp_path, "work", provider="microsoft") is None
    (path,) = (tmp_path / "loop" / "mail-sync").glob("*.json")
    path.write_text("not json")
    assert load_sync_state(tmp_path, "work", provider="google") is None
    clear_sync_state(tmp_path, "work")
    assert not path.exists()


def test_the_first_poll_takes_a_token_first_then_scans_the_newest_messages(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    box.inbox_items = [_item(1)]
    result = _poll(setup, box)
    assert result.enqueued == 1
    assert [c["state"] for c in box.sync_calls] == [None]
    assert box.inbox_calls == 1
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "token-1" and state.mode == "scan"


def test_later_polls_use_the_token_and_do_not_list_the_inbox(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box)
    box.inbox_calls = 0
    box.next_token = "token-2"
    box.new_items = [_item(2)]
    result = _poll(setup, box)
    assert result.enqueued == 1
    assert box.sync_calls[-1]["state"] == "token-1"
    assert box.inbox_calls == 0
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "token-2" and state.mode == "delta"


def test_a_burst_of_more_than_25_messages_is_all_seen(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box)
    box.new_items = [_item(n) for n in range(1, 41)]
    result = _poll(setup, box)
    assert result.enqueued == 40
    assert len(peek_queue(tmp_path)) == 40


def test_a_poll_with_no_new_mail_fetches_no_message_bodies(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box)
    box.message_calls.clear()
    box.next_token = "token-2"
    result = _poll(setup, box)
    assert result.enqueued == 0 and box.message_calls == []
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "token-2"


def test_an_expired_token_falls_back_to_one_scan_and_a_fresh_token(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box)
    box.inbox_calls = 0
    box.sync_errors_once = [MailSyncResetError("syncStateNotFound")]
    box.inbox_items = [_item(7)]
    box.next_token = "token-fresh"
    result = _poll(setup, box)
    assert result.enqueued == 1
    assert [c["state"] for c in box.sync_calls[-2:]] == ["token-1", None]
    assert box.inbox_calls == 1
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "token-fresh"


def test_a_message_that_fails_to_load_keeps_the_token_so_the_next_poll_retries(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box)
    box.next_token = "token-2"
    box.new_items = [_item(3)]
    box.message_fail = {"msg-3"}
    result = _poll(setup, box)
    assert result.enqueued == 0
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "token-1"
    assert not is_handled(tmp_path, profile="work", message_id="msg-3")
    box.message_fail = set()
    box.new_items = [_item(3)]
    box.next_token = "token-2"
    again = _poll(setup, box)
    assert again.enqueued == 1
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "token-2"


def test_a_sync_error_that_is_not_a_reset_skips_the_profile_and_keeps_the_token(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box)
    box.inbox_calls = 0
    box.sync_error = OSError("network down")
    result = _poll(setup, box)
    assert result.enqueued == 0 and box.inbox_calls == 0
    state = load_sync_state(tmp_path, "work", provider="microsoft")
    assert state is not None and state.token == "token-1"


def test_a_failing_first_sync_still_scans_and_saves_no_token(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    box.sync_error = OSError("not available")
    box.inbox_items = [_item(1)]
    result = _poll(setup, box)
    assert result.enqueued == 1
    assert load_sync_state(tmp_path, "work", provider="microsoft") is None


def test_without_a_sync_function_the_old_scan_still_works(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    box.inbox_items = [_item(1)]
    result = _poll(setup, box, with_sync=False)
    assert result.enqueued == 1


def test_two_triggers_on_one_profile_share_one_sync_call(tmp_path: Path) -> None:
    setup = _setup(tmp_path, triggers=2)
    box = _Mailbox()
    _poll(setup, box)
    box.sync_calls.clear()
    box.new_items = [_item(1, "[trip0]"), _item(2, "[trip1]")]
    box.next_token = "token-2"
    result = _poll(setup, box)
    assert len(box.sync_calls) == 1
    assert result.enqueued == 2
    assert sorted(e.trigger for e in peek_queue(tmp_path)) == ["receipts0", "receipts1"]


def test_status_lists_each_profile_sync_without_the_token(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    box.next_token = "SENTINEL-token-5678"
    _poll(setup, box)
    payload = status(setup)
    assert payload["mail_sync"][0]["profile"] == "work"
    assert payload["mail_sync"][0]["mode"] == "scan"
    assert "SENTINEL-token-5678" not in json.dumps(payload)


@pytest.mark.parametrize("name", ["work", "personal.me", "odd name"])
def test_profile_names_cannot_escape_the_state_folder(tmp_path: Path, name: str) -> None:
    save_sync_state(tmp_path, name, provider="microsoft", token="t", mode="delta")
    files = list((tmp_path / "loop" / "mail-sync").glob("*.json"))
    assert len(files) == 1 and files[0].parent == tmp_path / "loop" / "mail-sync"


def test_status_shows_the_sync_mode_in_the_human_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from blumkin.cli import main

    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[profiles.default]\nclient_id = "abc-123"\n\n[loop.triggers.probe]\ntask = "probe"\n'
        'instance = "default"\nkind = "manual"\n',
        encoding="utf-8",
    )
    save_sync_state(tmp_path, "work", provider="microsoft", token="SENTINEL-token-9", mode="delta")
    result = CliRunner().invoke(main, ["loop", "status"])
    assert result.exit_code == 0, result.output
    assert "mail sync work: delta" in result.output
    assert "SENTINEL-token-9" not in result.output


def test_a_changed_set_of_triggers_for_the_profile_starts_over(tmp_path: Path) -> None:
    box = _Mailbox()
    _poll(_setup(tmp_path), box)
    box.sync_calls.clear()
    box.inbox_calls = 0
    changed = parse_loop_setup(
        tmp_path,
        {
            "loop": {
                "triggers": {
                    "receipts0": {
                        "task": "expense-report",
                        "instance": "trip-0",
                        "kind": "loop",
                        "allowlist": ["someone-else@example.com"],
                        "profiles": ["work"],
                        "subject_tag": "[trip]",
                    }
                }
            }
        },
    )
    _poll(changed, box)
    assert box.sync_calls[0]["state"] is None
    assert box.inbox_calls == 1


def test_a_changed_account_email_starts_over(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box, email="one@example.com")
    box.sync_calls.clear()
    _poll(setup, box, email="two@example.com")
    assert box.sync_calls[0]["state"] is None


def test_pausing_and_resuming_a_profile_forgets_its_token(tmp_path: Path) -> None:
    from blumkin.loop.mail_profiles import pause_profile, resume_profile

    setup = _setup(tmp_path)
    box = _Mailbox()
    _poll(setup, box)
    assert load_sync_state(tmp_path, "work", provider="microsoft") is not None
    pause_profile(tmp_path, "work", reason="renewal_failed")
    assert load_sync_state(tmp_path, "work", provider="microsoft") is None
    _poll(setup, box)  # paused: skipped
    resume_profile(tmp_path, "work")
    box.sync_calls.clear()
    _poll(setup, box)
    assert box.sync_calls[0]["state"] is None


def test_the_first_read_looks_back_a_few_minutes_for_clock_skew(tmp_path: Path) -> None:
    from datetime import UTC, timedelta
    from datetime import datetime as dt

    box = _Mailbox()
    _poll(_setup(tmp_path), box)
    since = box.sync_calls[0]["since"]
    assert dt.now(UTC) - since >= timedelta(minutes=4)


def test_a_sync_that_cannot_run_is_logged_without_the_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    box = _Mailbox()
    _poll(_setup(tmp_path), box)
    box.sync_error = OSError("SENTINEL-token-secret network down")
    with caplog.at_level(logging.WARNING, logger="blumkin.loop"):
        _poll(_setup(tmp_path), box)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "mail sync" in text and "OSError" in text
    assert "SENTINEL" not in text and "token-1" not in text


def test_a_token_that_cannot_be_saved_does_not_stop_the_other_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("blumkin.loop.mail_poll.save_sync_state", boom)
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
                        "subject_tag": "[trip]",
                    }
                }
            }
        },
    )
    box = _Mailbox()
    box.inbox_items = [_item(1)]
    with caplog.at_level(logging.WARNING, logger="blumkin.loop"):
        result = poll_mail_triggers(
            setup,
            inbox=box.inbox,
            sync=box.sync,
            message=box.message,
            download=box.download,
            refresh=lambda _cfg: {"ok": True},
            load_config=lambda profile: type(
                "Cfg", (), {"provider": "microsoft", "name": profile, "email": ""}
            )(),
            list_profiles=lambda: [{"name": "one"}, {"name": "two"}],
        )
    assert len(box.sync_calls) == 2
    assert result.enqueued == 2
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "not saved" in text and "OSError" in text and "token-1" not in text
