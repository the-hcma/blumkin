"""Flow parameters (#622): declared, typed, checked before anything is staged or touched."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.flow_params import validate_params
from blumkin.browser.ledger import ItemKind, ItemState, LedgerError, begin_instance, instance_status
from blumkin.browser.ledger_ops import apply_item
from blumkin.browser.model import Flow, FlowParam, FlowStep, ParamKind, SiteModel

_PARAMS = (
    FlowParam(name="date", kind=ParamKind.DATE),
    FlowParam(name="type", kind=ParamKind.CHOICE, choices=("Meals", "Taxi")),
    FlowParam(name="file", kind=ParamKind.FILE),
    FlowParam(name="merchant", kind=ParamKind.TEXT),
)


def _flow() -> Flow:
    steps = (
        FlowStep(action="fill", control="Date", expect=None, page=None, bind="date"),
        FlowStep(action="fill", control="Merchant", expect=None, page=None, bind="merchant"),
        FlowStep(action="upload", control="File Name", expect=None, page=None, bind="file"),
    )
    return Flow(name="add-receipt", params=_PARAMS, steps=steps)


def _values(tmp_path: Path, **overrides: str) -> dict[str, str]:
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF")
    values = {"date": "2026-10-30", "type": "Meals", "file": str(receipt), "merchant": "Cafe"}
    return values | overrides


def test_complete_valid_values_pass(tmp_path: Path) -> None:
    values = _values(tmp_path)
    assert validate_params(_flow(), values | {"item": "r1"}) == values | {"item": "r1"}


def test_a_flow_without_parameters_keeps_the_older_unchecked_bindings() -> None:
    flow = Flow(name="old", steps=())
    assert validate_params(flow, {"anything": "goes"}) == {"anything": "goes"}


def test_every_problem_is_listed_at_once_and_no_value_is_shown(tmp_path: Path) -> None:
    secret = "Cafe-Zebra-4417"
    values = _values(tmp_path, date="30/10/2026", type="Spa", merchant="") | {"extra": secret}
    del values["file"]

    with pytest.raises(LedgerError) as caught:
        validate_params(_flow(), values)

    message = str(caught.value)
    for part in (
        "missing file",
        "not parameters of this flow: 'extra'",
        "date:",
        "type:",
        "merchant:",
    ):
        assert part in message
    assert "'Meals', 'Taxi'" in message
    assert (
        secret not in message
        and "30/10/2026" not in message
        and "Spa'" not in message.replace("'Meals', 'Taxi'", "")
    )


@pytest.mark.parametrize(
    "date", ["2026-13-01", "2026-02-30", "2026-1-5", "20261030", " 2026-10-30"]
)
def test_a_date_must_be_a_real_iso_date(tmp_path: Path, date: str) -> None:
    with pytest.raises(LedgerError, match="date:"):
        validate_params(_flow(), _values(tmp_path, date=date))


def test_a_choice_must_be_exactly_one_of_the_recorded_options(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="type:"):
        validate_params(_flow(), _values(tmp_path, type="meals"))


def test_a_file_must_be_an_existing_regular_file_not_a_link(tmp_path: Path) -> None:
    real = tmp_path / "receipt.pdf"
    real.write_bytes(b"%PDF")
    link = tmp_path / "link.pdf"
    link.symlink_to(real)
    for bad in (str(tmp_path / "missing.pdf"), str(tmp_path), str(link), ""):
        with pytest.raises(LedgerError, match="file:"):
            validate_params(_flow(), _values(tmp_path, file=bad))


@pytest.mark.parametrize("merchant", ["", "   ", "line\nbreak", "x" * 501, "tab\tchar"])
def test_text_must_be_printable_and_not_empty_or_long(tmp_path: Path, merchant: str) -> None:
    with pytest.raises(LedgerError, match="merchant:"):
        validate_params(_flow(), _values(tmp_path, merchant=merchant))


def _model(flow: Flow) -> SiteModel:
    return SiteModel(
        blumkin_version="0.0.0",
        flows=(flow,),
        hosts=("erp.example.com",),
        pages=(),
        recorded_at="2026-10-26T00:00:00Z",
        task="expense-report",
        version=1,
    )


def test_apply_refuses_bad_parameters_before_staging_or_running_anything(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    ran: list[str] = []

    with pytest.raises(LedgerError, match="missing"):
        apply_item(
            tmp_path,
            "expense-report",
            "trip-1",
            ItemKind.RECEIPT,
            "r1",
            flow_name="add-receipt",
            values={"date": "2026-10-30"},
            model=_model(_flow()),
            run_step=lambda step, values: ran.append(step.action),
        )

    assert ran == []
    assert instance_status(tmp_path, "expense-report", "trip-1").items == ()


def test_apply_runs_the_steps_with_validated_values_and_marks_the_item_filled(
    tmp_path: Path,
) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    seen: list[tuple[str, str | None, str]] = []

    def run(step: FlowStep, values: Any) -> None:
        seen.append((step.action, step.bind, values[step.bind]))

    values = _values(tmp_path)
    result = apply_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.DAY,
        "2026-10-30",
        flow_name="add-receipt",
        values=values,
        model=_model(_flow()),
        run_step=run,
    )

    assert result.item.state is ItemState.FILLED
    assert seen == [
        ("fill", "date", "2026-10-30"),
        ("fill", "merchant", "Cafe"),
        ("upload", "file", values["file"]),
    ]


def test_the_default_runner_fills_and_uploads_the_bound_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.config import BrowserSetup

    calls: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(
        actions,
        "fill_task",
        lambda config, task, label, value: calls.append(("fill", (label, value))),
    )
    monkeypatch.setattr(
        actions,
        "upload_task",
        lambda config, task, label, path: calls.append(("upload", (label, path))),
    )
    monkeypatch.setattr(
        "blumkin.config.load_browser_setup", lambda: BrowserSetup.__new__(BrowserSetup)
    )
    run = ledger_ops._default_run_step("expense-report")  # type: ignore[attr-defined]
    values = _values(tmp_path)

    for step in _flow().steps:
        run(step, values)

    assert calls == [
        ("fill", ("Date", "2026-10-30")),
        ("fill", ("Merchant", "Cafe")),
        ("upload", ("File Name", Path(values["file"]))),
    ]


def test_a_verified_item_is_a_noop_the_second_time_even_without_parameters(
    tmp_path: Path,
) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    kwargs: dict[str, Any] = {
        "flow_name": "add-receipt",
        "model": _model(_flow()),
        "run_step": lambda step, values: None,
    }
    apply_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.DAY,
        "2026-10-30",
        values=_values(tmp_path),
        **kwargs,
    )
    from blumkin.browser.ledger import record_item

    record_item(
        tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-30", ItemState.VERIFIED
    )

    again = apply_item(tmp_path, "expense-report", "trip-1", ItemKind.DAY, "2026-10-30", **kwargs)

    assert again.noop is True


def test_an_undeclared_key_is_shown_quoted_so_control_characters_cannot_reach_the_terminal(
    tmp_path: Path,
) -> None:
    values = _values(tmp_path) | {"bad\x1b[2Jkey": "x"}

    with pytest.raises(LedgerError) as caught:
        validate_params(_flow(), values)

    assert "\x1b" not in str(caught.value)
    assert "\\x1b" in str(caught.value)


def test_a_missing_model_leaves_the_ledger_empty(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")

    with pytest.raises(LedgerError, match="saved site model"):
        apply_item(
            tmp_path,
            "expense-report",
            "trip-1",
            ItemKind.DAY,
            "2026-10-30",
            flow_name="add-receipt",
            models_store=tmp_path / "no-models",
        )

    assert instance_status(tmp_path, "expense-report", "trip-1").items == ()


def test_a_run_cannot_override_the_ledger_item_through_an_item_value(tmp_path: Path) -> None:
    begin_instance(tmp_path, "expense-report", "trip-1")
    seen: list[str] = []

    apply_item(
        tmp_path,
        "expense-report",
        "trip-1",
        ItemKind.DAY,
        "2026-10-30",
        flow_name="add-receipt",
        values=_values(tmp_path) | {"item": "someone-else"},
        model=_model(_flow()),
        run_step=lambda step, values: seen.append(values["item"]),
    )

    assert set(seen) == {"2026-10-30"}
