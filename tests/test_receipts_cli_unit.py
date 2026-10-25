"""`blumkin receipts find|read` (issue #469)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_SUCCESS, EXIT_USAGE
from blumkin.receipts import (
    DEFAULT_FIND_LIMIT,
    Confidence,
    ReceiptField,
    ReceiptFields,
    ReceiptReading,
    ReceiptStatus,
)

_FIELDS = ReceiptFields(
    currency=ReceiptField(confidence=Confidence.HIGH, value="USD"),
    date=ReceiptField(confidence=Confidence.HIGH, value="2026-10-23"),
    total=ReceiptField(confidence=Confidence.HIGH, value="10.80"),
    vendor=ReceiptField(confidence=Confidence.LOW, value="ACME COFFEE"),
)


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text('[profiles.default]\nclient_id = "abc-123"\n')


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _touch(path: Path, mtime: float) -> Path:
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def test_find_lists_receipt_files_newest_first(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    _touch(receipts / "a.jpg", 1_000)
    _touch(receipts / "b.pdf", 2_000)
    _touch(receipts / "notes.txt", 3_000)
    result = _run("receipts", "find", "--dir", str(receipts))
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert [r["name"] for r in payload["receipts"]] == ["b.pdf", "a.jpg"]
    assert payload["receipts"][0]["path"] == str(receipts / "b.pdf")
    assert payload["receipts"][0]["size"] == 1
    assert payload["ok"] is True


def test_find_since_keeps_only_newer_files(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    _touch(receipts / "old.jpg", 1_000)
    new = _touch(receipts / "new.jpg", 4_000_000_000)
    result = _run("receipts", "find", "--dir", str(receipts), "--since", "2030-01-01")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert [r["path"] for r in json.loads(result.output)["receipts"]] == [str(new)]


def test_find_rejects_a_malformed_since_date(tmp_path: Path) -> None:
    result = _run("receipts", "find", "--dir", str(tmp_path), "--since", "last week")
    assert result.exit_code == EXIT_USAGE
    assert "YYYY-MM-DD" in json.loads(result.output)["message"]


def test_find_reports_a_missing_directory_as_not_found(tmp_path: Path) -> None:
    result = _run("receipts", "find", "--dir", str(tmp_path / "missing"))
    assert result.exit_code == EXIT_NOT_FOUND
    assert json.loads(result.output)["error"] == "not_found"


def test_find_human_output_names_each_file(tmp_path: Path) -> None:
    _touch(tmp_path / "taxi.png", 1_000)
    result = CliRunner().invoke(main, ["receipts", "find", "--dir", str(tmp_path)])
    assert result.exit_code == EXIT_SUCCESS
    assert "taxi.png" in result.output


def test_read_prints_each_field_with_its_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _touch(tmp_path / "r.jpg", 1_000)

    async def fake_read(path: Path, *, config: Any, reader: Any = None) -> ReceiptReading:
        return ReceiptReading(
            fields=_FIELDS,
            injection_warning=None,
            message="",
            ocr_used=True,
            path=str(path),
            status=ReceiptStatus.OK,
        )

    monkeypatch.setattr("blumkin.cli.read_receipt", fake_read)
    result = _run("receipts", "read", str(receipt))
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["fields"]["total"] == {"confidence": "high", "note": "", "value": "10.80"}
    assert payload["fields"]["vendor"]["confidence"] == "low"


def test_read_human_output_shows_confidence_and_asks_when_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _touch(tmp_path / "r.jpg", 1_000)
    nothing = ReceiptField(confidence=Confidence.NONE, value=None)

    async def manual(path: Path, *, config: Any, reader: Any = None) -> ReceiptReading:
        return ReceiptReading(
            fields=ReceiptFields(currency=nothing, date=nothing, total=nothing, vendor=nothing),
            injection_warning=None,
            message="OCR is not available; tell me the vendor, date and total.",
            ocr_used=False,
            path=str(path),
            status=ReceiptStatus.NEEDS_MANUAL,
        )

    monkeypatch.setattr("blumkin.cli.read_receipt", manual)
    result = CliRunner().invoke(main, ["receipts", "read", str(receipt)])
    assert result.exit_code == EXIT_SUCCESS
    assert "tell me the vendor" in result.output
    assert "total: (unreadable)" in result.output


def test_read_of_a_missing_file_is_not_found(tmp_path: Path) -> None:
    result = _run("receipts", "read", str(tmp_path / "nope.jpg"))
    assert result.exit_code == EXIT_NOT_FOUND


def test_read_of_an_unsupported_file_is_a_usage_error(tmp_path: Path) -> None:
    doc = _touch(tmp_path / "r.docx", 1_000)
    result = _run("receipts", "read", str(doc))
    assert result.exit_code == EXIT_USAGE
    assert json.loads(result.output)["error"] == "usage_error"


def test_read_refuses_a_symlink_the_way_find_skips_them(tmp_path: Path) -> None:
    real = _touch(tmp_path / "private.png", 1_000)
    link = tmp_path / "receipt.png"
    link.symlink_to(real)
    result = _run("receipts", "read", str(link))
    assert result.exit_code == EXIT_USAGE
    assert "regular" in json.loads(result.output)["message"]


def test_read_refuses_a_hidden_file_the_way_find_skips_them(tmp_path: Path) -> None:
    hidden = _touch(tmp_path / ".secret.png", 1_000)
    result = _run("receipts", "read", str(hidden))
    assert result.exit_code == EXIT_USAGE


def test_the_human_message_is_stripped_of_control_and_bidi_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _touch(tmp_path / "r.jpg", 1_000)
    nothing = ReceiptField(confidence=Confidence.NONE, value=None)

    async def hostile(path: Path, *, config: Any, reader: Any = None) -> ReceiptReading:
        return ReceiptReading(
            fields=ReceiptFields(currency=nothing, date=nothing, total=nothing, vendor=nothing),
            injection_warning=None,
            message="failed \x1b[31m‮\ninjected line",
            ocr_used=False,
            path=str(path),
            status=ReceiptStatus.NEEDS_MANUAL,
        )

    monkeypatch.setattr("blumkin.cli.read_receipt", hostile)
    result = CliRunner().invoke(main, ["receipts", "read", str(receipt)])
    assert result.exit_code == EXIT_SUCCESS
    assert "\x1b" not in result.output
    assert "‮" not in result.output
    assert "failed" in result.output


def test_reading_a_receipt_through_the_command_logs_no_amounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    receipt = _touch(tmp_path / "r.jpg", 1_000)

    async def fake_read(path: Path, *, config: Any, reader: Any = None) -> ReceiptReading:
        return ReceiptReading(
            fields=_FIELDS,
            injection_warning=None,
            message="",
            ocr_used=True,
            path=str(path),
            status=ReceiptStatus.OK,
        )

    monkeypatch.setattr("blumkin.cli.read_receipt", fake_read)
    with caplog.at_level(logging.DEBUG):
        _run("receipts", "read", str(receipt))
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "10.80" not in text
    assert "ACME" not in text


def test_find_defaults_to_the_shared_limit(tmp_path: Path) -> None:
    for index in range(DEFAULT_FIND_LIMIT + 5):
        _touch(tmp_path / f"r{index}.jpg", 1_000 + index)
    payload = json.loads(_run("receipts", "find", "--dir", str(tmp_path)).output)
    assert len(payload["receipts"]) == DEFAULT_FIND_LIMIT
