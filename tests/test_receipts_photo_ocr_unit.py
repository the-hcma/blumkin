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
