"""Receipts that must never read back a confident wrong value (issue #469 review)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.cli import main
from blumkin.receipts import (
    Confidence,
    ReceiptStatus,
    find_local_receipts,
    parse_receipt_text,
    read_receipt,
)

_TODAY = date(2026, 10, 24)


def _parse(text: str) -> Any:
    return parse_receipt_text(text, today=_TODAY)


@pytest.mark.parametrize(
    "text",
    [
        "Total 25.00 CAD (US$ 18.00)",
        "Total USD 25.00 EUR 23.00",
        "Total 25.00 / 18.00",
    ],
    ids=["foreign-echo", "two-currencies", "two-amounts"],
)
def test_a_total_line_with_two_amounts_is_not_a_confident_total(text: str) -> None:
    total = _parse(text).total
    assert total.confidence is not Confidence.HIGH
    assert total.value is None
    assert "amounts" in total.note


@pytest.mark.parametrize(
    "text",
    [
        "Total -25.00",
        "Total - 25.00",
        "Total −25.00",
        "Total (25.00)",
        "Total 25.00 CR",
        "Total 25.00-",
        "REFUND\nTotal 25.00",
        "Credit note\nTotal 25.00",
        "VOID\nTotal 25.00",
    ],
)
def test_a_refund_or_credit_total_is_not_a_confident_expense(text: str) -> None:
    total = _parse(text).total
    assert total.confidence is not Confidence.HIGH
    assert "refund" in total.note or "negative" in total.note


@pytest.mark.parametrize("text", ["Total: 1O0.00", "Total: l5.00", "Total 1234.5O", "Total: I5.00"])
def test_a_letter_touching_the_total_looks_like_ocr_noise_and_is_not_trusted(text: str) -> None:
    total = _parse(text).total
    assert total.confidence is not Confidence.HIGH
    assert "letter" in total.note


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Total 25.00 (incl 20% VAT)", "25.00"),
        ("Total USD 25.00", "25.00"),
        ("Total: $25.00", "25.00"),
        ("Total 25.00USD", "25.00"),
    ],
)
def test_unambiguous_totals_beside_other_numbers_stay_high(text: str, expected: str) -> None:
    total = _parse(text).total
    assert total.confidence is Confidence.HIGH, total
    assert total.value == expected


def test_a_return_policy_footer_does_not_make_a_normal_receipt_low() -> None:
    total = _parse("ACME\nTotal 25.00\nReturn policy: 30 days with receipt").total
    assert total.confidence is Confidence.HIGH
    assert total.value == "25.00"


def test_an_ambiguous_date_beside_a_clear_one_is_not_confident() -> None:
    field = _parse("Shop 03/04/2025\nDue 2025-05-01").date
    assert field.confidence is Confidence.LOW
    assert "could be" in field.note


def test_a_statement_period_date_alone_is_not_taken_as_the_purchase_date() -> None:
    field = _parse("Statement period ending 2026-09-30").date
    assert field.confidence is Confidence.LOW
    assert "labelled" in field.note


def test_a_printed_date_that_differs_from_the_sale_date_is_not_confident() -> None:
    field = _parse("Sale 2026-10-20\nPrinted 2026-10-22").date
    assert field.confidence is Confidence.LOW
    assert "labelled" in field.note


def test_a_printed_date_matching_the_sale_date_stays_high() -> None:
    field = _parse("Sale 2026-10-20\nPrinted 2026-10-20").date
    assert field.confidence is Confidence.HIGH
    assert field.value == "2026-10-20"


def test_a_currency_code_far_from_any_amount_is_not_confident() -> None:
    field = _parse("Shop Visa EUR Tower\nTotal 25.00").currency
    assert field.confidence is not Confidence.HIGH
    assert "not next to an amount" in field.note


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Total 25.00 usd", "USD"),
        ("Total 25.00 EURO", "EUR"),
        ("Total CA$25.00", "CAD"),
        ("Total R$25.00", "BRL"),
        ("Total US$18.00", "USD"),
        ("Total AU$18.00", "AUD"),
        ("Currency: GBP\nTotal 5.00", "GBP"),
    ],
)
def test_currency_spellings_beside_an_amount_are_recognised(text: str, code: str) -> None:
    field = _parse(text).currency
    assert field.value == code
    assert field.confidence is Confidence.HIGH, field


@pytest.mark.parametrize("error", [RuntimeError("SECRET-TEXT"), KeyError("SECRET-TEXT")])
def test_read_receipt_never_raises_and_never_echoes_the_error(
    error: Exception, tmp_path: Path
) -> None:
    receipt = tmp_path / "r.png"
    receipt.write_bytes(b"x")

    async def broken(**kwargs: Any) -> dict[str, Any]:
        raise error

    reading = asyncio.run(read_receipt(receipt, config=None, reader=broken))
    assert reading.status is ReceiptStatus.NEEDS_MANUAL
    assert type(error).__name__ in reading.message
    assert "SECRET-TEXT" not in reading.message


class _VanishingEntry:
    name = "gone.png"
    path = "/nonexistent/gone.png"

    def is_file(self) -> bool:
        return True

    def is_symlink(self) -> bool:
        return False

    def stat(self) -> os.stat_result:
        raise FileNotFoundError(self.path)


class _Entries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def __enter__(self) -> Iterator[Any]:
        return iter(self._entries)

    def __exit__(self, *exc: object) -> None:
        return None


def test_find_skips_a_file_that_vanishes_during_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.receipts.os.scandir", lambda folder: _Entries([_VanishingEntry()]))
    assert find_local_receipts(tmp_path) == []


@pytest.fixture
def _los_angeles(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_find_since_is_a_utc_date_like_the_modified_times_it_prints(
    tmp_path: Path, _los_angeles: None
) -> None:
    receipt = tmp_path / "late.png"
    receipt.write_bytes(b"x")
    # 2030-01-01T00:30:00Z is still December 31 in Los Angeles.
    os.utime(receipt, (1_893_457_800, 1_893_457_800))
    result = CliRunner().invoke(
        main, ["--json", "receipts", "find", "--dir", str(tmp_path), "--since", "2030-01-01"]
    )
    assert result.exit_code == 0, result.output
    assert '"name": "late.png"' in result.output
