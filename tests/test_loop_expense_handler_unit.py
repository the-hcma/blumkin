"""The expense-report loop handler: file mailed receipts into an open trip (issue #504)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from blumkin.browser.ledger import (
    ItemKind,
    ItemState,
    begin_instance,
    instance_status,
    mark_submitted,
)
from blumkin.browser.ledger_ops import ApplyResult, apply_item
from blumkin.browser.receipt_rehome import AlreadyFiledError, RehomeError, rehome_receipt
from blumkin.loop.expense_handler import EXPENSE_FLOW_NAME, file_receipts
from blumkin.loop.mail_intake import intake_dir
from blumkin.loop.types import RunContext, RunOutcome, TriggerKind, TriggerSpec
from blumkin.receipts import (
    Confidence,
    ReceiptField,
    ReceiptFields,
    ReceiptReading,
    ReceiptStatus,
)

_INSTANCE = "trip-lisbon"
_TASK = "expense-report"
_VENDOR = "Cafe Rosa"
_TOTAL = "18.50"


def _field(value: str | None, confidence: Confidence = Confidence.HIGH) -> ReceiptField:
    return ReceiptField(confidence=confidence, value=value)


def _reading(
    *,
    currency: ReceiptField | None = None,
    date: ReceiptField | None = None,
    status: ReceiptStatus = ReceiptStatus.OK,
    total: ReceiptField | None = None,
    vendor: ReceiptField | None = None,
    injection: dict[str, Any] | None = None,
) -> ReceiptReading:
    return ReceiptReading(
        fields=ReceiptFields(
            currency=currency or _field("USD"),
            date=date or _field("2026-10-20"),
            total=total or _field(_TOTAL),
            vendor=vendor or _field(_VENDOR),
        ),
        injection_warning=injection,
        message="",
        ocr_used=False,
        path="x",
        status=status,
    )


def _trigger(instance: str = _INSTANCE) -> TriggerSpec:
    return TriggerSpec(instance=instance, kind=TriggerKind.LOOP, name="receipts", task=_TASK)


def _context(tmp_path: Path, message_id: str | None = "m1") -> RunContext:
    return RunContext(
        config_dir=tmp_path, instance=_INSTANCE, message_id=message_id, profile="work"
    )


def _intake(tmp_path: Path, *names: str, content: str = "receipt-bytes") -> list[Path]:
    folder = intake_dir(tmp_path, trigger="receipts", message_id="m1")
    paths: list[Path] = []
    for index, name in enumerate(names):
        path = folder / name
        path.write_text(f"{content}-{index}", encoding="utf-8")
        paths.append(path)
    return paths


class _Apply:
    """Stands in for `apply_item`: runs the real ledger code with a no-op browser step."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> ApplyResult:
        from blumkin.browser.model import Flow, FlowStep, SiteModel

        self.calls.append({"args": args, **kwargs})
        model = SiteModel(
            blumkin_version="0.0.0",
            flows=(Flow(name=EXPENSE_FLOW_NAME, steps=(FlowStep("click", "Add", None, "p"),)),),
            hosts=("erp.example.com",),
            pages=(),
            recorded_at="2026-10-26T00:00:00Z",
            task=_TASK,
            version=1,
        )
        return apply_item(*args, model=model, run_step=lambda _s, _v: None, **kwargs)


def _run(
    tmp_path: Path,
    reading: ReceiptReading | list[ReceiptReading],
    *,
    apply_fn: Any = None,
    rehome_fn: Any = rehome_receipt,
    context: RunContext | None = None,
) -> tuple[Any, _Apply]:
    applier = apply_fn or _Apply()
    readings = list(reading) if isinstance(reading, list) else None
    upload = tmp_path / "archive"
    result = file_receipts(
        _trigger(),
        context if context is not None else _context(tmp_path),
        apply_fn=applier,
        read_fn=(lambda _p: readings.pop(0)) if readings is not None else (lambda _p: reading),  # type: ignore[arg-type,return-value]
        rehome_fn=rehome_fn,
        upload_dirs_fn=lambda _task: (str(upload),),
    )
    return result, applier


def _items(tmp_path: Path) -> tuple[Any, ...]:
    return instance_status(tmp_path, _TASK, _INSTANCE).items


def _begin(tmp_path: Path) -> None:
    begin_instance(tmp_path, _TASK, _INSTANCE)


def test_files_a_confident_receipt_into_the_ledger(tmp_path: Path) -> None:
    _begin(tmp_path)
    (source,) = _intake(tmp_path, "taxi.pdf")
    result, applier = _run(tmp_path, _reading())
    assert result.outcome is RunOutcome.DONE
    assert "filed 1 receipt(s)" in result.reason
    assert "low_confidence" not in result.reason
    (item,) = _items(tmp_path)
    assert item.kind is ItemKind.RECEIPT
    assert item.state is ItemState.FILLED
    assert item.low_confidence is False
    assert item.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert item.item == f"receipt-{item.sha256[:12]}"
    call = applier.calls[0]
    assert call["flow_name"] == EXPENSE_FLOW_NAME
    # The managed copy exists under the instance folder; the intake file is kept (copied).
    managed = tmp_path / "archive" / _INSTANCE
    assert [p.name for p in managed.iterdir()] == [
        f"2026-10-20_cafe-rosa_18.50-USD_{item.sha256[:8]}.pdf"
    ]
    assert source.is_file()
    assert call["values"]["path"] == str(managed / next(managed.iterdir()).name)


def test_low_confidence_field_files_with_the_best_reading_and_flags_it(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "taxi.pdf")
    reading = _reading(total=_field(_TOTAL, Confidence.LOW))
    calls: list[bool] = []

    def spy_rehome(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["require_high_confidence"])
        return rehome_receipt(*args, **kwargs)

    result, _ = _run(tmp_path, reading, rehome_fn=spy_rehome)
    assert result.outcome is RunOutcome.DONE
    assert calls == [False]
    assert "low_confidence=true" in result.reason
    (item,) = _items(tmp_path)
    assert item.state is ItemState.FILLED
    assert item.low_confidence is True


def test_a_fully_confident_receipt_requires_high_confidence_on_rehome(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "taxi.pdf")
    calls: list[bool] = []

    def spy_rehome(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["require_high_confidence"])
        return rehome_receipt(*args, **kwargs)

    _run(tmp_path, _reading(), rehome_fn=spy_rehome)
    assert calls == [True]


def test_nothing_the_ledger_or_reason_holds_is_a_value(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "taxi.pdf")
    result, _ = _run(tmp_path, _reading(total=_field(_TOTAL, Confidence.LOW)))
    journal = (tmp_path / "browser-tasks" / _TASK / _INSTANCE / "journal.jsonl").read_text()
    for secret in (_VENDOR, "cafe", _TOTAL, "2026-10-20", "taxi"):
        assert secret.lower() not in journal.lower()
        assert secret.lower() not in result.reason.lower()


def test_unreadable_receipt_alone_needs_the_user(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "scan.jpg")
    calls: list[str] = []

    def rehome_never(*_a: Any, **_k: Any) -> Any:
        calls.append("rehome")

    result, applier = _run(
        tmp_path, _reading(status=ReceiptStatus.NEEDS_MANUAL), rehome_fn=rehome_never
    )
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "unreadable" in result.reason
    assert calls == []
    assert applier.calls == []
    assert _items(tmp_path) == ()


def test_a_field_with_no_value_is_unusable_not_guessed(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "scan.jpg")
    result, applier = _run(
        tmp_path, _reading(total=ReceiptField(confidence=Confidence.NONE, value=None))
    )
    assert result.outcome is RunOutcome.NEEDS_USER
    assert applier.calls == []


def test_all_fields_missing_is_unreadable(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "scan.jpg")
    none = ReceiptField(confidence=Confidence.NONE, value=None)
    result, _ = _run(tmp_path, _reading(currency=none, date=none, total=none, vendor=none))
    assert result.outcome is RunOutcome.NEEDS_USER


def test_one_unreadable_among_filed_is_still_done_and_says_so(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "a.pdf", "b.pdf")
    result, _ = _run(tmp_path, [_reading(), _reading(status=ReceiptStatus.NEEDS_MANUAL)])
    assert result.outcome is RunOutcome.DONE
    assert "filed 1 receipt(s)" in result.reason
    assert "1 unreadable" in result.reason
    assert len(_items(tmp_path)) == 1


def test_an_injection_warning_stops_the_receipt(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "a.pdf")
    result, applier = _run(tmp_path, _reading(injection={"patterns": ["x"]}))
    assert result.outcome is RunOutcome.NEEDS_USER
    assert applier.calls == []


def test_a_missing_ledger_instance_is_needs_user_and_never_begins_one(tmp_path: Path) -> None:
    _intake(tmp_path, "a.pdf")
    result, _ = _run(tmp_path, _reading())
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "begin the trip" in result.reason
    assert not (tmp_path / "browser-tasks" / _TASK / _INSTANCE).exists()


def test_a_submitted_instance_is_needs_user(tmp_path: Path) -> None:
    _begin(tmp_path)
    mark_submitted(tmp_path, _TASK, _INSTANCE)
    _intake(tmp_path, "a.pdf")
    result, applier = _run(tmp_path, _reading())
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "submitted" in result.reason
    assert applier.calls == []


def test_no_attachments_is_needs_user(tmp_path: Path) -> None:
    _begin(tmp_path)
    result, _ = _run(tmp_path, _reading())
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "no receipt attachments" in result.reason


def test_without_mail_context_it_is_needs_user(tmp_path: Path) -> None:
    _begin(tmp_path)
    for context in (None, RunContext(config_dir=tmp_path), _context(tmp_path, message_id=None)):
        result = file_receipts(_trigger(), context)
        assert result.outcome is RunOutcome.NEEDS_USER


def test_no_upload_dirs_is_needs_user(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "a.pdf")
    result = file_receipts(
        _trigger(),
        _context(tmp_path),
        upload_dirs_fn=lambda _task: (),
        read_fn=lambda _p: _reading(),
    )
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "upload_dirs" in result.reason


def test_a_rehome_failure_blocks_the_run(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "a.pdf")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RehomeError("nope")

    result, applier = _run(tmp_path, _reading(), rehome_fn=boom)
    assert result.outcome is RunOutcome.NEEDS_USER
    assert "could not be filed" in result.reason
    assert applier.calls == []


def test_a_browser_failure_is_needs_user_and_leaves_a_failed_item(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "a.pdf")

    def failing_apply(*args: Any, **kwargs: Any) -> ApplyResult:
        from blumkin.browser.model import Flow, FlowStep, SiteModel

        model = SiteModel(
            blumkin_version="0.0.0",
            flows=(Flow(name=EXPENSE_FLOW_NAME, steps=(FlowStep("click", "Add", None, "p"),)),),
            hosts=(),
            pages=(),
            recorded_at="2026-10-26T00:00:00Z",
            task=_TASK,
            version=1,
        )

        def step(_s: Any, _v: Any) -> None:
            raise RuntimeError("browser not running")

        return apply_item(*args, model=model, run_step=step, **kwargs)

    result, _ = _run(tmp_path, _reading(), apply_fn=failing_apply)
    assert result.outcome is RunOutcome.NEEDS_USER
    (item,) = _items(tmp_path)
    assert item.state is ItemState.FAILED
    assert "browser not running" not in result.reason


def test_a_rerun_of_the_same_message_files_nothing_twice(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "a.pdf")
    first, applier = _run(tmp_path, _reading())
    assert first.outcome is RunOutcome.DONE
    again, _ = _run(tmp_path, _reading(), apply_fn=applier)
    assert again.outcome is RunOutcome.DONE
    assert "0 receipt(s)" in again.reason
    assert "1 already filed" in again.reason
    assert len(applier.calls) == 1
    assert len(_items(tmp_path)) == 1


def test_a_retry_after_an_interrupted_filing_reuses_the_managed_copy(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, "a.pdf")

    def failing(*_a: Any, **_k: Any) -> ApplyResult:
        raise RuntimeError("browser not running")

    first, _ = _run(tmp_path, _reading(), apply_fn=failing)
    assert first.outcome is RunOutcome.NEEDS_USER
    second, applier = _run(tmp_path, _reading())
    assert second.outcome is RunOutcome.DONE
    managed = tmp_path / "archive" / _INSTANCE
    assert applier.calls[0]["values"]["path"] == str(next(managed.iterdir()))
    assert [i.state for i in _items(tmp_path)] == [ItemState.FILLED]


def test_already_filed_error_from_rehome_is_handled(tmp_path: Path) -> None:
    _begin(tmp_path)
    (source,) = _intake(tmp_path, "a.pdf")

    def already(*_a: Any, **_k: Any) -> Any:
        raise AlreadyFiledError("dup")

    result, applier = _run(tmp_path, _reading(), rehome_fn=already)
    assert result.outcome is RunOutcome.DONE
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert applier.calls[0]["sha256"] == digest
    assert applier.calls[0]["values"]["path"].endswith(f"_{digest[:8]}.pdf")


def test_hidden_files_in_the_intake_folder_are_skipped(tmp_path: Path) -> None:
    _begin(tmp_path)
    _intake(tmp_path, ".partial.pdf")
    result, _ = _run(tmp_path, _reading())
    assert result.outcome is RunOutcome.NEEDS_USER


def test_expense_report_is_a_default_handler() -> None:
    from blumkin.loop.expense_handler import expense_report
    from blumkin.loop.handlers import default_handlers

    assert default_handlers()["expense-report"] is expense_report
