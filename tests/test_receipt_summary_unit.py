"""Group receipts into trips, drop without-tip copies and total what would be filed (#584)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from blumkin.receipt_summary import SummaryInput, summarize_receipts


def _item(
    name: str,
    *,
    vendor: str | None = "Ride Co",
    date: str | None = "2026-10-23",
    total: str | None = "18.50",
    currency: str | None = "USD",
    date_confidence: str = "high",
    subject: str | None = None,
    received: str | None = None,
    message_id: str | None = None,
) -> SummaryInput:
    return SummaryInput(
        currency=currency,
        date=date,
        date_confidence=date_confidence,
        message_id=message_id,
        path=Path(f"/r/{name}"),
        received=received,
        subject=subject,
        total=total,
        vendor=vendor,
    )


def _actions(summary: dict[str, Any]) -> dict[str, str]:
    return {Path(row["path"]).name: row["action"] for row in summary["rows"]}


def test_the_with_tip_copy_is_filed_and_the_without_tip_copy_is_dropped() -> None:
    summary = summarize_receipts(
        [_item("before.pdf", total="18.50"), _item("after.pdf", total="21.00")]
    )

    assert _actions(summary) == {"before.pdf": "drop", "after.pdf": "file"}
    dropped = next(row for row in summary["rows"] if row["action"] == "drop")
    assert "after.pdf" in dropped["reason"]
    assert "without-tip" in dropped["reason"]
    assert summary["grand_totals"] == {"USD": "21.00"}


def test_an_exact_duplicate_is_dropped_and_the_first_one_is_kept() -> None:
    summary = summarize_receipts([_item("a.pdf"), _item("b.pdf")])

    assert _actions(summary) == {"a.pdf": "file", "b.pdf": "drop"}
    assert "duplicate" in summary["rows"][1]["reason"]


def test_two_rides_the_same_day_with_far_apart_totals_are_both_filed() -> None:
    summary = summarize_receipts([_item("am.pdf", total="9.00"), _item("pm.pdf", total="31.00")])

    assert _actions(summary) == {"am.pdf": "file", "pm.pdf": "file"}
    assert summary["grand_totals"] == {"USD": "40.00"}


def test_different_vendors_dates_and_currencies_are_never_grouped() -> None:
    summary = summarize_receipts(
        [
            _item("a.pdf"),
            _item("b.pdf", vendor="Other Co"),
            _item("c.pdf", date="2026-10-24", total="19.00"),
            _item("d.pdf", currency="EUR", total="19.00"),
        ]
    )

    assert set(_actions(summary).values()) == {"file"}
    assert summary["grand_totals"] == {"EUR": "19.00", "USD": "56.00"}


def test_vendor_names_match_ignoring_case_and_punctuation() -> None:
    summary = summarize_receipts(
        [_item("a.pdf", vendor="RIDE CO."), _item("b.pdf", vendor="ride co", total="20.00")]
    )

    assert _actions(summary) == {"a.pdf": "drop", "b.pdf": "file"}


def test_a_receipt_missing_a_field_needs_review_and_is_not_totalled() -> None:
    summary = summarize_receipts([_item("a.pdf"), _item("b.jpg", total=None)])

    assert _actions(summary) == {"a.pdf": "file", "b.jpg": "review"}
    assert summary["needs_review"] == 1
    assert summary["ok"] is False
    assert summary["grand_totals"] == {"USD": "18.50"}


def test_a_low_confidence_date_is_taken_from_the_subject_not_the_arrival_time() -> None:
    summary = summarize_receipts(
        [
            _item(
                "late.pdf",
                date=None,
                date_confidence="none",
                subject="Your ride on 2026-10-23",
                received="2026-10-24T01:15:00Z",
            )
        ]
    )

    row = summary["rows"][0]
    assert row["date"] == "2026-10-23"
    assert row["date_source"] == "subject"
    assert row["action"] == "file"


def test_a_date_from_the_arrival_time_alone_is_flagged_for_review() -> None:
    summary = summarize_receipts(
        [_item("late.pdf", date=None, date_confidence="none", received="2026-10-24T01:15:00Z")]
    )

    row = summary["rows"][0]
    assert row["date"] == "2026-10-24"
    assert row["date_source"] == "received"
    assert row["action"] == "review"
    assert "arrival" in row["reason"]


def test_the_totals_are_kept_per_currency_and_added_exactly() -> None:
    summary = summarize_receipts(
        [
            _item("a.pdf", vendor="A", total="0.10"),
            _item("b.pdf", vendor="B", total="0.20"),
            _item("c.pdf", vendor="C", currency="EUR", total="5.00"),
        ]
    )

    assert summary["grand_totals"] == {"EUR": "5.00", "USD": "0.30"}
    assert summary["filing"] == 3
    assert summary["dropping"] == 0


def test_an_unparseable_total_needs_review_instead_of_crashing() -> None:
    summary = summarize_receipts([_item("a.pdf", total="1,5x")])

    assert _actions(summary) == {"a.pdf": "review"}
