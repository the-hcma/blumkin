"""Rename and rehome receipts into a managed instance folder (issue #491)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blumkin.browser.ledger import LedgerError, begin_instance
from blumkin.browser.ledger_commands import ledger_discard
from blumkin.browser.receipt_rehome import (
    AlreadyFiledError,
    RehomeError,
    UnconfirmedError,
    canonical_receipt_name,
    file_sha256,
    managed_instance_dir,
    rehome_receipt,
    restore_rehomed,
    sanitize_vendor_for_name,
)
from blumkin.config import BrowserConfig, BrowserSetup, BrowserTaskPolicy
from blumkin.receipts import Confidence, ReceiptField, ReceiptFields


def _fields(
    *,
    date: str = "2026-10-20",
    vendor: str = "Taxi Co",
    total: str = "18.50",
    currency: str = "USD",
    confidence: Confidence = Confidence.HIGH,
) -> ReceiptFields:
    return ReceiptFields(
        currency=ReceiptField(confidence=confidence, value=currency),
        date=ReceiptField(confidence=confidence, value=date),
        total=ReceiptField(confidence=confidence, value=total),
        vendor=ReceiptField(confidence=confidence, value=vendor),
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    config = tmp_path / "config"
    config.mkdir()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    begin_instance(config, "expense-report", "trip-lisbon")
    return config, uploads, [str(uploads)]


def test_canonical_name_uses_date_vendor_amount_currency_and_hash() -> None:
    digest = "a" * 64
    name = canonical_receipt_name(
        date="2026-10-20",
        vendor="Taxi Co",
        amount="18.50",
        currency="USD",
        sha256=digest,
        suffix=".jpg",
    )
    assert name == f"2026-10-20_taxi-co_18.50-USD_{digest[:8]}.jpg"


def test_vendor_sanitize_strips_separators_and_controls() -> None:
    assert sanitize_vendor_for_name("Taxi/Co\u202e..\nHotel") == "taxi-co-hotel"
    assert sanitize_vendor_for_name("  ACME___Coffee  ") == "acme-coffee"


def test_unconfirmed_fields_leave_the_file_untouched(tmp_path: Path) -> None:
    config, uploads, dirs = _setup(tmp_path)
    source = tmp_path / "scan.jpg"
    source.write_bytes(b"receipt-bytes")
    with pytest.raises(UnconfirmedError, match="high confidence"):
        rehome_receipt(
            config,
            "expense-report",
            "trip-lisbon",
            source,
            fields=_fields(confidence=Confidence.LOW),
            upload_dirs=dirs,
        )
    assert source.is_file()
    assert list(uploads.iterdir()) == []


def test_rehome_moves_into_managed_folder_under_canonical_name(tmp_path: Path) -> None:
    config, uploads, dirs = _setup(tmp_path)
    source = tmp_path / "scan.jpg"
    source.write_bytes(b"receipt-bytes-one")
    digest = file_sha256(source)
    result = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        source,
        fields=_fields(),
        upload_dirs=dirs,
    )
    assert not source.exists()
    assert result.dest == managed_instance_dir(dirs, "trip-lisbon") / result.name
    assert result.dest.is_file()
    assert result.sha256 == digest
    assert result.name.startswith("2026-10-20_taxi-co_18.50-USD_")
    assert result.name.endswith(".jpg")
    assert result.copied is False
    journal = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "receipt-moves.jsonl"
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [row["event"] for row in lines] == ["intent", "result"]
    assert lines[1]["sha256"] == digest
    assert lines[1]["dest"] == str(result.dest)


def test_email_attachment_is_copied_and_message_id_recorded(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    source = tmp_path / "mail-att.pdf"
    source.write_bytes(b"%PDF-1.4 receipt")
    result = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        source,
        fields=_fields(total="210.00", currency="EUR", vendor="Hotel"),
        upload_dirs=dirs,
        message_id="AAMkAG-test",
    )
    assert source.is_file()
    assert result.copied is True
    assert result.message_id == "AAMkAG-test"
    assert result.dest.is_file()
    assert file_sha256(source) == file_sha256(result.dest)
    journal = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "receipt-moves.jsonl"
    payload = [json.loads(line) for line in journal.read_text().splitlines()][-1]
    assert payload["message_id"] == "AAMkAG-test"
    assert payload["copied"] is True


def test_duplicate_sha256_is_already_filed(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    first = tmp_path / "a.jpg"
    first.write_bytes(b"same-bytes")
    rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        first,
        fields=_fields(),
        upload_dirs=dirs,
    )
    second = tmp_path / "b.jpg"
    second.write_bytes(b"same-bytes")
    with pytest.raises(AlreadyFiledError, match="already filed"):
        rehome_receipt(
            config,
            "expense-report",
            "trip-lisbon",
            second,
            fields=_fields(),
            upload_dirs=dirs,
        )
    assert second.is_file()


def test_discard_restores_moved_receipt_to_original_path(tmp_path: Path) -> None:
    config, uploads, dirs = _setup(tmp_path)
    source = tmp_path / "inbox" / "scan.jpg"
    source.parent.mkdir()
    source.write_bytes(b"restore-me")
    result = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        source,
        fields=_fields(),
        upload_dirs=dirs,
    )
    assert not source.exists()
    assert result.dest.is_file()
    setup = BrowserSetup(
        browser=BrowserConfig(
            tasks={
                "expense-report": BrowserTaskPolicy(
                    allowed_hosts=("example.test",),
                    upload_dirs=tuple(dirs),
                )
            }
        ),
        config_dir=config,
    )
    payload = ledger_discard(setup, "expense-report", "trip-lisbon", yes=True)
    assert source.is_file()
    assert source.read_bytes() == b"restore-me"
    assert not result.dest.exists()
    assert payload["receipts_restored"][0]["restored"] is True
    assert list((uploads / "trip-lisbon").iterdir()) == []


def test_discard_removes_email_copy_but_leaves_source(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    source = tmp_path / "mail.pdf"
    source.write_bytes(b"email-bytes")
    result = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        source,
        fields=_fields(),
        upload_dirs=dirs,
        message_id="msg-1",
    )
    setup = BrowserSetup(
        browser=BrowserConfig(
            tasks={
                "expense-report": BrowserTaskPolicy(
                    allowed_hosts=("example.test",),
                    upload_dirs=tuple(dirs),
                )
            }
        ),
        config_dir=config,
    )
    ledger_discard(setup, "expense-report", "trip-lisbon", yes=True)
    assert source.is_file()
    assert not result.dest.exists()


def test_restore_requires_yes(tmp_path: Path) -> None:
    config, _uploads, _dirs = _setup(tmp_path)
    with pytest.raises(RehomeError, match="--yes"):
        restore_rehomed(config, "expense-report", "trip-lisbon", yes=False)


def test_managed_dir_needs_upload_dirs(tmp_path: Path) -> None:
    with pytest.raises(RehomeError, match="upload_dirs"):
        managed_instance_dir([], "trip-lisbon")


def test_canonical_name_rejects_bad_date() -> None:
    with pytest.raises(RehomeError, match="YYYY-MM-DD"):
        canonical_receipt_name(
            date="10/20/2026",
            vendor="Taxi",
            amount="1.00",
            currency="USD",
            sha256="a" * 64,
            suffix=".jpg",
        )


def test_interrupted_move_resumes_when_dest_already_matches(tmp_path: Path) -> None:
    config, uploads, dirs = _setup(tmp_path)
    source = tmp_path / "scan.jpg"
    source.write_bytes(b"interrupted-bytes")
    digest = file_sha256(source)
    dest_dir = managed_instance_dir(dirs, "trip-lisbon")
    dest_dir.mkdir(parents=True)
    dest = dest_dir / (f"2026-10-20_taxi-co_18.50-USD_{digest[:8]}.jpg")
    dest.write_bytes(b"interrupted-bytes")
    journal = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "receipt-moves.jsonl"
    journal.write_text(
        json.dumps(
            {
                "at": "2026-10-20T12:00:00Z",
                "copied": False,
                "dest": str(dest.resolve()),
                "dest_name": dest.name,
                "event": "intent",
                "message_id": None,
                "sha256": digest,
                "source": str(source.resolve()),
            },
            sort_keys=True,
        )
        + "\n"
    )
    result = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        source,
        fields=_fields(),
        upload_dirs=dirs,
    )
    assert not source.exists()
    assert result.dest == dest.resolve()
    assert result.sha256 == digest
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert lines[-1]["event"] == "result"


def test_restore_rejects_a_path_like_instance_name(tmp_path: Path) -> None:
    config, _uploads, _dirs = _setup(tmp_path)
    with pytest.raises(RehomeError, match="instance name"):
        restore_rehomed(config, "expense-report", "../escape", yes=True)


def test_rehome_rejects_source_already_at_destination(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    dest_dir = managed_instance_dir(dirs, "trip-lisbon")
    dest_dir.mkdir(parents=True)
    candidate = dest_dir / "tmp.jpg"
    candidate.write_bytes(b"already-there")
    dest = dest_dir / f"2026-10-20_taxi-co_18.50-USD_{file_sha256(candidate)[:8]}.jpg"
    candidate.rename(dest)
    with pytest.raises(RehomeError, match="same path"):
        rehome_receipt(
            config,
            "expense-report",
            "trip-lisbon",
            dest,
            fields=_fields(),
            upload_dirs=dirs,
        )
    assert dest.is_file()


def test_resume_without_a_live_intent_is_refused(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    source = tmp_path / "scan.jpg"
    source.write_bytes(b"no-intent")
    digest = file_sha256(source)
    dest_dir = managed_instance_dir(dirs, "trip-lisbon")
    dest_dir.mkdir(parents=True)
    dest = dest_dir / f"2026-10-20_taxi-co_18.50-USD_{digest[:8]}.jpg"
    dest.write_bytes(b"no-intent")
    with pytest.raises(RehomeError, match="live journaled intent"):
        rehome_receipt(
            config,
            "expense-report",
            "trip-lisbon",
            source,
            fields=_fields(),
            upload_dirs=dirs,
        )
    assert source.is_file()


def test_restore_accepts_source_that_already_has_the_bytes(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    source = tmp_path / "inbox" / "scan.jpg"
    source.parent.mkdir()
    source.write_bytes(b"both-sides")
    digest = file_sha256(source)
    dest_dir = managed_instance_dir(dirs, "trip-lisbon")
    dest_dir.mkdir(parents=True)
    dest = dest_dir / f"2026-10-20_taxi-co_18.50-USD_{digest[:8]}.jpg"
    dest.write_bytes(b"both-sides")
    journal = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "receipt-moves.jsonl"
    journal.write_text(
        json.dumps(
            {
                "at": "2026-10-20T12:00:00Z",
                "copied": False,
                "dest": str(dest.resolve()),
                "event": "result",
                "message_id": None,
                "sha256": digest,
                "source": str(source.resolve()),
            },
            sort_keys=True,
        )
        + "\n"
    )
    setup = BrowserSetup(
        browser=BrowserConfig(
            tasks={
                "expense-report": BrowserTaskPolicy(
                    allowed_hosts=("example.test",),
                    upload_dirs=tuple(dirs),
                )
            }
        ),
        config_dir=config,
    )
    payload = ledger_discard(setup, "expense-report", "trip-lisbon", yes=True)
    assert source.is_file()
    assert not dest.exists()
    assert payload["receipts_restored"][0]["restored"] is True


def test_discard_succeeds_when_intent_never_placed_and_source_intact(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    source = tmp_path / "inbox" / "scan.jpg"
    source.parent.mkdir()
    source.write_bytes(b"never-moved")
    digest = file_sha256(source)
    journal = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "receipt-moves.jsonl"
    journal.write_text(
        json.dumps(
            {
                "at": "2026-10-20T12:00:00Z",
                "copied": False,
                "dest": str(tmp_path / "uploads" / "trip-lisbon" / "missing.jpg"),
                "event": "intent",
                "message_id": None,
                "sha256": digest,
                "source": str(source.resolve()),
            },
            sort_keys=True,
        )
        + "\n"
    )
    setup = BrowserSetup(
        browser=BrowserConfig(
            tasks={
                "expense-report": BrowserTaskPolicy(
                    allowed_hosts=("example.test",),
                    upload_dirs=tuple(dirs),
                )
            }
        ),
        config_dir=config,
    )
    payload = ledger_discard(setup, "expense-report", "trip-lisbon", yes=True)
    assert source.is_file()
    assert payload["receipts_restored"][0]["restored"] is True


def test_discard_fails_when_intent_has_no_dest_and_source_is_gone(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    source = tmp_path / "inbox" / "gone.jpg"
    digest = "b" * 64
    journal = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "receipt-moves.jsonl"
    journal.write_text(
        json.dumps(
            {
                "at": "2026-10-20T12:00:00Z",
                "copied": False,
                "dest": str(tmp_path / "uploads" / "trip-lisbon" / "missing.jpg"),
                "event": "intent",
                "message_id": None,
                "sha256": digest,
                "source": str(source),
            },
            sort_keys=True,
        )
        + "\n"
    )
    setup = BrowserSetup(
        browser=BrowserConfig(
            tasks={
                "expense-report": BrowserTaskPolicy(
                    allowed_hosts=("example.test",),
                    upload_dirs=tuple(dirs),
                )
            }
        ),
        config_dir=config,
    )
    with pytest.raises(LedgerError, match="restore"):
        ledger_discard(setup, "expense-report", "trip-lisbon", yes=True)


def test_discard_restores_intent_only_half_finished_move(tmp_path: Path) -> None:
    config, _uploads, dirs = _setup(tmp_path)
    source = tmp_path / "inbox" / "scan.jpg"
    source.parent.mkdir()
    source.write_bytes(b"intent-only")
    digest = file_sha256(source)
    dest_dir = managed_instance_dir(dirs, "trip-lisbon")
    dest_dir.mkdir(parents=True)
    dest = dest_dir / f"2026-10-20_taxi-co_18.50-USD_{digest[:8]}.jpg"
    dest.write_bytes(b"intent-only")
    source.unlink()
    journal = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "receipt-moves.jsonl"
    journal.write_text(
        json.dumps(
            {
                "at": "2026-10-20T12:00:00Z",
                "copied": False,
                "dest": str(dest.resolve()),
                "dest_name": dest.name,
                "event": "intent",
                "message_id": None,
                "sha256": digest,
                "source": str(source.resolve()),
            },
            sort_keys=True,
        )
        + "\n"
    )
    setup = BrowserSetup(
        browser=BrowserConfig(
            tasks={
                "expense-report": BrowserTaskPolicy(
                    allowed_hosts=("example.test",),
                    upload_dirs=tuple(dirs),
                )
            }
        ),
        config_dir=config,
    )
    payload = ledger_discard(setup, "expense-report", "trip-lisbon", yes=True)
    assert source.is_file()
    assert source.read_bytes() == b"intent-only"
    assert not dest.exists()
    assert payload["receipts_restored"][0]["restored"] is True


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_replace_files_the_new_receipt_and_sets_the_old_one_aside(tmp_path: Path) -> None:
    config, uploads, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        _file(tmp_path, "a.pdf", b"bad-render"),
        fields=_fields(),
        upload_dirs=dirs,
        message_id="m1",
    )

    new = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        _file(tmp_path, "b.pdf", b"good-render"),
        fields=_fields(),
        upload_dirs=dirs,
        message_id="m1",
        replace=old.sha256[:8],
    )

    assert new.replaced == old.name
    assert not old.dest.exists()
    assert new.dest.is_file()
    aside = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "superseded" / old.name
    assert aside.read_bytes() == b"bad-render"
    assert sorted(path.name for path in managed_instance_dir(dirs, "trip-lisbon").iterdir()) == [
        new.name
    ]


def test_replace_by_file_name_works_and_the_old_bytes_can_be_filed_again(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(),
        upload_dirs=dirs,
        message_id="m1",
    )
    rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        _file(tmp_path, "b.pdf", b"two"),
        fields=_fields(total="20.00"),
        upload_dirs=dirs,
        message_id="m1",
        replace=old.name,
    )

    again = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        _file(tmp_path, "c.pdf", b"one"),
        fields=_fields(),
        upload_dirs=dirs,
        message_id="m1",
    )

    assert again.sha256 == old.sha256


def test_replace_with_an_unknown_receipt_changes_nothing(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    source = _file(tmp_path, "b.pdf", b"two")

    with pytest.raises(RehomeError, match="one filed receipt"):
        rehome_receipt(
            config,
            "expense-report",
            "trip-lisbon",
            source,
            fields=_fields(),
            upload_dirs=dirs,
            replace="deadbeef",
        )

    assert source.is_file()


def test_discard_leaves_a_superseded_receipt_where_it_was_set_aside(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(),
        upload_dirs=dirs,
    )
    rehome_receipt(
        config,
        "expense-report",
        "trip-lisbon",
        _file(tmp_path, "b.pdf", b"two"),
        fields=_fields(total="20.00"),
        upload_dirs=dirs,
        replace=old.name,
    )

    restored = restore_rehomed(config, "expense-report", "trip-lisbon", yes=True)

    assert [item.sha256 for item in restored] == [file_sha256(tmp_path / "b.pdf")]
    assert (
        config / "browser-tasks" / "expense-report" / "trip-lisbon" / "superseded" / old.name
    ).is_file()


def test_replace_checks_the_old_receipt_before_filing_the_new_one(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs,
    )  # fmt: skip
    old.dest.write_bytes(b"tampered")
    source = _file(tmp_path, "b.pdf", b"two")

    with pytest.raises(RehomeError, match="intact"):
        rehome_receipt(
            config, "expense-report", "trip-lisbon", source,
            fields=_fields(total="20.00"), upload_dirs=dirs, replace=old.name,
        )  # fmt: skip

    assert source.is_file()


def test_replace_with_identical_bytes_is_refused(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs, message_id="m",
    )  # fmt: skip

    with pytest.raises(RehomeError, match="differs"):
        rehome_receipt(
            config, "expense-report", "trip-lisbon", _file(tmp_path, "b.pdf", b"one"),
            fields=_fields(), upload_dirs=dirs, replace=old.name,
        )  # fmt: skip


def test_replace_resumes_when_an_earlier_run_filed_the_new_file_but_stopped(
    tmp_path: Path,
) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs, message_id="m",
    )  # fmt: skip
    new_source = _file(tmp_path, "b.pdf", b"two")
    rehome_receipt(
        config, "expense-report", "trip-lisbon", new_source,
        fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m",
    )  # fmt: skip

    resumed = rehome_receipt(
        config, "expense-report", "trip-lisbon", new_source,
        fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m", replace=old.name,
    )  # fmt: skip

    assert resumed.replaced == old.name
    assert not old.dest.exists()


def test_replace_finishes_the_journal_when_the_file_was_already_set_aside(
    tmp_path: Path,
) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs, message_id="m",
    )  # fmt: skip
    aside_dir = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "superseded"
    aside_dir.mkdir()
    old.dest.replace(aside_dir / old.name)  # a crash after the move, before the journal

    new = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "b.pdf", b"two"),
        fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m", replace=old.name,
    )  # fmt: skip

    assert new.replaced == old.name
    with pytest.raises(RehomeError, match="one filed receipt"):
        rehome_receipt(
            config, "expense-report", "trip-lisbon", _file(tmp_path, "c.pdf", b"three"),
            fields=_fields(), upload_dirs=dirs, replace=old.name,
        )  # fmt: skip


def test_a_taken_name_in_superseded_gets_a_numbered_one(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs, message_id="m",
    )  # fmt: skip
    aside_dir = config / "browser-tasks" / "expense-report" / "trip-lisbon" / "superseded"
    aside_dir.mkdir()
    (aside_dir / old.name).write_bytes(b"something else entirely")

    rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "b.pdf", b"two"),
        fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m", replace=old.name,
    )  # fmt: skip

    assert (aside_dir / f"1-{old.name}").read_bytes() == b"one"


def test_replace_is_refused_for_a_closed_instance_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from blumkin.browser import receipt_rehome
    from blumkin.browser.ledger import Phase

    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs, message_id="m",
    )  # fmt: skip
    new_source = _file(tmp_path, "b.pdf", b"two")
    rehome_receipt(
        config, "expense-report", "trip-lisbon", new_source,
        fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m",
    )  # fmt: skip
    monkeypatch.setattr(
        receipt_rehome, "instance_status", lambda *_a, **_k: SimpleNamespace(phase=Phase.SUBMITTED)
    )

    with pytest.raises(RehomeError, match="open ledger instance"):
        rehome_receipt(
            config, "expense-report", "trip-lisbon", new_source,
            fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m", replace=old.name,
        )  # fmt: skip

    assert old.dest.is_file()


def test_replace_rejects_a_bad_instance_name_before_looking_anything_up(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)

    with pytest.raises(RehomeError, match="instance name"):
        rehome_receipt(
            config, "expense-report", "../escape", _file(tmp_path, "b.pdf", b"two"),
            fields=_fields(), upload_dirs=dirs, replace="deadbeef",
        )  # fmt: skip


def test_replace_resumes_without_a_message_id_after_the_source_was_moved(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs,
    )  # fmt: skip
    new_source = _file(tmp_path, "b.pdf", b"two")
    rehome_receipt(
        config, "expense-report", "trip-lisbon", new_source,
        fields=_fields(total="20.00"), upload_dirs=dirs,
    )  # fmt: skip
    assert not new_source.exists()  # a move: the source is gone, only the journal knows it

    resumed = rehome_receipt(
        config, "expense-report", "trip-lisbon", new_source,
        fields=_fields(total="20.00"), upload_dirs=dirs, replace=old.name,
    )  # fmt: skip

    assert resumed.replaced == old.name
    assert not old.dest.exists()
    assert resumed.dest.read_bytes() == b"two"


def test_replace_refuses_a_filed_replacement_that_is_no_longer_intact(tmp_path: Path) -> None:
    config, _, dirs = _setup(tmp_path)
    old = rehome_receipt(
        config, "expense-report", "trip-lisbon", _file(tmp_path, "a.pdf", b"one"),
        fields=_fields(), upload_dirs=dirs, message_id="m",
    )  # fmt: skip
    new_source = _file(tmp_path, "b.pdf", b"two")
    new = rehome_receipt(
        config, "expense-report", "trip-lisbon", new_source,
        fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m",
    )  # fmt: skip
    new.dest.unlink()

    with pytest.raises(RehomeError, match="intact"):
        rehome_receipt(
            config, "expense-report", "trip-lisbon", new_source,
            fields=_fields(total="20.00"), upload_dirs=dirs, message_id="m", replace=old.name,
        )  # fmt: skip

    assert old.dest.is_file()
