"""Group receipts into trips and total what would be filed (issue #584).

Ride-hail services send two emails per ride, one before and one after the tip is added, and
only the with-tip copy should be filed. `summarize_receipts` groups receipts by vendor, ride
date and currency, treats a lower total within a tip's reach of a higher one as the
without-tip copy of the same ride, and returns one row per receipt saying whether it would be
filed, dropped or needs a person to look. A copy is dropped only with evidence that both are
the same ride (the same message or file, or arrival within minutes of each other); two
receipts that merely look alike are both marked `review`, because a dropped receipt would
never be filed. It decides nothing: nothing is moved or deleted, and the caller shows the
table and the grand total for the user to confirm before any `receipts rehome`.

The ride date comes from the receipt text, then from the subject, and only as a last resort
from the email arrival time (a late-evening ride arrives on the next UTC day), which is
flagged for review. Receipt contents are private: nothing here logs a vendor or an amount.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from blumkin.receipts import Confidence, parse_receipt_text

#: A tip rarely adds more than 40% to the fare; a bigger gap is a different ride.
MAX_TIP_RATIO = Decimal("1.4")
#: Two emails for one ride arrive within minutes of each other.
SAME_RIDE_WINDOW = timedelta(minutes=30)


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
    currency_confidence: str = "high"
    #: A prompt-injection warning found in the receipt text; such a receipt needs a person.
    injection_warning: dict[str, Any] | None = None
    #: SHA-256 of the file when known; identical bytes are the same receipt.
    sha256: str | None = None
    total_confidence: str = "high"


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
    for row in rows:
        row.pop("_evidence", None)
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
_NON_ALNUM = re.compile(r"[\W_]+")


def _mark_copies(members: list[dict[str, Any]]) -> None:
    """Within one vendor, date and currency, drop a copy only when it is the same ride."""
    ordered = sorted(members, key=lambda row: Decimal(row["total"]), reverse=True)
    anchor = ordered[0]
    for row in ordered[1:]:
        higher, lower = Decimal(anchor["total"]), Decimal(row["total"])
        if lower == 0 or higher / lower > MAX_TIP_RATIO:
            anchor = row
            continue
        name = Path(anchor["path"]).name
        if _same_ride(anchor["_evidence"], row["_evidence"]):
            row["action"] = "drop"
            row["reason"] = (
                f"exact duplicate of {name}"
                if higher == lower
                else f"without-tip copy of {name} (total {anchor['total']})"
            )
        else:
            # Neither is safe to file or drop on a guess, so both go to a person.
            row["action"] = "review"
            row["reason"] = (
                f"looks like {name} (total {anchor['total']}) but nothing shows it is the same "
                "ride: ask the user whether it is a copy or a separate ride"
            )
            if anchor["action"] == "file":
                anchor["action"] = "review"
                anchor["reason"] = (
                    f"looks like {Path(row['path']).name} (total {row['total']}) but nothing "
                    "shows it is the same ride: ask the user whether it is a copy or a "
                    "separate ride"
                )


def _same_ride(first: SummaryInput, second: SummaryInput) -> bool:
    """Evidence that two receipts are copies of one ride, not just similar."""
    if first.sha256 and first.sha256 == second.sha256:
        return True
    if first.message_id and first.message_id == second.message_id:
        return True
    # A shared subject alone proves little (ride-hail subjects are generic), so arrival time is
    # the signal: the two emails of one ride land within minutes of each other.
    sent, other = _parse_time(first.received), _parse_time(second.received)
    return sent is not None and other is not None and abs(sent - other) <= SAME_RIDE_WINDOW


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _valid_date(value: str | None) -> bool:
    try:
        return (
            bool(value)
            and _DATE.fullmatch(value or "") is not None
            and bool(date.fromisoformat(value or ""))
        )
    except ValueError:
        return False


def _resolve_date(item: SummaryInput) -> tuple[str | None, str, str]:
    """The ride date, where it came from, and a reason when it needs a person to check."""
    if _valid_date(item.date) and item.date_confidence == Confidence.HIGH.value:
        return item.date, "text", ""
    if item.subject:
        found = parse_receipt_text(item.subject).date
        if _valid_date(found.value) and found.confidence is Confidence.HIGH:
            return found.value, "subject", ""
    if item.date and _valid_date(item.date):
        return item.date, "text", "the receipt date is not certain"
    arrival = (item.received or "")[:10]
    if _valid_date(arrival):
        return (
            arrival,
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
        "injection_warning": item.injection_warning,
        "message_id": item.message_id,
        "path": str(item.path),
        "reason": "",
        "_evidence": item,
        "total": None,
        "vendor": (item.vendor or "").strip() or None,
    }
    problems = [date_reason] if date_reason else []
    total = (item.total or "").strip()
    if not _AMOUNT.fullmatch(total):
        problems.append("no readable total" if not total else "the total is not a plain amount")
    elif item.total_confidence != Confidence.HIGH.value:
        row["total"] = f"{Decimal(total):.2f}"
        problems.append("the total is not certain")
    else:
        row["total"] = f"{Decimal(total):.2f}"
    if row["vendor"] is None:
        problems.append("no vendor")
    if row["currency"] is None:
        problems.append("no currency")
    elif item.currency_confidence != Confidence.HIGH.value:
        problems.append("the currency is not certain")
    if item.injection_warning:
        problems.append(
            "the receipt text matched a prompt-injection pattern: read it yourself before "
            "trusting its fields"
        )
    if problems:
        row["action"] = "review"
        row["reason"] = "; ".join(problems)
    return row


def _vendor_key(vendor: str) -> str:
    lowered = vendor.lower()
    return _NON_ALNUM.sub("", lowered) or lowered
