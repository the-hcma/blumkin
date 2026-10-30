"""Group receipts into trips and total what would be filed (issue #584).

Ride-hail services send two emails per ride, one before and one after the tip is added, and
only the with-tip copy should be filed. `summarize_receipts` groups receipts by vendor, ride
date and currency, treats a lower total within a tip's reach of a higher one as the
without-tip copy of the same ride, and returns one row per receipt saying whether it would be
filed, dropped or needs a person to look. It decides nothing: nothing is moved or deleted,
and the caller shows the table and the grand total for the user to confirm before any
`receipts rehome`.

The ride date comes from the receipt text, then from the subject, and only as a last resort
from the email arrival time (a late-evening ride arrives on the next UTC day), which is
flagged for review. Receipt contents are private: nothing here logs a vendor or an amount.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from blumkin.receipts import Confidence, parse_receipt_text

#: A tip rarely adds more than half the fare; a bigger gap is a different ride.
MAX_TIP_RATIO = Decimal("1.5")


@dataclass(frozen=True, slots=True)
class SummaryInput:
    """One candidate receipt: its fields as read, plus the mail details when it came by mail."""

    currency: str | None
    date: str | None
    date_confidence: str
    message_id: str | None
    path: Path
    received: str | None
    subject: str | None
    total: str | None
    vendor: str | None


def summarize_receipts(items: list[SummaryInput]) -> dict[str, Any]:
    """One row per receipt (file, drop or review) and the grand total per currency."""
    rows = [_row(item) for item in items]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row["action"] == "file":
            key = (_vendor_key(row["vendor"]), row["date"], row["currency"])
            groups.setdefault(key, []).append(row)
    for members in groups.values():
        _mark_copies(members)
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row["action"] == "file":
            totals[row["currency"]] = totals.get(row["currency"], Decimal(0)) + Decimal(
                row["total"]
            )
    needs_review = sum(1 for row in rows if row["action"] == "review")
    return {
        "dropping": sum(1 for row in rows if row["action"] == "drop"),
        "filing": sum(1 for row in rows if row["action"] == "file"),
        "grand_totals": {code: f"{totals[code]:.2f}" for code in sorted(totals)},
        "needs_review": needs_review,
        "ok": needs_review == 0,
        "rows": rows,
    }


_AMOUNT = re.compile(r"\d+(?:\.\d{1,2})?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _mark_copies(members: list[dict[str, Any]]) -> None:
    """Within one vendor, date and currency, drop each copy that is a tip below another."""
    ordered = sorted(members, key=lambda row: Decimal(row["total"]), reverse=True)
    anchor = ordered[0]
    for row in ordered[1:]:
        higher, lower = Decimal(anchor["total"]), Decimal(row["total"])
        if lower == 0 or higher / lower > MAX_TIP_RATIO:
            anchor = row
            continue
        name = Path(anchor["path"]).name
        row["action"] = "drop"
        row["reason"] = (
            f"exact duplicate of {name}"
            if higher == lower
            else f"without-tip copy of {name} (total {anchor['total']})"
        )


def _resolve_date(item: SummaryInput) -> tuple[str | None, str, str]:
    """The ride date, where it came from, and a reason when it needs a person to check."""
    if item.date and _DATE.fullmatch(item.date) and item.date_confidence == Confidence.HIGH.value:
        return item.date, "text", ""
    if item.subject:
        found = parse_receipt_text(item.subject).date
        if found.value and found.confidence is Confidence.HIGH:
            return found.value, "subject", ""
    if item.date and _DATE.fullmatch(item.date):
        return item.date, "text", "the receipt date is not certain"
    if item.received and _DATE.fullmatch(item.received[:10]):
        return (
            item.received[:10],
            "received",
            "the date is the email arrival time, which can be the day after the ride",
        )
    return None, "none", "no date found"


def _row(item: SummaryInput) -> dict[str, Any]:
    date, source, date_reason = _resolve_date(item)
    row: dict[str, Any] = {
        "action": "file",
        "currency": (item.currency or "").strip().upper() or None,
        "date": date,
        "date_source": source,
        "message_id": item.message_id,
        "path": str(item.path),
        "reason": "",
        "total": None,
        "vendor": (item.vendor or "").strip() or None,
    }
    problems = [date_reason] if date_reason else []
    total = (item.total or "").strip()
    if not _AMOUNT.fullmatch(total):
        problems.append("no readable total" if not total else "the total is not a plain amount")
    else:
        row["total"] = f"{Decimal(total):.2f}"
    if row["vendor"] is None:
        problems.append("no vendor")
    if row["currency"] is None:
        problems.append("no currency")
    if problems:
        row["action"] = "review"
        row["reason"] = "; ".join(problems)
    return row


def _vendor_key(vendor: str) -> str:
    return _NON_ALNUM.sub("", vendor.lower())
