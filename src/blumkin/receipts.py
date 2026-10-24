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
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                receipt = _receipt_file(entry, since_epoch)
                if receipt is not None:
                    found.append(receipt)
    except OSError as exc:
        raise ReceiptsDirError(
            f"Expected a readable directory, got {type(exc).__name__} for {str(directory)!r}"
        ) from exc
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
        blank_pages = _blank_pages(payload)
        if suffix == ".pdf" and blank_pages:
            try:
                payload = await reader(config=config, path=str(path), ocr=True)
                text = _joined_text(payload)
                blank_pages = _blank_pages(payload)
            except DocsReadOcrUnavailableError, DocsReadExtraMissingError:
                if not text.strip():
                    raise
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
    except Exception as exc:  # noqa: BLE001 - OCR and PDF libraries raise many types; a bad receipt never tracebacks
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
    fields = parse_receipt_text(text)
    message = ""
    if blank_pages:
        total_pages = len(payload.get("pages") or [])
        message = (
            f"{len(blank_pages)} of {total_pages} pages had no text and could not be read, so "
            "fields may be missing; check every amount against the receipt."
        )
        fields = _distrust(fields, message)
    return ReceiptReading(
        fields=fields,
        injection_warning=payload.get("injection_warning"),
        message=message,
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
    #: True when the line itself is doubtful (two amounts, a refund, OCR noise); the note says why.
    doubtful: bool = False


_AMOUNT_RUN = re.compile("[0-9](?:[0-9 .,'\u2019\u00a0\u202f]*[0-9])?")
_BIDI_AND_ZERO_WIDTH = re.compile("[\u061c\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_CODE_WORDS = "AUD|BRL|CAD|CHF|CNY|DKK|EURO|EUR|GBP|INR|JPY|MXN|NOK|NZD|SEK|USD"
_CODE_ALIASES = {"EURO": "EUR"}
_CURRENCY_CODE = re.compile(rf"(?<![A-Za-z])({_CODE_WORDS})(?![A-Za-z])")
_CURRENCY_LABELLED = re.compile(
    rf"\bcurrency\b[^\n]{{0,12}}?(?<![A-Za-z])({_CODE_WORDS})(?![A-Za-z])", re.IGNORECASE
)
_CURRENCY_NEAR_AMOUNT = re.compile(
    rf"(?<![A-Za-z])({_CODE_WORDS})(?![A-Za-z])\s*[-\u2212]?\s*\d|\d\s*({_CODE_WORDS})(?![A-Za-z])",
    re.IGNORECASE,
)
_CURRENCY_PREFIXED = re.compile(r"(?<![A-Za-z])(US|CA|C|AU|A|NZ|R|MX)\$")
_CURRENCY_PREFIX_TO_CODE = {
    "A": "AUD",
    "AU": "AUD",
    "C": "CAD",
    "CA": "CAD",
    "MX": "MXN",
    "NZ": "NZD",
    "R": "BRL",
    "US": "USD",
}
_CURRENCY_SYMBOLS = {"\u00a3": "GBP", "\u20ac": "EUR", "\u20b9": "INR"}
_DATE_LABEL = re.compile(
    r"\b(?:due|printed|period|statement|expir\w*|valid\s+(?:until|through|to)|pay\s+by)\b",
    re.IGNORECASE,
)
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
_NEGATIVE_AMOUNT = re.compile(
    r"[-\u2212\u2013\u2014]\s*[^\d\s]{0,3}\s*\d|\(\s*[^\d\s()]{0,3}\s*\d[\d .,]*\)|"
    r"\d\s*[-\u2212]\s*$|\b(?:cr|credit)\b"
)
_NOISE_LETTERS = "OoIlSBZ|"
_REFUND_WORDS = re.compile(r"\b(?:credit\s+(?:memo|note)|refund(?:ed)?|reversal|void(?:ed)?)\b")
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
    found: list[tuple[int, str]] = [
        (m.start(), _CODE_ALIASES.get(m.group(1), m.group(1)))
        for m in _CURRENCY_CODE.finditer(text)
    ]
    near: set[str] = set()
    for match in _CURRENCY_NEAR_AMOUNT.finditer(text):
        code = (match.group(1) or match.group(2)).upper()
        near.add(_CODE_ALIASES.get(code, code))
    for match in _CURRENCY_LABELLED.finditer(text):
        code = match.group(1).upper()
        near.add(_CODE_ALIASES.get(code, code))
    for match in _CURRENCY_PREFIXED.finditer(text):
        code = _CURRENCY_PREFIX_TO_CODE[match.group(1)]
        found.append((match.start(), code))
        near.add(code)
    for symbol, code in _CURRENCY_SYMBOLS.items():
        for match in re.finditer(re.escape(symbol), text):
            found.append((match.start(), code))
            near.add(code)
    found.extend((0, code) for code in sorted(near) if all(code != seen for _, seen in found))
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
        if codes[0] in near:
            return ReceiptField(confidence=Confidence.HIGH, value=codes[0])
        return ReceiptField(
            confidence=Confidence.LOW,
            value=codes[0],
            note=f"{codes[0]} appears but not next to an amount",
        )
    if "\u00a5" in text:
        return ReceiptField(
            confidence=Confidence.LOW, value=None, note="\u00a5 could be JPY or CNY"
        )
    if "$" in text:
        return ReceiptField(confidence=Confidence.LOW, value="USD", note="$ may also be CAD or AUD")
    return ReceiptField(confidence=Confidence.NONE, value=None)


def _parse_date(text: str, today: date) -> ReceiptField:
    valid: list[tuple[int, str, bool]] = []
    labelled_isos: set[str] = set()
    ambiguous: list[str] = []

    def add(position: int, year: int, month: int, day: int, *, two_digit: bool = False) -> None:
        try:
            iso = date(year, month, day).isoformat()
        except ValueError:
            return
        line_start = text.rfind("\n", 0, position) + 1
        line_end = text.find("\n", position)
        line = text[line_start : line_end if line_end >= 0 else len(text)]
        if _DATE_LABEL.search(line):
            labelled_isos.add(iso)
            return
        valid.append((position, iso, two_digit))

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
    if not valid and labelled_isos:
        first_labelled = min(labelled_isos)
        notes = ["the only date is labelled due, printed or period; check it is the purchase date"]
        if len(labelled_isos) > 1:
            notes.append(f"several dates: {', '.join(sorted(labelled_isos))}")
        notes.extend(ambiguous[:1])
        return ReceiptField(confidence=Confidence.LOW, value=first_labelled, note="; ".join(notes))
    if not valid:
        if ambiguous:
            return ReceiptField(confidence=Confidence.LOW, value=None, note=ambiguous[0])
        return ReceiptField(confidence=Confidence.NONE, value=None)
    valid.sort()
    _, first_iso, first_two_digit = valid[0]
    distinct = list(dict.fromkeys(iso for _, iso, _ in valid))
    notes: list[str] = []
    if ambiguous:
        notes.append(ambiguous[0])
    if not labelled_isos <= set(distinct):
        notes.append("a date labelled due, printed or period differs from the purchase date")
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
        keyword = _TOTAL_LINE.search(line)
        if keyword is None or _NOT_A_TOTAL.search(line):
            continue
        if _TAX_WORDS.search(line) and not _INCLUDING.search(line):
            continue
        amount = _total_on_line(raw, line[keyword.end() :])
        if amount is not None:
            keyed.append(amount)
    refund = _REFUND_WORDS.search(text.casefold())
    if not keyed:
        return _refund_check(_guess_total(text), refund is not None)
    doubtful = [amount for amount in keyed if amount.doubtful]
    if doubtful:
        return ReceiptField(
            confidence=Confidence.LOW,
            value=None,
            note="; ".join(dict.fromkeys(amount.note for amount in doubtful)),
        )
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
    return _refund_check(
        ReceiptField(confidence=Confidence.HIGH, value=values[0]), refund is not None
    )


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


def _refund_check(field: ReceiptField, refund: bool) -> ReceiptField:
    """A receipt that mentions a refund, credit or void is a negative amount until proven not."""
    if not refund:
        return field
    return ReceiptField(
        confidence=Confidence.LOW,
        value=None,
        note="the receipt mentions a refund, credit or void; this may be a negative amount",
    )


def _total_on_line(raw: str, after_keyword: str) -> _Amount | None:
    """The amount on one total line, or a doubtful marker when the line cannot be trusted."""
    found = [(match, _parse_amount(match.group())) for match in _AMOUNT_RUN.finditer(raw)]
    if not found:
        return None
    with_decimals = [(match, amount) for match, amount in found if amount.has_decimals]
    chosen_match, chosen = with_decimals[0] if with_decimals else found[-1]
    values = {amount.value for _, amount in with_decimals}
    if len(values) > 1:
        return _Amount(
            value=None,
            has_decimals=True,
            note="the total line holds several amounts; check which one is the total",
            doubtful=True,
        )
    before = raw[chosen_match.start() - 1] if chosen_match.start() > 0 else ""
    after = raw[chosen_match.end()] if chosen_match.end() < len(raw) else ""
    if any(char and char in _NOISE_LETTERS for char in (before, after)):
        return _Amount(
            value=None,
            has_decimals=True,
            note="a letter touches the total (OCR may have misread a digit); check the amount",
            doubtful=True,
        )
    if _NEGATIVE_AMOUNT.search(after_keyword):
        return _Amount(
            value=None,
            has_decimals=True,
            note="the total looks negative (a minus sign, brackets or CR); this may be a refund",
            doubtful=True,
        )
    return chosen


def _blank_pages(payload: dict[str, Any]) -> list[int]:
    """Indexes of the pages that came back without any text."""
    return [
        position
        for position, page in enumerate(payload.get("pages") or [])
        if not str(page.get("text") or "").strip()
    ]


def _distrust(fields: ReceiptFields, note: str) -> ReceiptFields:
    """Nothing read from a receipt with unreadable pages is better than `low` confidence."""

    def lower(field: ReceiptField) -> ReceiptField:
        if field.confidence is not Confidence.HIGH:
            return field
        return ReceiptField(confidence=Confidence.LOW, value=field.value, note=note)

    return ReceiptFields(
        currency=lower(fields.currency),
        date=lower(fields.date),
        total=lower(fields.total),
        vendor=fields.vendor,
    )


def _receipt_file(entry: os.DirEntry[str], since_epoch: float | None) -> ReceiptFile | None:
    """One directory entry as a receipt file, or None when it is not one (or cannot be read)."""
    try:
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_file():
            return None
        if Path(entry.name).suffix.lower() not in RECEIPT_EXTENSIONS:
            return None
        info = entry.stat()
    except OSError:
        return None  # removed or unreadable while scanning
    if since_epoch is not None and info.st_mtime < since_epoch:
        return None
    return ReceiptFile(
        modified_epoch=info.st_mtime, name=entry.name, path=Path(entry.path), size=info.st_size
    )
