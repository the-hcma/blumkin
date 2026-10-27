"""Ledger-backed stage / apply / verify / reconcile / handoff (issue #489)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.ledger import ItemKind, ItemState, begin_instance, record_item
from blumkin.browser.ledger_ops import (
    apply_item,
    handoff_from_ledger,
    reconcile,
    stage_item,
    verify_item,
)
from blumkin.browser.model import (
    Control,
    ControlKind,
    ControlSafety,
    Flow,
    FlowStep,
    Locator,
    LocatorStrategy,
    Page,
    SiteModel,
)


def _model(task: str = "expense-report", *, version: int = 1) -> SiteModel:
    return SiteModel(
        blumkin_version="0.0.0",
        flows=(
            Flow(
                name="add-line",
                steps=(
                    FlowStep(action="click", control="Add line", expect=None, page="form"),
                    FlowStep(action="fill", control="Amount", expect=None, page="form"),
                ),
            ),
        ),
        hosts=("erp.example.com",),
        pages=(
            Page(
                page_id="form",
                url_template="/expenses",
                fingerprint=("button:Add line", "textbox:Amount"),
                controls=(
                    Control(
                        name="expense.add",
                        kind=ControlKind.BUTTON,
                        safety=ControlSafety.SAFE,
                        locators=(
                            Locator(strategy=LocatorStrategy.ROLE_NAME, value="button:Add line"),
                        ),
                    ),
                    Control(
                        name="expense.amount",
                        kind=ControlKind.TEXT,
                        safety=ControlSafety.SAFE,
                        locators=(Locator(strategy=LocatorStrategy.LABEL, value="Amount"),),
                    ),
                ),
            ),
        ),
        recorded_at="2026-10-26T00:00:00Z",
        task=task,
        version=version,
    )


def test_stage_then_verify_and_re_stage_is_noop(tmp_path: Path) -> None:
    digest = "a" * 64
    begin_instance(tmp_path, "expense-report", "trip-1", expected={ItemKind.RECEIPT: 2})
    first = stage_item(tmp_path, "expense-report", "trip-1", ItemKind.RECEIPT, "r1", sha256=digest)
    assert first.noop is False
    assert first.item.state is ItemState.STAGED
    record_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.RECEIPT,
        "r1",
        ItemState.FILLED,
        sha256=digest,
    )
    verify_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.RECEIPT,
        "r1",
        present_on_page=True,
        sha256=digest,
    )
    again = stage_item(tmp_path, "expense-report", "trip-1", ItemKind.RECEIPT, "r1", sha256=digest)
    assert again.noop is True
    assert again.item.state is ItemState.VERIFIED


def test_apply_runs_flow_and_marks_filled(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    steps: list[str] = []

    def run(step: Any, values: Any) -> None:
        steps.append(f"{step.action}:{step.control}:{values.get('Amount')}")

    result = apply_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.DAY,
        "2026-10-01",
        flow_name="add-line",
        values={"Amount": "8"},
        model=_model(),
        run_step=run,
    )
    assert result.noop is False
    assert result.item.state is ItemState.FILLED
    assert steps == ["click:Add line:8", "fill:Amount:8"]


def test_apply_already_verified_is_noop(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    record_item(tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-01", ItemState.STAGED)
    record_item(tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-01", ItemState.FILLED)
    record_item(
        tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-01", ItemState.VERIFIED
    )
    calls = {"n": 0}

    def run(_step: Any, _values: Any) -> None:
        calls["n"] += 1

    result = apply_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.DAY,
        "2026-10-01",
        flow_name="add-line",
        model=_model(),
        run_step=run,
    )
    assert result.noop is True
    assert calls["n"] == 0


def test_apply_failure_marks_failed(tmp_path: Path) -> None:
    from blumkin.browser.ledger import instance_status

    begin_instance(tmp_path, "expense-report", "trip-1")

    def boom(_step: Any, _values: Any) -> None:
        raise RuntimeError("page gone")

    with pytest.raises(RuntimeError, match="page gone"):
        apply_item(
            tmp_path,
            "expense-report",
            "trip-1",
            ItemKind.DAY,
            "2026-10-01",
            flow_name="add-line",
            model=_model(),
            run_step=boom,
        )
    folded = instance_status(tmp_path, "expense-report", "trip-1")
    assert folded.items[0].state is ItemState.FAILED


def test_reconcile_reports_divergence(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    record_item(tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-01", ItemState.STAGED)
    record_item(tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-01", ItemState.FILLED)
    record_item(
        tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-01", ItemState.VERIFIED
    )
    record_item(tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-02", ItemState.STAGED)
    record_item(tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-02", ItemState.FILLED)
    report = reconcile(
        tmp_path,
        "expense-report",
        "trip-1",
        kind=ItemKind.DAY,
        page_items=["2026-10-01", "2026-10-03"],
    )
    assert report.matched == ("2026-10-01",)
    assert report.only_in_ledger == ("2026-10-02",)
    assert report.only_on_page == ("2026-10-03",)


def test_handoff_from_ledger_never_submitted(tmp_path: Path) -> None:
    digest = "b" * 64
    begin_instance(tmp_path, "expense-report", "trip-1", expected={ItemKind.RECEIPT: 1})
    stage_item(tmp_path, "expense-report", "trip-1", ItemKind.RECEIPT, "r1", sha256=digest)
    handoff = handoff_from_ledger(
        tmp_path,
        "expense-report",
        "trip-1",
        steps=("Review the lines.", "Press Submit yourself."),
    )
    assert handoff.submitted is False
    assert handoff.needs_user_action is True
    assert any("r1" in line for line in handoff.summary)
    assert any("in progress" in d for d in handoff.open_decisions)


def test_apply_records_low_confidence_on_the_item(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    result = apply_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.RECEIPT,
        "r1",
        flow_name="add-line",
        values={"Amount": "8"},
        sha256="b" * 64,
        low_confidence=True,
        model=_model(),
        run_step=lambda _step, _values: None,
    )
    assert result.item.state is ItemState.FILLED
    assert result.item.low_confidence is True
