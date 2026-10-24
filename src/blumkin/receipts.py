"""Receipt intake for expense tasks (issue #469).

Finds receipt files (images and PDFs) in a local directory, reads them with the existing
`docs read` OCR path and parses vendor, date, currency and total, reporting how confident
each reading is. A wrong amount in an expense report is worse than an unread one, so the
parser is conservative: a field is `high` confidence only when the receipt says it
unambiguously, and anything doubtful (two different totals, an amount that could be
thousands or decimals, a two-digit year, mixed currencies) comes back `low` with a note
saying why, or with no value at all, so a task asks the user instead of guessing.

Receipt contents are private: nothing here logs a vendor or an amount, and the OCR text is
scanned for prompt injection by `docs read` before it gets here.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from blumkin.config import BlumkinConfig
from blumkin.skills.docs_read import (
    DocsReadExtraMissingError,
    DocsReadFileNotFoundError,
    DocsReadOcrUnavailableError,
    DocsReadOversizeError,
    docs_read,
)

DEFAULT_FIND_LIMIT = 50
RECEIPT_EXTENSIONS = frozenset({".bmp", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff", ".webp"})


class Confidence(StrEnum):
    HIGH = "high"
    LOW = "low"
    NONE = "none"


class ReceiptStatus(StrEnum):
    NEEDS_MANUAL = "needs_manual"
    OK = "ok"
    UNSUPPORTED = "unsupported"


class ReceiptsDirError(ValueError):
    """The receipts directory is missing or not a directory."""


@dataclass(frozen=True, slots=True)
class ReceiptField:
    confidence: Confidence
    value: str | None
    note: str = ""

    def to_payload(self) -> dict[str, str | None]:
        return {"confidence": self.confidence.value, "note": self.note, "value": self.value}


@dataclass(frozen=True, slots=True)
class ReceiptFields:
    currency: ReceiptField
    date: ReceiptField
    total: ReceiptField
    vendor: ReceiptField

    def to_payload(self) -> dict[str, dict[str, str | None]]:
        return {
            "currency": self.currency.to_payload(),
            "date": self.date.to_payload(),
            "total": self.total.to_payload(),
            "vendor": self.vendor.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class ReceiptFile:
    modified_epoch: float
    name: str
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class ReceiptReading:
    fields: ReceiptFields
    injection_warning: dict[str, Any] | None
    message: str
    ocr_used: bool
    path: str
    status: ReceiptStatus

    def to_payload(self) -> dict[str, Any]:
        return {
            "fields": self.fields.to_payload(),
            "injection_warning": self.injection_warning,
            "message": self.message,
            "ocr_used": self.ocr_used,
            "ok": self.status is ReceiptStatus.OK,
            "path": self.path,
            "status": self.status.value,
        }


def find_local_receipts(
    directory: Path,
    *,
    limit: int = DEFAULT_FIND_LIMIT,
    since_epoch: float | None = None,
) -> list[ReceiptFile]:
    """Receipt files directly inside `directory`, newest first (no recursion, no symlinks)."""
    folder = directory.expanduser()
    if not folder.is_dir():
        raise ReceiptsDirError(f"Expected an existing directory, got {str(directory)!r}")
    found: list[ReceiptFile] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_file():
                continue
            if Path(entry.name).suffix.lower() not in RECEIPT_EXTENSIONS:
                continue
            info = entry.stat()
            if since_epoch is not None and info.st_mtime < since_epoch:
                continue
            found.append(
                ReceiptFile(
                    modified_epoch=info.st_mtime,
                    name=entry.name,
                    path=Path(entry.path),
                    size=info.st_size,
                )
            )
    found.sort(key=lambda item: (-item.modified_epoch, item.name))
    return found[:limit]


def parse_receipt_text(text: str, *, today: date | None = None) -> ReceiptFields:
    """Parse vendor, date, currency and total out of OCR or PDF text."""
    clean = _ascii_digits(text)
    return ReceiptFields(
        currency=_parse_currency(clean),
        date=_parse_date(clean, today or date.today()),
        total=_parse_total(clean),
        vendor=_parse_vendor(clean),
    )


async def read_receipt(
    path: Path,
    *,
    config: BlumkinConfig | None,
    reader: Callable[..., Awaitable[dict[str, Any]]] = docs_read,
) -> ReceiptReading:
    """Read one receipt file and parse its fields; never raises for an unreadable receipt."""
    suffix = path.suffix.lower()
    if suffix not in RECEIPT_EXTENSIONS:
        return _manual(
            path,
            ReceiptStatus.UNSUPPORTED,
            f"Expected an image or PDF receipt, got {suffix or 'no extension'!r}",
        )
    try:
        payload = await reader(config=config, path=str(path), ocr=False)
        text = _joined_text(payload)
        if suffix == ".pdf" and not text.strip():
            payload = await reader(config=config, path=str(path), ocr=True)
            text = _joined_text(payload)
    except (DocsReadExtraMissingError, DocsReadOcrUnavailableError) as exc:
        return _manual(
            path,
            ReceiptStatus.NEEDS_MANUAL,
            f"OCR is not available ({exc}); install the ocr extra "
            "(`uv tool install -e '.[pdf,ocr]'`, plus tesseract and poppler) or tell me the "
            "vendor, date and total.",
        )
    except DocsReadOversizeError as exc:
        return _manual(
            path,
            ReceiptStatus.NEEDS_MANUAL,
            f"This receipt is too large to read ({exc}); tell me the vendor, date and total.",
        )
    except (DocsReadFileNotFoundError, OSError, ValueError) as exc:
        return _manual(
            path,
            ReceiptStatus.NEEDS_MANUAL,
            f"This receipt could not be read ({type(exc).__name__}); "
            "tell me the vendor, date and total.",
        )
    if not text.strip():
        return _manual(
            path,
            ReceiptStatus.NEEDS_MANUAL,
            "No text could be read from this receipt; tell me the vendor, date and total.",
        )
    return ReceiptReading(
        fields=parse_receipt_text(text),
        injection_warning=payload.get("injection_warning"),
        message="",
        ocr_used=bool(payload.get("ocr_used")),
        path=str(path),
        status=ReceiptStatus.OK,
    )


@dataclass(frozen=True, slots=True)
class _Amount:
    #: The normalized "1234.50" form, or None when the token cannot be read with certainty.
    value: str | None
    #: True when the receipt wrote a decimal part ("12.00"); an integer may have lost its point.
    has_decimals: bool
    note: str = ""


_AMOUNT_RUN = re.compile("[0-9](?:[0-9 .,'\u2019\u00a0\u202f]*[0-9])?")
_BIDI_AND_ZERO_WIDTH = re.compile("[\u061c\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_CURRENCY_CODE = re.compile(
    r"(?<![A-Za-z])(AUD|BRL|CAD|CHF|CNY|DKK|EUR|GBP|INR|JPY|MXN|NOK|NZD|SEK|USD)(?![A-Za-z])"
)
_CURRENCY_SYMBOLS = {"\u00a3": "GBP", "\u20ac": "EUR", "\u20b9": "INR"}
_DATE_DAY_MONTH = re.compile(
    r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r",?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_NUMERIC = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b")
_GROUPED_INTEGER = re.compile("\\d{1,3}(?P<sep>[ .,'\u2019\u00a0\u202f])\\d{3}(?:(?P=sep)\\d{3})*")
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_MONTH_LABEL = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}
_INCLUDING = re.compile(r"\b(?:inc|incl|including|inclusive)\b")
_MAX_AMOUNT_RUN = 30
_NOT_A_TOTAL = re.compile(
    r"\b(?:cash|change|discount|gratuity|item|items|paid|point|points|qty|reward|rewards|"
    r"saving|savings|sub\s*-?\s*total|tender|tendered|tip)\b"
)
_SEPARATORS = " .,'\u2019\u00a0\u202f"
_TAX_WORDS = re.compile(r"\b(?:gst|hst|tax|vat)\b")
_TOTAL_LINE = re.compile(
    r"\b(?:amount\s+due|balance\s+due|grand\s+total|total\s+amount|total(?:\s+due)?)\b"
)
_VENDOR_SKIP = re.compile(
    r"receipt|invoice|order|tel\b|phone|www\.|http|@|thank|welcome|\d{3}[-. ]\d{3,4}|"
    r"^\s*(?:amex|cash|change|mastercard|sub\s*-?\s*total|tax|total|visa)\b|\d[.,]\d{2}\b",
    re.IGNORECASE,
)


def _ascii_digits(text: str) -> str:
    """Normalize the text for parsing: other scripts' digits become ASCII, NFKC, and the
    invisible bidirectional and zero-width controls are removed."""
    out: list[str] = []
    for char in unicodedata.normalize("NFKC", _BIDI_AND_ZERO_WIDTH.sub("", text)):
        if unicodedata.category(char) == "Nd":
            out.append(str(unicodedata.decimal(char)))
        elif char == "٫":
            out.append(".")
        elif char == "٬":
            out.append(",")
        else:
            out.append(char)
    return "".join(out)


def _manual(path: Path, status: ReceiptStatus, message: str) -> ReceiptReading:
    return ReceiptReading(
        fields=_nothing(),
        injection_warning=None,
        message=message,
        ocr_used=False,
        path=str(path),
        status=status,
    )


def _joined_text(payload: dict[str, Any]) -> str:
    return "\n".join(str(page.get("text") or "") for page in payload.get("pages") or [])


def _nothing() -> ReceiptFields:
    empty = ReceiptField(confidence=Confidence.NONE, value=None)
    return ReceiptFields(currency=empty, date=empty, total=empty, vendor=empty)


def _parse_amount(token: str) -> _Amount:
    """Read one run of digits and separators, refusing anything that is not unambiguous."""
    cleaned = token.strip(_SEPARATORS)
    if not cleaned or len(cleaned) > _MAX_AMOUNT_RUN:
        return _Amount(value=None, has_decimals=False, note="unrecognised amount")
    unrecognised = _Amount(value=None, has_decimals=False, note=f"unrecognised amount {cleaned!r}")
    fraction = ""
    whole = cleaned
    trailing = re.search(r"([.,])(\d{1,2})$", cleaned)
    if trailing:
        whole, fraction = cleaned[: trailing.start()], trailing.group(2)
    if re.fullmatch(r"\d+", whole):
        grouped = False
    else:
        match = _GROUPED_INTEGER.fullmatch(whole)
        if not match:
            return unrecognised
        grouped = True
        if trailing and match.group("sep") == trailing.group(1):
            return unrecognised
        if not trailing and match.group("sep") in ".," and whole.count(match.group("sep")) == 1:
            digits = re.sub(r"\D", "", whole)
            return _Amount(
                value=None,
                has_decimals=False,
                note=f"{cleaned} could be {digits} or {whole.replace(match.group('sep'), '.')}",
            )
    del grouped
    digits = re.sub(r"\D", "", whole)
    amount = Decimal(f"{digits}.{fraction}" if fraction else digits)
    return _Amount(value=f"{amount:.2f}", has_decimals=bool(fraction))


def _parse_currency(text: str) -> ReceiptField:
    found: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in _CURRENCY_CODE.finditer(text)]
    for symbol, code in _CURRENCY_SYMBOLS.items():
        found.extend((m.start(), code) for m in re.finditer(re.escape(symbol), text))
    codes: list[str] = []
    for _, code in sorted(found):
        if code not in codes:
            codes.append(code)
    if len(codes) > 1:
        return ReceiptField(
            confidence=Confidence.LOW,
            value=codes[0],
            note=f"several currencies: {', '.join(codes)}",
        )
    if codes:
        return ReceiptField(confidence=Confidence.HIGH, value=codes[0])
    if "\u00a5" in text:
        return ReceiptField(
            confidence=Confidence.LOW, value=None, note="\u00a5 could be JPY or CNY"
        )
    if "$" in text:
        return ReceiptField(confidence=Confidence.LOW, value="USD", note="$ may also be CAD or AUD")
    return ReceiptField(confidence=Confidence.NONE, value=None)


def _parse_date(text: str, today: date) -> ReceiptField:
    valid: list[tuple[int, str, bool]] = []
    ambiguous: list[str] = []

    def add(position: int, year: int, month: int, day: int, *, two_digit: bool = False) -> None:
        try:
            valid.append((position, date(year, month, day).isoformat(), two_digit))
        except ValueError:
            return

    for match in _DATE_ISO.finditer(text):
        add(match.start(), int(match.group(1)), int(match.group(2)), int(match.group(3)))
    for match in _DATE_MONTH_DAY.finditer(text):
        add(
            match.start(), int(match.group(3)), _MONTHS[match.group(1).lower()], int(match.group(2))
        )
    for match in _DATE_DAY_MONTH.finditer(text):
        add(
            match.start(), int(match.group(3)), _MONTHS[match.group(2).lower()], int(match.group(1))
        )
    for match in _DATE_NUMERIC.finditer(text):
        first, second, year_text = int(match.group(1)), int(match.group(2)), match.group(3)
        two_digit = len(year_text) == 2
        year = int(year_text)
        if two_digit:
            year += 2000 if year <= today.year % 100 + 1 else 1900
        if first > 12 or first == second:
            add(match.start(), year, second, first, two_digit=two_digit)
        elif second > 12:
            add(match.start(), year, first, second, two_digit=two_digit)
        elif 1 <= first <= 12 and 1 <= second <= 12:
            ambiguous.append(
                f"{match.group(0)} could be {_MONTH_LABEL[first]} {second} "
                f"or {_MONTH_LABEL[second]} {first}"
            )
    if not valid:
        if ambiguous:
            return ReceiptField(confidence=Confidence.LOW, value=None, note=ambiguous[0])
        return ReceiptField(confidence=Confidence.NONE, value=None)
    valid.sort()
    _, first_iso, first_two_digit = valid[0]
    distinct = list(dict.fromkeys(iso for _, iso, _ in valid))
    notes: list[str] = []
    if first_two_digit:
        notes.append("the receipt gives a two-digit year")
    if len(distinct) > 1:
        notes.append(f"several dates: {', '.join(distinct)}")
    first = date.fromisoformat(first_iso)
    if first > today + timedelta(days=1):
        notes.append("the date is in the future")
    if first < today - timedelta(days=3652):
        notes.append("the date looks old (more than ten years)")
    if notes:
        return ReceiptField(confidence=Confidence.LOW, value=first_iso, note="; ".join(notes))
    return ReceiptField(confidence=Confidence.HIGH, value=first_iso)


def _parse_total(text: str) -> ReceiptField:
    keyed: list[_Amount] = []
    for raw in text.splitlines():
        line = raw.casefold()
        if not _TOTAL_LINE.search(line) or _NOT_A_TOTAL.search(line):
            continue
        if _TAX_WORDS.search(line) and not _INCLUDING.search(line):
            continue
        runs = _AMOUNT_RUN.findall(raw)
        if runs:
            keyed.append(_parse_amount(runs[-1]))
    if not keyed:
        return _guess_total(text)
    valid = [amount for amount in keyed if amount.value is not None]
    if not valid:
        ambiguous = next((a for a in keyed if "could be" in a.note), None)
        shown = ambiguous or keyed[0]
        return ReceiptField(
            confidence=Confidence.LOW if ambiguous else Confidence.NONE,
            value=None,
            note=shown.note,
        )
    values = sorted({amount.value for amount in valid if amount.value is not None}, key=Decimal)
    if len(values) > 1:
        return ReceiptField(
            confidence=Confidence.LOW,
            value=values[-1],
            note=f"several total lines: {', '.join(values)}",
        )
    if len(valid) < len(keyed):
        problem = next(a.note for a in keyed if a.value is None)
        return ReceiptField(confidence=Confidence.LOW, value=values[0], note=problem)
    if not all(amount.has_decimals for amount in valid):
        return ReceiptField(
            confidence=Confidence.LOW,
            value=values[0],
            note="the total has no decimal part; check the decimal point was not lost",
        )
    return ReceiptField(confidence=Confidence.HIGH, value=values[0])


def _guess_total(text: str) -> ReceiptField:
    amounts = [_parse_amount(run) for run in _AMOUNT_RUN.findall(text)]
    guesses = [a.value for a in amounts if a.value is not None and a.has_decimals]
    if not guesses:
        return ReceiptField(confidence=Confidence.NONE, value=None)
    return ReceiptField(
        confidence=Confidence.LOW,
        value=max(guesses, key=Decimal),
        note="no total line found; this is the largest amount on the receipt",
    )


def _parse_vendor(text: str) -> ReceiptField:
    for raw in text.splitlines():
        line = raw.strip()
        letters = sum(char.isalpha() for char in line)
        if not 2 <= len(line) <= 60 or letters < 2 or letters / len(line) < 0.6:
            continue
        if _VENDOR_SKIP.search(line):
            continue
        return ReceiptField(confidence=Confidence.LOW, value=line)
    return ReceiptField(confidence=Confidence.NONE, value=None)
