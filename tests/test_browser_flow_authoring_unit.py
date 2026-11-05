"""`blumkin browser model flow add`: write a flow into the model from short specs (#622)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.explore import ModelDraft, parse_aria_snapshot
from blumkin.browser.flow_authoring import FlowSpecError, build_flow
from blumkin.browser.model import ParamKind, default_store, list_versions, load_model, save_model
from blumkin.cli import main
from blumkin.exit_codes import EXIT_NOT_FOUND, EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE

_RECORDED = """\
- heading "Expenses" [level=1]
- link "Help"
- button "Add line"
- textbox "Amount"
- button "Submit report"
"""


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    (config / "config.toml").write_text(
        '[browser.tasks.expense-report]\nallowed_hosts = ["erp.example.com"]\n\n'
        '[profiles.default]\nclient_id = "abc-123"\n'
    )
    return config


def _store(config: Path) -> list[str]:
    draft = ModelDraft(task="expense-report", hosts=("erp.example.com",))
    draft.add_page(
        "expense-list", "https://erp.example.com/expenses", parse_aria_snapshot(_RECORDED)
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    saved = save_model(default_store(config), model)
    return [control.name for control in saved.pages[0].controls]


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", "browser", "model", "flow", "add", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


# ------------------------------------------------------------------------------ the parser


def test_params_and_steps_are_read_from_their_specs() -> None:
    flow = build_flow(
        "add-receipt",
        ["date:date", "type:choice=Meals|Taxi", "file:file", "note:text"],
        [
            "click expense.add page=expense-list expect_heading='Edit Report'",
            "fill expense.amount bind=note",
            "cell row=1 column=$date value=''",
            'handoff instruction="Click OK, then Save"',
        ],
    )

    assert [(p.name, p.kind) for p in flow.params] == [
        ("date", ParamKind.DATE),
        ("type", ParamKind.CHOICE),
        ("file", ParamKind.FILE),
        ("note", ParamKind.TEXT),
    ]
    assert flow.params[1].choices == ("Meals", "Taxi")
    first, second, third, fourth = flow.steps
    assert (first.action, first.control, first.page) == ("click", "expense.add", "expense-list")
    assert first.expect_heading == "Edit Report"
    assert (second.control, second.bind) == ("expense.amount", "note")
    assert (third.control, third.row, third.column, third.value) == (None, "1", "$date", "")
    assert (fourth.action, fourth.instruction) == ("handoff", "Click OK, then Save")


@pytest.mark.parametrize(
    ("params", "steps", "match"),
    [
        ([], [], "at least one --step"),
        (["date"], ["click x"], "NAME:KIND"),
        (["date:when"], ["click x"], "parameter kind of"),
        (["t:choice"], ["click x"], "options after choice="),
        (["d:date=A|B"], ["click x"], "options only for a choice"),
        ([], ["  "], "name an action"),
        ([], ["click 'unterminated"], "quoted properly"),
        ([], ["click a b"], "key=value"),
        ([], ["click a nonsense=1"], "key=value"),
        ([], ["click a page=x page=y"], "once"),
        ([], ["handoff pause"], "key=value"),
    ],
)
def test_a_spec_that_cannot_be_read_is_refused_with_what_was_expected(
    params: list[str], steps: list[str], match: str
) -> None:
    with pytest.raises(FlowSpecError, match=match):
        build_flow("f", params, steps)


# ----------------------------------------------------------------------------- the command


def test_without_yes_the_flow_is_only_checked_and_nothing_is_written(_config_dir: Path) -> None:
    names = _store(_config_dir)
    add_line = next(n for n in names if "add" in n)

    result = _run("expense-report", "add-line", "--step", f"click {add_line} page=expense-list")

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["saved"] is False and payload["version"] is None
    assert payload["steps"] == 1 and payload["replaced"] is False
    assert list_versions(default_store(_config_dir), "expense-report") == [1]


def test_yes_saves_the_next_model_version_with_the_flow(_config_dir: Path) -> None:
    names = _store(_config_dir)
    amount = next(n for n in names if "amount" in n)
    add_line = next(n for n in names if "add" in n)

    result = _run(
        "expense-report", "fill-amount", "--yes",
        "--param", "amount:text",
        "--step", f"click {add_line} page=expense-list",
        "--step", f"fill {amount} page=expense-list bind=amount",
        "--step", "handoff instruction='Check the total, then Save' expect_heading='Expenses'",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["version"] == 2 and _json(result)["saved"] is True
    stored = load_model(default_store(_config_dir), "expense-report")
    assert stored is not None and stored.version == 2
    (flow,) = [f for f in stored.flows if f.name == "fill-amount"]
    assert [step.action for step in flow.steps] == ["click", "fill", "handoff"]
    assert [p.name for p in flow.params] == ["amount"]
    assert len(stored.pages) == 1  # the recorded pages are untouched


def test_a_step_that_names_a_control_the_model_does_not_record_is_refused(
    _config_dir: Path,
) -> None:
    _store(_config_dir)

    result = _run(
        "expense-report", "bad", "--yes", "--step", "click not.recorded page=expense-list"
    )

    assert result.exit_code == EXIT_OTHER
    assert "model_invalid" in result.output + (result.stderr or "")
    assert list_versions(default_store(_config_dir), "expense-report") == [1]


def test_a_binding_must_name_a_declared_parameter_and_a_handoff_no_control(
    _config_dir: Path,
) -> None:
    names = _store(_config_dir)
    amount = next(n for n in names if "amount" in n)

    unbound = _run("expense-report", "f", "--step", f"fill {amount} page=expense-list bind=nope")
    handoff = _run("expense-report", "g", "--step", f"handoff {amount} instruction='x'")

    assert unbound.exit_code == EXIT_OTHER
    assert handoff.exit_code == EXIT_USAGE  # a control word on a handoff is not key=value


def test_a_flow_of_the_same_name_is_replaced_only_with_replace(_config_dir: Path) -> None:
    names = _store(_config_dir)
    add_line = next(n for n in names if "add" in n)
    step = f"click {add_line} page=expense-list"
    assert _run("expense-report", "once", "--yes", "--step", step).exit_code == EXIT_SUCCESS

    again = _run("expense-report", "once", "--yes", "--step", step)
    replaced = _run("expense-report", "once", "--yes", "--replace", "--step", step)

    assert again.exit_code == EXIT_USAGE and "--replace" in again.output + (again.stderr or "")
    assert replaced.exit_code == EXIT_SUCCESS and _json(replaced)["replaced"] is True
    assert list_versions(default_store(_config_dir), "expense-report") == [1, 2, 3]


def test_a_task_without_a_saved_model_is_not_found(_config_dir: Path) -> None:
    result = _run("expense-report", "f", "--step", "click x")

    assert result.exit_code == EXIT_NOT_FOUND


def test_the_text_output_says_what_happened(_config_dir: Path) -> None:
    names = _store(_config_dir)
    add_line = next(n for n in names if "add" in n)
    args = ["browser", "model", "flow", "add", "expense-report", "go", "--step",
            f"click {add_line} page=expense-list"]  # fmt: skip

    checked = CliRunner().invoke(main, args)
    saved = CliRunner().invoke(main, [*args, "--yes"])

    assert "is valid (1 steps)" in checked.output and "--yes" in checked.output
    assert "added flow go (model version 2)" in saved.output
