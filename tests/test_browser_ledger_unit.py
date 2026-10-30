"""The task instance ledger: an append-only journal of what has been filed (issue #488)."""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import pytest

from blumkin.browser.ledger import (
    ItemKind,
    ItemState,
    LedgerError,
    LedgerNotFoundError,
    Phase,
    SessionOutcome,
    begin_instance,
    discard_instance,
    instance_status,
    list_instances,
    mark_submitted,
    record_item,
    record_report,
    record_session,
)

_SHA = "ab12cd34" * 8
_OTHER_SHA = "ef56ab78" * 8


def _journal(config: Path, task: str = "timesheet", instance: str = "2026-p21") -> Path:
    return config / "browser-tasks" / task / instance / "journal.jsonl"


def _file_day(config: Path, day: str, *, instance: str = "2026-p21") -> None:
    for state in (ItemState.STAGED, ItemState.FILLED, ItemState.VERIFIED):
        record_item(config, "timesheet", instance, ItemKind.DAY, day, state, model_version=1)


def test_beginning_an_instance_creates_a_private_journal(tmp_path: Path) -> None:
    status = begin_instance(tmp_path, "timesheet", "2026-p21", expected={ItemKind.DAY: 10})
    assert status.phase is Phase.OPEN
    assert status.summary() == "0 of 10 days filed, not submitted"
    journal = _journal(tmp_path)
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.parent.parent.stat().st_mode) == 0o700


def test_status_counts_each_state_and_reads_like_a_sentence(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21", expected={ItemKind.DAY: 10})
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        _file_day(tmp_path, day)
    record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "2026-10-22", ItemState.STAGED)
    record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "2026-10-23", ItemState.STAGED)
    record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "2026-10-23", ItemState.FAILED)
    status = instance_status(tmp_path, "timesheet", "2026-p21")
    assert status.count(ItemKind.DAY, ItemState.VERIFIED) == 3
    assert status.count(ItemKind.DAY, ItemState.STAGED) == 1
    assert status.count(ItemKind.DAY, ItemState.FAILED) == 1
    assert status.summary() == "3 of 10 days filed, 1 in progress, 1 failed, not submitted"


def test_receipts_are_counted_without_an_expected_total(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    for n, sha in enumerate((_SHA, _OTHER_SHA)):
        for state in (ItemState.STAGED, ItemState.FILLED, ItemState.VERIFIED):
            record_item(
                tmp_path,
                "expense-report",
                "trip-lisbon",
                ItemKind.RECEIPT,
                f"receipt-{n}",
                state,
                model_version=2,
                sha256=sha,
            )
    status = instance_status(tmp_path, "expense-report", "trip-lisbon")
    assert status.summary() == "2 receipts filed, not submitted"


def test_one_receipt_reads_in_the_singular(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    for state in (ItemState.STAGED, ItemState.FILLED, ItemState.VERIFIED):
        record_item(
            tmp_path,
            "expense-report",
            "trip-lisbon",
            ItemKind.RECEIPT,
            "r1",
            state,
            sha256=_SHA,
        )
    assert (
        instance_status(tmp_path, "expense-report", "trip-lisbon").summary()
        == "1 receipt filed, not submitted"
    )


@pytest.mark.parametrize(
    "steps",
    [
        (ItemState.FILLED,),
        (ItemState.VERIFIED,),
        (ItemState.STAGED, ItemState.VERIFIED),
        (ItemState.STAGED, ItemState.FILLED, ItemState.VERIFIED, ItemState.STAGED),
        (ItemState.STAGED, ItemState.FILLED, ItemState.STAGED),
    ],
)
def test_a_state_that_skips_or_goes_backward_is_refused(
    tmp_path: Path, steps: tuple[ItemState, ...]
) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerError, match="Expected"):
        for state in steps:
            record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "2026-10-19", state)


def test_a_failed_item_can_be_staged_again(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    for state in (ItemState.STAGED, ItemState.FAILED, ItemState.STAGED, ItemState.FILLED):
        record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "2026-10-19", state)
    status = instance_status(tmp_path, "timesheet", "2026-p21")
    assert [i.state for i in status.items] == [ItemState.FILLED]


def test_a_receipt_needs_its_hash_and_keeps_it(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    with pytest.raises(LedgerError, match="sha256"):
        record_item(
            tmp_path, "expense-report", "trip-lisbon", ItemKind.RECEIPT, "r1", ItemState.STAGED
        )
    record_item(
        tmp_path,
        "expense-report",
        "trip-lisbon",
        ItemKind.RECEIPT,
        "r1",
        ItemState.STAGED,
        sha256=_SHA,
    )
    with pytest.raises(LedgerError, match="sha256"):
        record_item(
            tmp_path,
            "expense-report",
            "trip-lisbon",
            ItemKind.RECEIPT,
            "r1",
            ItemState.FILLED,
            sha256=_OTHER_SHA,
        )
    filled = record_item(
        tmp_path, "expense-report", "trip-lisbon", ItemKind.RECEIPT, "r1", ItemState.FILLED
    )
    assert filled.sha256 == _SHA


@pytest.mark.parametrize("sha", ["", "xyz", "AB12" * 16, "ab12" * 15, "ab12" * 17])
def test_a_malformed_hash_is_refused(tmp_path: Path, sha: str) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    with pytest.raises(LedgerError, match="sha256"):
        record_item(
            tmp_path,
            "expense-report",
            "trip-lisbon",
            ItemKind.RECEIPT,
            "r1",
            ItemState.STAGED,
            sha256=sha,
        )


@pytest.mark.parametrize("version", [0, -1])
def test_a_model_version_must_be_positive(tmp_path: Path, version: int) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerError, match="model version"):
        record_item(
            tmp_path,
            "timesheet",
            "2026-p21",
            ItemKind.DAY,
            "2026-10-19",
            ItemState.STAGED,
            model_version=version,
        )


def test_the_model_version_is_kept_with_the_item(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    record_item(
        tmp_path,
        "timesheet",
        "2026-p21",
        ItemKind.DAY,
        "2026-10-19",
        ItemState.STAGED,
        model_version=3,
    )
    status = instance_status(tmp_path, "timesheet", "2026-p21")
    assert status.items[0].model_version == 3


@pytest.mark.parametrize("name", ["", "../x", "Trip", "a/b", "-a", "a" * 65, "a b", "a\n"])
def test_an_instance_name_that_is_not_a_plain_word_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(LedgerError, match="instance"):
        begin_instance(tmp_path, "timesheet", name)


@pytest.mark.parametrize("item", ["", "a/b", "../x", "x" * 65, "a b", "A", "a\n"])
def test_an_item_id_that_is_not_a_plain_word_is_refused(tmp_path: Path, item: str) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerError, match="item"):
        record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, item, ItemState.STAGED)


def test_beginning_an_instance_twice_is_refused(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerError, match="already"):
        begin_instance(tmp_path, "timesheet", "2026-p21")


def test_an_unknown_instance_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(LedgerNotFoundError):
        instance_status(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerNotFoundError):
        record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "d", ItemState.STAGED)


def test_a_torn_last_line_is_ignored_on_read_and_repaired_on_the_next_write(
    tmp_path: Path,
) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    _file_day(tmp_path, "2026-10-19")
    journal = _journal(tmp_path)
    with journal.open("ab") as handle:
        handle.write(b'{"event":"item","seq":99,"kind":"day","it')
    assert (
        instance_status(tmp_path, "timesheet", "2026-p21").count(ItemKind.DAY, ItemState.VERIFIED)
        == 1
    )
    record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "2026-10-20", ItemState.STAGED)
    lines = journal.read_bytes().splitlines()
    assert all(json.loads(line) for line in lines)
    assert (
        instance_status(tmp_path, "timesheet", "2026-p21").count(ItemKind.DAY, ItemState.STAGED)
        == 1
    )


def test_a_damaged_line_in_the_middle_is_an_error_not_silently_skipped(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    _file_day(tmp_path, "2026-10-19")
    journal = _journal(tmp_path)
    lines = journal.read_bytes().splitlines(keepends=True)
    lines[1] = b"not json\n"
    journal.write_bytes(b"".join(lines))
    with pytest.raises(LedgerError, match="line 2"):
        instance_status(tmp_path, "timesheet", "2026-p21")


def test_concurrent_writers_never_corrupt_the_journal(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for m in range(5):
                item = f"d{n}-{m}"
                for state in (ItemState.STAGED, ItemState.FILLED, ItemState.VERIFIED):
                    record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, item, state)
        except BaseException as exc:  # noqa: BLE001 - collected, then asserted on
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    entries = [json.loads(line) for line in _journal(tmp_path).read_bytes().splitlines()]
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))
    assert len(entries) == 1 + 8 * 5 * 3
    assert (
        instance_status(tmp_path, "timesheet", "2026-p21").count(ItemKind.DAY, ItemState.VERIFIED)
        == 40
    )


def test_marking_submitted_closes_the_instance(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21", expected={ItemKind.DAY: 1})
    _file_day(tmp_path, "2026-10-19")
    status = mark_submitted(tmp_path, "timesheet", "2026-p21")
    assert status.phase is Phase.SUBMITTED
    assert status.summary() == "1 of 1 day filed, submitted"
    with pytest.raises(LedgerError, match="submitted"):
        record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "d2", ItemState.STAGED)
    with pytest.raises(LedgerError, match="submitted"):
        mark_submitted(tmp_path, "timesheet", "2026-p21")


def test_an_item_still_in_progress_does_not_stop_the_user_marking_it_submitted(
    tmp_path: Path,
) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "d1", ItemState.STAGED)
    status = mark_submitted(tmp_path, "timesheet", "2026-p21")
    assert status.phase is Phase.SUBMITTED
    assert status.count(ItemKind.DAY, ItemState.STAGED) == 1


def test_discarding_keeps_the_journal_and_closes_the_instance(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    _file_day(tmp_path, "2026-10-19")
    status = discard_instance(tmp_path, "timesheet", "2026-p21")
    assert status.phase is Phase.DISCARDED
    assert _journal(tmp_path).is_file()
    assert status.summary().endswith("discarded")
    with pytest.raises(LedgerError, match="discarded"):
        record_item(tmp_path, "timesheet", "2026-p21", ItemKind.DAY, "d2", ItemState.STAGED)
    with pytest.raises(LedgerError, match="already"):
        begin_instance(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerError, match="submitted"):
        mark_submitted(tmp_path, "timesheet", "2026-p21")


def test_a_submitted_instance_cannot_be_discarded(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    mark_submitted(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerError, match="submitted"):
        discard_instance(tmp_path, "timesheet", "2026-p21")


def test_instances_are_listed_with_their_phase(tmp_path: Path) -> None:
    assert list_instances(tmp_path, "timesheet") == []
    begin_instance(tmp_path, "timesheet", "2026-p22")
    begin_instance(tmp_path, "timesheet", "2026-p21")
    mark_submitted(tmp_path, "timesheet", "2026-p21")
    assert list_instances(tmp_path, "timesheet") == [
        ("2026-p21", Phase.SUBMITTED),
        ("2026-p22", Phase.OPEN),
    ]


def test_a_symlinked_instance_directory_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    task_dir = tmp_path / "browser-tasks" / "timesheet"
    task_dir.mkdir(parents=True)
    (task_dir / "2026-p21").symlink_to(target)
    with pytest.raises(LedgerError, match="symlink"):
        begin_instance(tmp_path, "timesheet", "2026-p21")


def test_the_journal_holds_only_ids_states_versions_and_hashes(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon", expected={ItemKind.RECEIPT: 2})
    record_item(
        tmp_path,
        "expense-report",
        "trip-lisbon",
        ItemKind.RECEIPT,
        "r1",
        ItemState.STAGED,
        model_version=1,
        sha256=_SHA,
    )
    mark_submitted(tmp_path, "expense-report", "trip-lisbon")
    entries = [
        json.loads(line)
        for line in _journal(tmp_path, "expense-report", "trip-lisbon").read_bytes().splitlines()
    ]
    assert [e["event"] for e in entries] == ["begin", "item", "submitted"]
    assert set(entries[0]) == {"at", "event", "expected", "seq"}
    assert set(entries[1]) == {
        "at",
        "event",
        "item",
        "kind",
        "model_version",
        "seq",
        "sha256",
        "state",
    }
    assert set(entries[2]) == {"at", "event", "seq"}


def test_low_confidence_is_a_flag_that_later_states_keep(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    staged = record_item(
        tmp_path,
        "expense-report",
        "trip-lisbon",
        ItemKind.RECEIPT,
        "r1",
        ItemState.STAGED,
        low_confidence=True,
        sha256=_SHA,
    )
    assert staged.low_confidence is True
    filled = record_item(
        tmp_path, "expense-report", "trip-lisbon", ItemKind.RECEIPT, "r1", ItemState.FILLED
    )
    assert filled.low_confidence is True
    status = instance_status(tmp_path, "expense-report", "trip-lisbon")
    assert [i.low_confidence for i in status.items] == [True]
    record_item(
        tmp_path,
        "expense-report",
        "trip-lisbon",
        ItemKind.RECEIPT,
        "r1",
        ItemState.VERIFIED,
        low_confidence=False,
    )
    status = instance_status(tmp_path, "expense-report", "trip-lisbon")
    assert [i.low_confidence for i in status.items] == [False]
    entries = [
        json.loads(line)
        for line in _journal(tmp_path, "expense-report", "trip-lisbon").read_bytes().splitlines()
    ]
    # A boolean flag only: never a reading, an amount or a vendor.
    assert [e.get("low_confidence") for e in entries[1:]] == [True, True, False]


def test_an_item_never_flagged_has_no_flag_and_no_journal_key(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    record = record_item(
        tmp_path,
        "expense-report",
        "trip-lisbon",
        ItemKind.RECEIPT,
        "r1",
        ItemState.STAGED,
        sha256=_SHA,
    )
    assert record.low_confidence is None
    line = _journal(tmp_path, "expense-report", "trip-lisbon").read_bytes().splitlines()[1]
    assert "low_confidence" not in json.loads(line)


def test_low_confidence_is_for_receipts_only(tmp_path: Path) -> None:
    begin_instance(tmp_path, "timesheet", "2026-p21")
    with pytest.raises(LedgerError, match="low_confidence"):
        record_item(
            tmp_path,
            "timesheet",
            "2026-p21",
            ItemKind.DAY,
            "2026-10-19",
            ItemState.STAGED,
            low_confidence=True,
        )


def test_a_damaged_low_confidence_value_is_a_journal_error(tmp_path: Path) -> None:
    from blumkin.browser.ledger import LedgerJournalError

    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    record_item(
        tmp_path,
        "expense-report",
        "trip-lisbon",
        ItemKind.RECEIPT,
        "r1",
        ItemState.STAGED,
        sha256=_SHA,
    )
    journal = _journal(tmp_path, "expense-report", "trip-lisbon")
    lines = journal.read_bytes().splitlines()
    entry = json.loads(lines[1])
    entry["low_confidence"] = "yes"
    lines[1] = json.dumps(entry, sort_keys=True).encode()
    journal.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(LedgerJournalError):
        instance_status(tmp_path, "expense-report", "trip-lisbon")


# --------------------------------------------------------------------------- report, session (#592)


def test_the_report_is_recorded_without_an_amount_and_reads_in_the_summary(
    tmp_path: Path,
) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")

    status = record_report(
        tmp_path, "expense-report", "trip-lisbon",
        report_id="100234", draft_saved=True, total_confirmed=True,
    )  # fmt: skip

    assert status.report is not None
    assert (status.report.report_id, status.report.draft_saved) == ("100234", True)
    assert "report 100234 saved as a draft, total confirmed" in status.summary()
    assert status.summary().endswith("not submitted")
    journal = tmp_path / "browser-tasks" / "expense-report" / "trip-lisbon" / "journal.jsonl"
    assert "84" not in journal.read_text()  # nothing like a total ever reaches the journal


def test_a_report_field_left_out_keeps_its_last_value(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    record_report(tmp_path, "expense-report", "trip-lisbon", report_id="100234", draft_saved=True)

    status = record_report(tmp_path, "expense-report", "trip-lisbon", total_confirmed=True)

    assert status.report is not None
    assert (status.report.report_id, status.report.draft_saved, status.report.total_confirmed) == (
        "100234", True, True,
    )  # fmt: skip


@pytest.mark.parametrize("report_id", ["", "bad id", "../x", "-x", "x" * 65, "a/b", "1;2"])
def test_a_report_id_that_is_not_an_identifier_is_refused(tmp_path: Path, report_id: str) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")

    with pytest.raises(LedgerError, match="report id"):
        record_report(tmp_path, "expense-report", "trip-lisbon", report_id=report_id)


def test_recording_nothing_about_the_report_is_an_error(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")

    with pytest.raises(LedgerError, match="to record"):
        record_report(tmp_path, "expense-report", "trip-lisbon")


def test_a_draft_that_is_not_saved_reads_as_not_yet_saved(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")

    status = record_report(
        tmp_path, "expense-report", "trip-lisbon", report_id="7", draft_saved=False
    )

    assert "report 7 not yet saved" in status.summary()


def test_the_session_outcome_is_recorded_and_the_last_answer_wins(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")

    record_session(tmp_path, "expense-report", "trip-lisbon", SessionOutcome.PENDING)
    status = record_session(tmp_path, "expense-report", "trip-lisbon", SessionOutcome.DONE)

    assert status.session is not None
    assert status.session.outcome is SessionOutcome.DONE
    assert "session done" in status.summary()
    assert status.phase is Phase.OPEN  # closing a session is not submitting


def test_a_pending_instance_says_how_to_resume(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    record_report(tmp_path, "expense-report", "trip-lisbon", report_id="100234", draft_saved=True)

    status = record_session(tmp_path, "expense-report", "trip-lisbon", SessionOutcome.PENDING)

    hint = status.resume_hint()
    assert hint is not None
    assert "reopen the saved report 100234" in hint
    assert "ledger reconcile expense-report trip-lisbon" in hint


def test_a_done_or_submitted_instance_has_nothing_to_resume(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    done = record_session(tmp_path, "expense-report", "trip-lisbon", SessionOutcome.DONE)
    assert done.resume_hint() is None
    record_session(tmp_path, "expense-report", "trip-lisbon", SessionOutcome.PENDING)

    submitted = mark_submitted(tmp_path, "expense-report", "trip-lisbon")

    assert submitted.resume_hint() is None


def test_what_is_on_the_server_is_what_was_read_back_and_the_rest_is_local(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    for item, states in (
        ("a", (ItemState.STAGED, ItemState.FILLED, ItemState.VERIFIED)),
        ("b", (ItemState.STAGED, ItemState.FILLED)),
        ("c", (ItemState.STAGED,)),
    ):
        for state in states:
            record_item(
                tmp_path, "expense-report", "trip-lisbon", ItemKind.RECEIPT, item, state,
                sha256=_SHA,
            )  # fmt: skip

    status = instance_status(tmp_path, "expense-report", "trip-lisbon")

    assert [i.item for i in status.on_server()] == ["a"]
    assert [i.item for i in status.local_only()] == ["b", "c"]


def test_a_closed_instance_takes_no_report_or_session_entries(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    mark_submitted(tmp_path, "expense-report", "trip-lisbon")

    with pytest.raises(LedgerError, match="open instance"):
        record_report(tmp_path, "expense-report", "trip-lisbon", draft_saved=True)
    with pytest.raises(LedgerError, match="open instance"):
        record_session(tmp_path, "expense-report", "trip-lisbon", SessionOutcome.PENDING)


def test_a_missing_instance_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(LedgerNotFoundError):
        record_report(tmp_path, "expense-report", "nope", draft_saved=True)
    with pytest.raises(LedgerNotFoundError):
        record_session(tmp_path, "expense-report", "nope", SessionOutcome.DONE)


def test_a_journal_with_a_bad_report_or_session_entry_is_refused_not_skipped(
    tmp_path: Path,
) -> None:
    begin_instance(tmp_path, "expense-report", "trip-lisbon")
    journal = tmp_path / "browser-tasks" / "expense-report" / "trip-lisbon" / "journal.jsonl"
    lines = journal.read_text().splitlines()
    bad = {"at": "2026-10-30T00:00:00Z", "event": "session", "outcome": "maybe", "seq": 2}
    journal.write_text("\n".join([*lines, json.dumps(bad)]) + "\n")

    from blumkin.browser.ledger import LedgerJournalError

    with pytest.raises(LedgerJournalError):
        instance_status(tmp_path, "expense-report", "trip-lisbon")
