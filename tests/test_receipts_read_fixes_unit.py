"""Vendor extraction, quiet PDF logging, photo OCR prep, find --new-since, to-pdf (issue #585)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_USAGE
from blumkin.receipt_pdf import ReceiptPdfError, image_to_pdf
from blumkin.receipts import Confidence, find_local_receipts, parse_receipt_text
from blumkin.skills import docs_read


def test_a_known_vendor_in_the_text_beats_a_stray_heading() -> None:
    fields = parse_receipt_text("Tip\nThis is your charge summary\nUber\nTotal $18.50\n")

    assert fields.vendor.value == "Uber"
    assert fields.vendor.confidence is Confidence.LOW


def test_headings_that_are_not_names_are_skipped() -> None:
    text = "This is your charge summary\nTip\nYour ride with Dana\nCorner Cafe\nTotal $4.00\n"

    assert parse_receipt_text(text).vendor.value == "Corner Cafe"


def test_the_sender_names_the_vendor_when_the_text_does_not() -> None:
    fields = parse_receipt_text(
        "Tip\nThis is your charge summary\nTotal $18.50\n", sender="Ride Co"
    )

    assert fields.vendor.value == "Ride Co"
    assert "sender" in fields.vendor.note


def test_the_text_still_wins_over_the_sender_when_it_names_a_vendor() -> None:
    fields = parse_receipt_text("Corner Cafe\nTotal $4.00\n", sender="Square Receipts")

    assert fields.vendor.value == "Corner Cafe"


def test_pdfminer_font_warnings_are_silenced_for_pdf_reads() -> None:
    docs_read._import_pdfplumber()

    assert logging.getLogger("pdfminer").getEffectiveLevel() >= logging.ERROR


def test_a_small_photo_is_enlarged_and_flattened_before_ocr() -> None:
    photo = Image.new("RGB", (600, 900), (200, 190, 180))

    prepared = docs_read._prepare_for_ocr(photo)

    assert prepared.mode == "L"
    assert prepared.width >= 1500
    assert prepared.height / prepared.width == pytest.approx(900 / 600, rel=0.01)


def test_a_large_photo_is_not_enlarged() -> None:
    photo = Image.new("RGB", (3000, 2000), (255, 255, 255))

    assert docs_read._prepare_for_ocr(photo).width == 3000


def test_find_reports_when_each_file_arrived_and_filters_by_new_since(tmp_path: Path) -> None:
    (tmp_path / "till.jpg").write_bytes(b"x")

    everything = find_local_receipts(tmp_path)
    recent = find_local_receipts(tmp_path, new_since_epoch=time.time() - 3600)
    later = find_local_receipts(tmp_path, new_since_epoch=time.time() + 3600)

    assert [item.name for item in everything] == ["till.jpg"]
    assert everything[0].added_epoch > 0
    assert [item.name for item in recent] == ["till.jpg"]
    assert later == []


def test_find_new_since_accepts_a_duration_and_a_time_and_marks_the_json(tmp_path: Path) -> None:
    (tmp_path / "till.jpg").write_bytes(b"x")
    base = ["receipts", "find", "--dir", str(tmp_path), "--json"]

    recent = CliRunner().invoke(main, [*base, "--new-since", "30m"])
    future = CliRunner().invoke(main, [*base, "--new-since", "2999-01-01T00:00:00Z"])

    assert recent.exit_code == 0, recent.output
    receipts = json.loads(recent.output)["receipts"]
    assert [item["name"] for item in receipts] == ["till.jpg"]
    assert "added" in receipts[0]
    assert json.loads(future.output)["receipts"] == []


def test_find_new_since_rejects_nonsense(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["receipts", "find", "--dir", str(tmp_path), "--new-since", "yesterdayish"]
    )

    assert result.exit_code == EXIT_USAGE


def _photo(path: Path, size: tuple[int, int] = (200, 300)) -> Path:
    Image.new("RGB", size, (255, 255, 255)).save(path)
    return path


def test_to_pdf_writes_a_pdf_next_to_the_name_you_choose(tmp_path: Path) -> None:
    photo = _photo(tmp_path / "till.jpg")

    dest = image_to_pdf(photo, tmp_path / "till.pdf")

    assert dest.read_bytes().startswith(b"%PDF")
    assert oct(dest.stat().st_mode & 0o777) == "0o600"


def test_to_pdf_never_overwrites(tmp_path: Path) -> None:
    photo = _photo(tmp_path / "till.jpg")
    (tmp_path / "till.pdf").write_bytes(b"keep me")

    with pytest.raises(ReceiptPdfError, match="already exists"):
        image_to_pdf(photo, tmp_path / "till.pdf")

    assert (tmp_path / "till.pdf").read_bytes() == b"keep me"


def test_to_pdf_refuses_what_is_not_an_image(tmp_path: Path) -> None:
    bad = tmp_path / "note.jpg"
    bad.write_text("not an image")

    with pytest.raises(ReceiptPdfError, match="image"):
        image_to_pdf(bad, tmp_path / "note.pdf")


def test_to_pdf_command_into_a_directory_names_the_pdf_after_the_photo(tmp_path: Path) -> None:
    photo = _photo(tmp_path / "till.png")
    out = tmp_path / "out"

    result = CliRunner().invoke(
        main, ["receipts", "to-pdf", str(photo), "--out", f"{out}/", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["pdf"] == str((out / "till.pdf").resolve())


def test_to_pdf_command_reports_a_missing_photo(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["receipts", "to-pdf", str(tmp_path / "gone.jpg"), "--out", str(tmp_path / "o.pdf")]
    )

    assert result.exit_code == EXIT_NOT_FOUND


def test_a_mail_export_text_read_uses_the_sender_as_a_fallback_vendor(tmp_path: Path) -> None:
    import asyncio

    from blumkin.receipts import read_receipt

    text = "Tip\nThis is your charge summary\nTOTAL USD 18.50\nDate: 2026-10-23\n"
    pdf = tmp_path / "ride.pdf"
    pdf.write_bytes(b"%PDF")
    pdf.with_suffix(".txt").write_text(text)
    pdf.with_suffix(".json").write_text(
        json.dumps(
            {
                "from": "receipts@ride.example",
                "from_name": "Ride Co",
                "pdf_sha256": hashlib.sha256(b"%PDF").hexdigest(),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    )

    reading = asyncio.run(read_receipt(pdf, config=None))

    assert reading.fields.vendor.value == "Ride Co"
