"""Photo OCR that finds text on clear phone photos: deskew, flattening and retries (#627)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from PIL import Image, ImageDraw

from blumkin.receipts import ReceiptStatus, read_receipt
from blumkin.skills import docs_read


def _lined(angle: float = 0.0) -> Image.Image:
    """White paper with ten dark lines of print, turned by `angle` degrees."""
    page = Image.new("L", (800, 600), 255)
    draw = ImageDraw.Draw(page)
    for row in range(10):
        draw.rectangle((100, 60 + row * 50, 700, 80 + row * 50), fill=20)
    return page.rotate(angle, expand=False, fillcolor=255) if angle else page


def _row_contrast(image: Image.Image) -> float:
    squeezed = image.resize((1, image.height), Image.Resampling.BOX)
    rows = [float(cast("int", value)) for value in squeezed.get_flattened_data()]
    mean = sum(rows) / len(rows)
    return sum((row - mean) ** 2 for row in rows)


def test_a_tilted_photo_is_straightened() -> None:
    tilted = _lined(angle=6)

    straightened = docs_read._deskew(tilted)

    assert _row_contrast(straightened.resize(tilted.size)) > _row_contrast(tilted) * 1.3


def test_a_level_photo_is_left_alone() -> None:
    level = _lined()

    assert docs_read._deskew(level) is level


def test_a_shadow_across_the_paper_does_not_hide_the_print() -> None:
    page = Image.new("L", (800, 600))
    for x in range(800):  # a bed sheet's shadow: the light falls from 230 to 90 across the photo
        shade = 230 - x * 140 // 800
        page.paste(shade, (x, 0, x + 1, 600))
    ImageDraw.Draw(page).rectangle((600, 280, 700, 300), fill=30)

    flat = docs_read._flatten_background(page)

    assert flat.getpixel((650, 290)) == 0
    assert flat.getpixel((50, 100)) == 255
    assert flat.getpixel((750, 100)) == 255


def _fake(replies: dict[str | None, str]) -> tuple[Any, list[str | None]]:
    calls: list[str | None] = []

    def image_to_string(image: object, config: str | None = None) -> str:
        calls.append(config)
        return replies.get(config, "")

    return SimpleNamespace(image_to_string=image_to_string), calls


def test_a_photo_that_reads_empty_is_retried_with_other_page_layouts() -> None:
    fake, calls = _fake({"--psm 11": "CORNER CAFE\nTOTAL 12.50 USD 2026-10-23"})

    text, passes, _ = docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)

    assert text.startswith("CORNER CAFE")
    assert calls == ["--psm 6", None, "--psm 6", "--psm 4", "--psm 11"]
    assert passes == 5


def test_a_photo_that_reads_fine_is_not_retried() -> None:
    fake, calls = _fake({"--psm 6": "CORNER CAFE\nTOTAL 12.50 USD 2026-10-23"})

    text, passes, _ = docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)

    assert text.startswith("CORNER CAFE")
    assert calls == ["--psm 6", None]
    assert passes == 2


def test_a_photo_with_no_text_reports_how_many_passes_found_nothing() -> None:
    fake, calls = _fake({})

    text, passes, _ = docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)

    assert text == ""
    assert passes == len(calls) == 7


def test_an_empty_photo_read_says_no_text_was_detected_and_needs_a_manual_reading(
    tmp_path: Path,
) -> None:
    photo = tmp_path / "till.jpg"
    photo.write_bytes(b"x")

    async def reader(**_kw: Any) -> dict[str, Any]:
        return {"pages": [{"index": 1, "ocr_passes": 7, "tables": [], "text": ""}]}

    reading = asyncio.run(read_receipt(photo, config=None, reader=reader))

    assert reading.status is ReceiptStatus.NEEDS_MANUAL
    assert "No text was detected in this photo after 7 OCR passes" in reading.message
    assert "manual reading" in reading.message


def test_a_photo_read_with_text_does_not_carry_the_pass_count(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "till.png"
    Image.new("RGB", (40, 40), "white").save(path)
    fake, _ = _fake({"--psm 6": "CORNER CAFE\nTOTAL 12.50 USD"})
    monkeypatch.setattr(docs_read, "_require_tesseract_binary", lambda: None)
    monkeypatch.setattr(docs_read, "_import_image_ocr_modules", lambda: (Image, fake))

    pages = docs_read._read_image(path)

    assert "ocr_passes" not in pages[0]


def test_a_bracketed_amount_seen_by_one_pass_is_kept_when_a_retry_reads_more() -> None:
    fake, _ = _fake({"--psm 6": "($12.50)", None: "12.50", "--psm 4": "TOTAL 12.50 VISA 4242"})

    text, _passes, _ = docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)

    assert "($12.50)" in text
    assert "TOTAL 12.50 VISA 4242" in text


def test_identical_reads_with_a_minus_are_not_doubled() -> None:
    fake, _ = _fake({"--psm 6": "TOTAL -12.50 USD CAFE", None: "TOTAL -12.50 USD CAFE"})

    text, _passes, _ = docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)

    assert text == "TOTAL -12.50 USD CAFE"


def test_tesseract_missing_during_a_retry_is_not_swallowed() -> None:
    class _Missing(Exception):
        pass

    calls = 0

    def image_to_string(image: object, config: str | None = None) -> str:
        nonlocal calls
        calls += 1
        if config == "--psm 4":
            raise _Missing("gone")
        return ""

    fake = SimpleNamespace(image_to_string=image_to_string, TesseractNotFoundError=_Missing)

    try:
        docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)
    except _Missing:
        return
    raise AssertionError("expected the missing binary to surface")


def test_a_sign_conflict_between_passes_is_flagged_and_makes_the_total_doubtful(
    tmp_path: Path,
) -> None:
    from blumkin.receipts import Confidence

    fake, _ = _fake({"--psm 6": "($12.50)", None: "12.50", "--psm 4": "TOTAL 12.50 VISA 4242"})
    text, _passes, conflict = docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)
    assert conflict is True
    photo = tmp_path / "till.jpg"
    photo.write_bytes(b"x")

    async def reader(**_kw: Any) -> dict[str, Any]:
        return {"pages": [{"index": 1, "text": text, "sign_conflict": conflict}]}

    reading = asyncio.run(read_receipt(photo, config=None, reader=reader))

    assert reading.fields.total.value == "12.50"
    assert reading.fields.total.confidence is Confidence.LOW
    assert "disagree about a minus sign" in reading.message


def test_reads_that_agree_do_not_raise_a_sign_conflict() -> None:
    fake, _ = _fake({"--psm 6": "TOTAL -12.50 USD CAFE", None: "TOTAL -12.50 USD CAFE"})

    _text, _passes, conflict = docs_read._ocr_photo(Image.new("RGB", (900, 700), "white"), fake)

    assert conflict is False


def test_every_pass_reads_the_photo_turned_the_way_its_exif_says() -> None:
    """A phone photo stored sideways reads as garbage unless it is turned upright first (#627)."""
    sideways = Image.new("L", (800, 600), 255)
    exif = sideways.getexif()
    exif[274] = 6  # EXIF orientation: turn 90 degrees clockwise to view
    sideways.info["exif"] = exif.tobytes()
    seen: list[tuple[int, int]] = []

    def image_to_string(image: Image.Image, config: str | None = None) -> str:
        seen.append(image.size)
        return ""

    docs_read._ocr_photo(sideways, SimpleNamespace(image_to_string=image_to_string))

    assert seen and all(width < height for width, height in seen)


def test_a_tip_table_does_not_make_a_larger_total() -> None:
    """The tip suggestions list totals the diner did not pay; the real total line may be garbled."""
    from blumkin.receipts import parse_receipt_text

    text = (
        "Subtotal $27.90\nSale tax (8.625%) $2.41\nTip $5.02\nTotalihit $35.33\n"
        "Tip Suggestions\n16% Tip:$4.46 Total: $39.79\n18% Tip:$5.02 Total: $40.35\n"
        "20% Tip:$5.58 tetal: $40.91\nPiha a 18 Ae Tip:$5.02 Total: $40.35\n"
    )

    fields = parse_receipt_text(text)

    assert fields.total.value == "35.33"


def test_one_stray_symbol_does_not_outvote_the_dollar_signs() -> None:
    from blumkin.receipts import parse_receipt_text

    text = "Sale tax #£Bt (8.625%) $2.41\nSubtotal $27.90\nTip $5.02\nCard $35.33\n"

    currency = parse_receipt_text(text).currency

    assert currency.value == "USD"
    assert "several currencies" in currency.note


def test_a_read_with_words_and_no_amount_is_tried_again_as_sparse_text() -> None:
    """An app screenshot reads as words but loses its amount, which only `--psm 11` finds (#627)."""
    prose = "Clipper card Virtual card replaced your card details for this purchase"
    fake, calls = _fake({None: prose, "--psm 6": prose, "--psm 11": prose + "\n$11.80"})

    text, passes, _ = docs_read._ocr_photo(_lined(), fake)

    assert "$11.80" in text and "--psm 11" in calls and passes == 3


def test_a_percentage_or_a_date_is_not_an_amount() -> None:
    for text in ("Sales tax 8.62%", "Date 10.28.2026", "Rate 8,62 %"):
        assert docs_read._AMOUNT.search(text) is None, text
    for text in ("Total $11.80", "11,80", "Card 35.33"):
        assert docs_read._AMOUNT.search(text) is not None, text


def test_a_real_total_line_that_mentions_a_tip_is_kept() -> None:
    from blumkin.receipts import _without_tip_table, parse_receipt_text

    for line in ("Total incl. 18% tip $45.00", "Tip: 0.00 Total: 35.00", "Tips 5.00  TOTAL 40.00"):
        assert _without_tip_table(f"Subtotal $30.00\n{line}\n") == f"Subtotal $30.00\n{line}\n"
    assert parse_receipt_text("Tip: 0.00 Total: 35.00\n").total.value == "35.00"


def test_one_printed_dollar_line_does_not_make_a_single_currency_receipt_doubtful() -> None:
    from blumkin.receipts import parse_receipt_text

    cad = parse_receipt_text("Total CA$45.00\nTax $2\n").currency
    eur = parse_receipt_text("Total €12,50\nApprox $13.50\nRate $1.08\n").currency

    assert (cad.value, cad.confidence.value) == ("CAD", "high")
    assert (eur.value, eur.confidence.value) == ("EUR", "high")


def test_amounts_with_thousands_or_a_minus_count_as_amounts() -> None:
    for text in ("Total 1,234.56", "Total 1.234,56", "Total -11.80", "Total 11.80"):
        assert docs_read._AMOUNT.search(text) is not None, text


def test_dollar_signs_do_not_outvote_an_explicit_currency_code() -> None:
    from blumkin.receipts import parse_receipt_text

    currency = parse_receipt_text(
        "Currency CAD\nSubtotal $27.90\nTax $2.41\nTotal $30.31\n"
    ).currency

    assert currency.value == "CAD"


def test_a_read_with_an_integer_total_is_not_beaten_by_one_with_only_a_tax_amount() -> None:
    assert docs_read._has_amount("TOTAL $12") and docs_read._has_amount("Tax $1.50")
    assert not docs_read._has_amount("Thank you, total pending")


def test_a_tip_table_with_the_percentage_after_the_word_tip_is_dropped() -> None:
    from blumkin.receipts import parse_receipt_text

    text = (
        "Subtotal $27.90\nTip $5.02\nTotalihit $35.33\n"
        "Suggested Tip 18%: $5.02 Total $40.35\nSuggested Tip 20%: $5.58 Total $40.91\n"
    )

    assert parse_receipt_text(text).total.value == "35.33"
