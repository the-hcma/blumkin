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


def test_starts_on_and_verify_are_read_from_their_specs() -> None:
    flow = build_flow(
        "check-amount",
        ["amount:text"],
        [
            "verify expense.amount page=expense-list bind=amount starts_on='Edit Report'",
            "click expense.add starts_on='Edit Report'",
        ],
    )

    check, add = flow.steps
    assert (check.action, check.control, check.bind) == ("verify", "expense.amount", "amount")
    assert check.starts_on == "Edit Report" and add.starts_on == "Edit Report"


def test_starts_on_and_verify_are_saved_and_read_back(_config_dir: Path) -> None:
    names = _store(_config_dir)
    amount = next(n for n in names if "amount" in n)
    add_line = next(n for n in names if "add" in n)

    result = _run(
        "expense-report", "check-then-add", "--yes",
        "--param", "amount:text",
        "--step", f"verify {amount} page=expense-list bind=amount starts_on=Expenses",
        "--step", f"click {add_line} page=expense-list starts_on=Expenses",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    stored = load_model(default_store(_config_dir), "expense-report")
    assert stored is not None
    (flow,) = [f for f in stored.flows if f.name == "check-then-add"]
    assert [(s.action, s.starts_on) for s in flow.steps] == [
        ("verify", "Expenses"),
        ("click", "Expenses"),
    ]


def test_a_verify_step_without_a_bind_is_refused_even_when_the_flow_has_no_params(
    _config_dir: Path,
) -> None:
    names = _store(_config_dir)
    amount = next(n for n in names if "amount" in n)

    result = _run("expense-report", "bad", "--step", f"verify {amount} page=expense-list")

    assert result.exit_code != EXIT_SUCCESS
    assert "verify step to name a control and bind a parameter" in result.output


def test_a_verify_step_must_bind_a_parameter_the_flow_declares(_config_dir: Path) -> None:
    names = _store(_config_dir)
    amount = next(n for n in names if "amount" in n)

    result = _run(
        "expense-report", "bad", "--param", "amount:text",
        "--step", f"verify {amount} page=expense-list",
    )  # fmt: skip

    assert result.exit_code != EXIT_SUCCESS


def test_a_verify_step_on_a_button_is_refused_when_the_flow_is_saved(_config_dir: Path) -> None:
    names = _store(_config_dir)
    add_line = next(n for n in names if "add" in n)

    result = _run(
        "expense-report", "bad", "--param", "amount:text",
        "--step", f"verify {add_line} page=expense-list bind=amount",
    )  # fmt: skip

    assert result.exit_code != EXIT_SUCCESS
    assert "to name a field" in result.output


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


def test_the_rendered_help_keeps_its_example_lines_and_the_quoting_of_a_dollar() -> None:
    from blumkin import help_text

    text = help_text.BROWSER_MODEL_FLOW_ADD_EPILOG

    assert " \\\n" in text  # a backslash then a newline: the examples are not joined into one line
    assert "column=\\$day" in text
    result = CliRunner().invoke(main, ["browser", "model", "flow", "add", "--help"])
    assert "--param hours:text --param day:text" in result.output


def test_an_unknown_action_an_empty_value_and_too_many_steps_are_usage_errors() -> None:
    for steps, match in (
        (["sumbit expense.add"], "start with one of"),
        (["click a page="], "to have a value"),
        (["click a bind="], "to have a value"),
        (["click a"] * 201, "at most 200 steps"),
    ):
        with pytest.raises(FlowSpecError, match=match):
            build_flow("f", [], steps)
    build_flow("f", [], ["cell row=1 column=2 value="])  # the one empty value that means something


def test_the_cli_says_usage_error_for_a_bad_spec_and_model_invalid_for_a_bad_flow(
    _config_dir: Path,
) -> None:
    _store(_config_dir)

    spec = _run("expense-report", "f", "--step", "sumbit x")
    model = _run("expense-report", "f", "--step", "click not.recorded page=expense-list")

    assert spec.exit_code == EXIT_USAGE and "usage_error" in spec.output + (spec.stderr or "")
    assert model.exit_code == EXIT_OTHER and "model_invalid" in model.output + (model.stderr or "")


def test_a_click_on_a_finalizing_control_is_refused_when_the_flow_is_written(
    _config_dir: Path,
) -> None:
    names = _store(_config_dir)
    submit = next(n for n in names if "submit" in n)

    for action in ("click", "press"):
        result = _run(
            "expense-report", "f", "--yes", "--step", f"{action} {submit} page=expense-list"
        )
        assert result.exit_code == EXIT_USAGE, result.output
        assert "finalizing" in result.output + (result.stderr or "")
    assert list_versions(default_store(_config_dir), "expense-report") == [1]


def test_a_replaced_flow_keeps_its_place_and_the_pages_keep_their_recorded_at(
    _config_dir: Path,
) -> None:
    names = _store(_config_dir)
    add_line = next(n for n in names if "add" in n)
    step = f"click {add_line} page=expense-list"
    for flow in ("one", "two", "three"):
        assert _run("expense-report", flow, "--yes", "--step", step).exit_code == EXIT_SUCCESS

    assert _run("expense-report", "two", "--yes", "--replace", "--step", step).exit_code == 0

    stored = load_model(default_store(_config_dir), "expense-report")
    assert stored is not None
    assert [f.name for f in stored.flows] == ["one", "two", "three"]
    assert stored.recorded_at == "2026-10-25T12:00:00Z" and stored.blumkin_version == "1.0.0"


def test_a_save_from_a_stale_read_is_refused_instead_of_undoing_a_newer_version(
    _config_dir: Path,
) -> None:
    from blumkin.browser.model import SiteModelError

    names = _store(_config_dir)
    store = default_store(_config_dir)
    stale = load_model(store, "expense-report")
    assert stale is not None
    save_model(store, stale)  # a concurrent writer saved version 2

    with pytest.raises(SiteModelError, match="still be the newest model"):
        save_model(store, stale, base_version=stale.version)

    assert list_versions(store, "expense-report") == [1, 2]
    assert names


def test_a_step_runs_on_the_accessible_name_the_model_records_for_its_control(
    _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.browser.model import FlowStep

    names = _store(_config_dir)
    model = load_model(default_store(_config_dir), "expense-report")
    add_line = next(n for n in names if "add" in n)
    amount = next(n for n in names if "amount" in n)
    seen: list[tuple[str, str, Any]] = []
    monkeypatch.setattr(
        actions, "click_task", lambda c, t, name, role=None, **k: seen.append(("click", name, role))
    )
    monkeypatch.setattr(
        actions,
        "fill_task",
        lambda c, t, label, value, **k: seen.append(("fill", label, k.get("role"))),
    )
    run = ledger_ops._default_run_step("expense-report", model)

    run(FlowStep(action="click", control=add_line, expect=None, page="expense-list"), {})
    run(
        FlowStep(action="fill", control=amount, expect=None, page="expense-list", bind="x"),
        {"x": "1"},
    )
    run(FlowStep(action="click", control="Unrecorded name", expect=None, page=None), {})

    assert seen[0] == ("click", "Add line", "button")
    assert seen[1][:2] == ("fill", "Amount")
    assert seen[2] == ("click", "Unrecorded name", "button")  # a name the model lacks, as before


def _two_page_model() -> Any:
    from blumkin.browser.model import (
        Control,
        ControlKind,
        ControlSafety,
        Locator,
        LocatorStrategy,
        Page,
        SiteModel,
    )

    def control(safety: ControlSafety, role_name: str) -> Control:
        return Control(
            name="save",
            kind=ControlKind.BUTTON,
            safety=safety,
            locators=(Locator(strategy=LocatorStrategy.ROLE_NAME, value=role_name),),
        )

    def page(page_id: str, safety: ControlSafety, role_name: str) -> Page:
        return Page(
            page_id=page_id,
            url_template="/x",
            fingerprint=("button:a", "button:b", "button:c"),
            controls=(control(safety, role_name),),
        )

    return SiteModel(
        blumkin_version="1.0.0",
        flows=(),
        hosts=("erp.example.com",),
        pages=(
            page("draft", ControlSafety.SAFE, "button:Save Draft"),
            page("final", ControlSafety.FINALIZING, "button:Submit Report"),
        ),
        recorded_at="2026-10-25T12:00:00Z",
        task="expense-report",
        version=1,
    )


def test_a_spec_that_names_no_control_for_a_click_fill_press_or_upload_is_refused() -> None:
    for action in ("click", "fill", "press", "upload"):
        with pytest.raises(FlowSpecError, match=f"{action}\\) to name the control"):
            build_flow("f", [], [f"{action} page=expense-list"])
    build_flow("f", [], ["goto", 'handoff instruction="x"'])  # these need none


def test_the_finalizing_check_follows_the_page_a_step_names() -> None:
    from blumkin.browser.model_commands import _refuse_finalizing_clicks

    model = _two_page_model()

    _refuse_finalizing_clicks(model, build_flow("ok", [], ["click save page=draft"]))
    with pytest.raises(FlowSpecError, match="finalizing"):
        _refuse_finalizing_clicks(model, build_flow("no", [], ["click save page=final"]))
    with pytest.raises(FlowSpecError, match="finalizing"):  # unscoped: any page counts
        _refuse_finalizing_clicks(model, build_flow("any", [], ["click save"]))


def test_a_step_resolves_its_control_on_the_page_it_names_and_matches_the_whole_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.browser.model import FlowStep

    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        actions, "click_task", lambda c, t, name, role=None, **k: seen.append((name, role))
    )
    fields: list[dict[str, Any]] = []
    monkeypatch.setattr(
        actions, "fill_task", lambda c, t, label, value, **k: fields.append({"label": label, **k})
    )
    run = ledger_ops._default_run_step("expense-report", _two_page_model())

    run(FlowStep(action="click", control="save", expect=None, page="final"), {})
    run(FlowStep(action="click", control="save", expect=None, page="draft"), {})
    run(FlowStep(action="fill", control="save", expect=None, page="draft", bind="v"), {"v": "1"})
    run(FlowStep(action="fill", control="Unrecorded", expect=None, page=None, bind="v"), {"v": "1"})

    assert seen == [("Submit Report", "button"), ("Save Draft", "button")]
    assert fields[0] == {
        "label": "Save Draft",
        "role": "button",
        "exact": True,
        "settle": False,
        "nth": None,
    }
    assert fields[1] == {"label": "Unrecorded", "settle": False, "nth": None}  # as written


def test_a_select_spec_names_its_control_and_binds_or_gives_the_option() -> None:
    flow = build_flow(
        "item",
        ["kind:choice=Meals|Taxi"],
        ["select expense.type bind=kind", "select expense.type value=Meals"],
    )

    first, second = flow.steps
    assert (first.action, first.control, first.bind) == ("select", "expense.type", "kind")
    assert (second.control, second.value, second.bind) == ("expense.type", "Meals", None)

    with pytest.raises(FlowSpecError, match="name the control"):
        build_flow("item", [], ["select bind=kind"])


def test_a_pick_spec_can_name_a_role_and_a_prefix_match() -> None:
    flow = build_flow("lov", ["name:text"], ["pick bind=name role=gridcell match=prefix"])

    (step,) = flow.steps
    assert (step.role, step.match, step.bind) == ("gridcell", "prefix", "name")


def test_pick_is_a_flow_action_that_takes_a_bind_or_a_value_and_no_control() -> None:
    flow = build_flow("lov", ["city:text"], ["pick bind=city", "pick value='Boston, MA'"])

    first, second = flow.steps
    assert (first.action, first.bind, first.control) == ("pick", "city", None)
    assert (second.value, second.bind) == ("Boston, MA", None)


def test_a_step_spec_can_say_which_of_several_same_named_controls_it_means() -> None:
    flow = build_flow(
        "attach",
        ["title:text", "file:file"],
        ["fill title bind=title nth=2", "upload file bind=file nth=2", "click ok nth=1"],
    )

    assert [step.nth for step in flow.steps] == [2, 2, 1]


@pytest.mark.parametrize("text", ["two", "-1", "1.5", "\u0662"])
def test_a_non_numeric_nth_is_refused_when_the_spec_is_read(text: str) -> None:
    with pytest.raises(FlowSpecError, match="nth in step 1 to be a whole number"):
        build_flow("attach", [], [f"click ok nth={text}"])


def test_the_runner_passes_a_step_s_nth_on_to_every_action_that_takes_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.browser.model import FlowStep

    seen: list[tuple[str, int | None]] = []
    for name in ("click_task", "fill_task", "select_task", "upload_task", "verify_task"):
        monkeypatch.setattr(
            actions,
            name,
            lambda *a, _n=name, **k: seen.append((_n, k.get("nth"))) or {"status": "match"},
        )
    run = ledger_ops._default_run_step("expense-report", None)
    base = {"expect": None, "page": None}

    run(FlowStep(action="click", control="OK", nth=3, **base), {})
    run(FlowStep(action="fill", control="Title", bind="v", nth=2, **base), {"v": "x"})
    run(FlowStep(action="select", control="Type", value="Meals", nth=4, **base), {})
    run(FlowStep(action="upload", control="File", bind="f", nth=2, **base), {"f": "r.pdf"})
    run(FlowStep(action="verify", control="Title", bind="v", nth=2, **base), {"v": "x"})
    run(FlowStep(action="click", control="Save", **base), {})

    assert seen == [
        ("click_task", 3),
        ("fill_task", 2),
        ("select_task", 4),
        ("upload_task", 2),
        ("verify_task", 2),
        ("click_task", None),
    ]


def test_a_press_step_with_a_value_sends_that_key_and_one_without_still_clicks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.browser.model import FlowStep

    pressed: list[tuple[str, str, int | None]] = []
    clicked: list[str] = []
    monkeypatch.setattr(
        actions,
        "press_task",
        lambda c, t, label, key, **k: pressed.append((label, key, k.get("nth"))),
    )
    monkeypatch.setattr(actions, "click_task", lambda c, t, name, **k: clicked.append(name))
    run = ledger_ops._default_run_step("expense-report", None)
    base = {"expect": None, "page": None}

    run(FlowStep(action="press", control="Location", value="Tab", nth=2, **base), {})
    run(FlowStep(action="press", control="Open", **base), {})

    assert pressed == [("Location", "Tab", 2)]
    assert clicked == ["Open"]


def test_a_fill_step_with_a_literal_value_types_it_and_needs_no_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.browser.model import FlowStep

    typed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        actions, "fill_task", lambda c, t, label, value, **k: typed.append((label, value))
    )
    run = ledger_ops._default_run_step("expense-report", None)
    base = {"expect": None, "page": None}

    run(FlowStep(action="fill", control="Project", value="P-1", **base), {})
    run(FlowStep(action="fill", control="Amount", bind="amount", **base), {"amount": "9.50"})

    assert typed == [("Project", "P-1"), ("Amount", "9.50")]


def test_a_fill_or_press_spec_reads_its_value() -> None:
    flow = build_flow(
        "item",
        ["amount:text"],
        ["fill project value=P-1", "fill amount bind=amount", "press location value=Tab"],
    )

    assert [step.value for step in flow.steps] == ["P-1", None, "Tab"]


def test_an_upload_step_whose_control_is_recorded_as_a_button_finds_the_input_by_its_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.browser.model import FlowStep

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        actions, "upload_task", lambda c, t, label, path, **k: seen.append({"label": label, **k})
    )
    run = ledger_ops._default_run_step("expense-report", _two_page_model())

    run(FlowStep(action="upload", control="save", expect=None, page="draft", bind="f"), {"f": "r"})

    assert seen == [{"label": "Save Draft", "settle": False, "nth": None}]  # no role, no exact


def test_an_upload_step_recorded_by_its_label_keeps_the_whole_name_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blumkin.browser import actions, ledger_ops
    from blumkin.browser.model import FlowStep

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        actions, "upload_task", lambda c, t, label, path, **k: seen.append({"label": label, **k})
    )
    monkeypatch.setattr(
        ledger_ops, "_control_ref", lambda site, control, page, **k: ("Receipt", None, True)
    )
    run = ledger_ops._default_run_step("expense-report", _two_page_model())

    run(FlowStep(action="upload", control="save", expect=None, page="draft", bind="f"), {"f": "r"})

    assert seen == [{"label": "Receipt", "exact": True, "settle": False, "nth": None}]


# ------------------------------------------------------------------ model flows (#695)


def _flows(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", "browser", "model", "flows", *args])


def test_model_flows_lists_each_flow_with_its_params_and_steps(_config_dir: Path) -> None:
    names = _store(_config_dir)
    amount = next(n for n in names if "amount" in n)
    assert (
        _run(
            "expense-report", "fill-amount", "--yes", "--param", "total:text", "--step",
            f"fill {amount} page=expense-list bind=total",
        ).exit_code
        == EXIT_SUCCESS
    )  # fmt: skip

    payload = _json(_flows("expense-report"))

    assert payload["version"] == max(list_versions(default_store(_config_dir), "expense-report"))
    (flow,) = payload["flows"]
    assert flow["params"] == [{"kind": "text", "name": "total"}]
    assert flow["steps"][0]["bind"] == "total" and flow["steps"][0]["action"] == "fill"


def test_model_flows_names_one_flow_params_and_steps_and_refuses_an_unknown_name(
    _config_dir: Path,
) -> None:
    names = _store(_config_dir)
    add_line = next(n for n in names if "add" in n)
    assert (
        _run(
            "expense-report", "go", "--yes", "--step",
            f"click {add_line} page=expense-list expect_heading='Edit Report'",
        ).exit_code
        == EXIT_SUCCESS
    )  # fmt: skip

    one = _json(_flows("expense-report", "go"))
    missing = _flows("expense-report", "nope")
    text = CliRunner().invoke(main, ["browser", "model", "flows", "expense-report", "go"])

    (flow,) = one["flows"]
    assert flow["name"] == "go" and flow["params"] == []
    assert flow["steps"] == [
        {
            "action": "click",
            "control": add_line,
            "page": "expense-list",
            "expect_heading": "Edit Report",
        }
    ]
    assert missing.exit_code == EXIT_USAGE and "go" in _json(missing)["message"]
    assert "1. click" in text.output and "expect_heading=Edit Report" in text.output


def test_model_flows_without_a_saved_model_is_not_found(_config_dir: Path) -> None:
    assert _flows("expense-report").exit_code == EXIT_NOT_FOUND
