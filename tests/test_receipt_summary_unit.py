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
    subject: str | None = "Your ride",
    received: str | None = "2026-10-23T08:00:00Z",
    total_confidence: str = "high",
    currency_confidence: str = "high",
    message_id: str | None = None,
    sha256: str | None = None,
) -> SummaryInput:
    return SummaryInput(
        currency=currency,
        currency_confidence=currency_confidence,
        date=date,
        date_confidence=date_confidence,
        message_id=message_id,
        path=Path(f"/r/{name}"),
        received=received,
        sha256=sha256,
        subject=subject,
        total=total,
        total_confidence=total_confidence,
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
    summary = summarize_receipts([_item("a.pdf", sha256="1" * 64), _item("b.pdf", sha256="1" * 64)])

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


def test_similar_totals_with_nothing_tying_them_together_are_reviewed_not_dropped() -> None:
    summary = summarize_receipts(
        [
            _item("am.pdf", total="12.00", subject="Morning trip", received="2026-10-23T08:00:00Z"),
            _item("pm.pdf", total="15.00", subject="Evening trip", received="2026-10-23T22:00:00Z"),
        ]
    )

    assert _actions(summary) == {"pm.pdf": "file", "am.pdf": "review"}
    assert "separate ride" in summary["rows"][0]["reason"]
    assert summary["ok"] is False


def test_a_round_trip_with_two_identical_fares_is_not_called_a_duplicate() -> None:
    summary = summarize_receipts(
        [
            _item("out.pdf", subject="Trip out", received="2026-10-23T08:00:00Z"),
            _item("back.pdf", subject="Trip back", received="2026-10-23T18:00:00Z"),
        ]
    )

    assert sorted(_actions(summary).values()) == ["file", "review"]


def test_arrival_within_minutes_is_evidence_of_one_ride() -> None:
    summary = summarize_receipts(
        [
            _item("a.pdf", total="18.50", subject="Receipt 1", received="2026-10-23T08:00:00Z"),
            _item("b.pdf", total="21.00", subject="Receipt 2", received="2026-10-23T08:12:00Z"),
        ]
    )

    assert _actions(summary) == {"a.pdf": "drop", "b.pdf": "file"}


def test_the_same_message_id_is_the_same_receipt() -> None:
    summary = summarize_receipts(
        [
            _item("a.pdf", subject="x", message_id="m1"),
            _item("b.pdf", subject="y", message_id="m1"),
        ]
    )

    assert sorted(_actions(summary).values()) == ["drop", "file"]


def test_a_chain_of_rides_is_not_collapsed_into_the_highest() -> None:
    summary = summarize_receipts(
        [
            _item("a.pdf", total="10.00", received="2026-10-23T06:00:00Z"),
            _item("b.pdf", total="12.00", received="2026-10-23T12:00:00Z"),
            _item("c.pdf", total="14.00", received="2026-10-23T18:00:00Z"),
        ]
    )

    assert "drop" not in _actions(summary).values()


def test_an_impossible_calendar_date_is_not_trusted() -> None:
    summary = summarize_receipts(
        [_item("a.pdf", date="2026-13-45", received="2026-10-24T01:00:00Z")]
    )

    row = summary["rows"][0]
    assert row["date"] == "2026-10-24"
    assert row["action"] == "review"


def test_a_shared_subject_alone_does_not_make_two_rides_one() -> None:
    summary = summarize_receipts(
        [
            _item("am.pdf", total="18.50", received="2026-10-23T08:00:00Z"),
            _item("pm.pdf", total="21.00", received="2026-10-23T20:00:00Z"),
        ]
    )

    assert sorted(_actions(summary).values()) == ["file", "review"]


def test_a_naive_and_an_aware_arrival_time_can_be_compared() -> None:
    summary = summarize_receipts(
        [
            _item("a.pdf", total="18.50", received="2026-10-23T08:00:00"),
            _item("b.pdf", total="21.00", received="2026-10-23T08:05:00Z"),
        ]
    )

    assert _actions(summary) == {"a.pdf": "drop", "b.pdf": "file"}


def test_a_total_or_currency_that_is_not_certain_goes_to_review() -> None:
    summary = summarize_receipts(
        [
            _item("a.pdf", vendor="A", total_confidence="low"),
            _item("b.pdf", vendor="B", currency_confidence="low"),
            _item("c.pdf", vendor="C"),
        ]
    )

    assert _actions(summary) == {"a.pdf": "review", "b.pdf": "review", "c.pdf": "file"}
    assert summary["grand_totals"] == {"USD": "18.50"}
    assert summary["ok"] is False


def test_totals_at_the_ratio_limit_are_a_tip_and_beyond_it_are_not() -> None:
    inside = summarize_receipts(
        [
            _item("a.pdf", total="20.00"),
            _item("b.pdf", total="28.00", received="2026-10-23T08:10:00Z"),
        ]
    )
    beyond = summarize_receipts(
        [
            _item("a.pdf", total="20.00"),
            _item("b.pdf", total="29.00", received="2026-10-23T08:10:00Z"),
        ]
    )

    assert _actions(inside) == {"a.pdf": "drop", "b.pdf": "file"}
    assert _actions(beyond) == {"a.pdf": "file", "b.pdf": "file"}
