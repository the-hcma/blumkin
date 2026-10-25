"""Site model store: a task's recorded interaction map, kept on disk (issue #485)."""

from __future__ import annotations

import copy
import json
import os
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.model import (
    SCHEMA_VERSION,
    Control,
    ControlKind,
    ControlSafety,
    Flow,
    FlowStep,
    Locator,
    LocatorStrategy,
    Page,
    SiteModel,
    SiteModelError,
    discard_draft,
    list_versions,
    load_draft,
    load_model,
    load_version,
    save_draft,
    save_model,
    update_draft,
)
from blumkin.browser.profiles import BrowserProfileError


def _model(task: str = "expense-report", **overrides: Any) -> SiteModel:
    fields: dict[str, Any] = {
        "blumkin_version": "1.2.3",
        "flows": (
            Flow(
                name="add-item",
                steps=(
                    FlowStep(action="click", control="expense.add", page="list", expect="form"),
                    FlowStep(action="fill", control="expense.amount", page="form", expect=None),
                ),
            ),
        ),
        "hosts": ("erp.example.com",),
        "pages": (
            Page(
                page_id="list",
                url_template="/expenses/{id}",
                fingerprint=("button:Add line", "heading:Expenses"),
                controls=(
                    Control(
                        name="expense.add",
                        kind=ControlKind.BUTTON,
                        safety=ControlSafety.SAFE,
                        locators=(
                            Locator(strategy=LocatorStrategy.ROLE_NAME, value="button:Add line"),
                        ),
                    ),
                ),
            ),
            Page(
                page_id="form",
                url_template="/expenses/{id}/line",
                fingerprint=("textbox:Amount",),
                controls=(
                    Control(
                        name="expense.amount",
                        kind=ControlKind.TEXT,
                        safety=ControlSafety.SAFE,
                        locators=(
                            Locator(strategy=LocatorStrategy.LABEL, value="Amount"),
                            Locator(strategy=LocatorStrategy.CSS, value="input#amount"),
                        ),
                    ),
                    Control(
                        name="expense.submit",
                        kind=ControlKind.BUTTON,
                        safety=ControlSafety.FINALIZING,
                        locators=(
                            Locator(strategy=LocatorStrategy.ROLE_NAME, value="button:Submit"),
                        ),
                    ),
                ),
            ),
        ),
        "recorded_at": "2026-10-24T12:00:00Z",
        "task": task,
        "version": 0,
    }
    fields.update(overrides)
    return SiteModel(**fields)


def test_a_saved_model_round_trips_with_its_version_assigned(tmp_path: Path) -> None:
    saved = save_model(tmp_path, _model())
    assert saved.version == 1
    loaded = load_model(tmp_path, "expense-report")
    assert loaded == saved
    assert loaded is not None
    assert loaded.pages[1].controls[1].safety is ControlSafety.FINALIZING
    assert loaded.flows[0].steps[1].expect is None


def test_saving_again_keeps_every_earlier_version_listable_and_loadable(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    second = save_model(tmp_path, _model(recorded_at="2026-10-25T12:00:00Z"))
    save_model(tmp_path, _model(recorded_at="2026-10-26T12:00:00Z"))
    assert list_versions(tmp_path, "expense-report") == [1, 2, 3]
    assert load_version(tmp_path, "expense-report", 2) == second
    assert load_version(tmp_path, "expense-report", 1).recorded_at == "2026-10-24T12:00:00Z"
    current = load_model(tmp_path, "expense-report")
    assert current is not None
    assert current.version == 3


def test_a_task_with_no_model_loads_as_none_and_lists_no_versions(tmp_path: Path) -> None:
    assert load_model(tmp_path, "time-sheet") is None
    assert list_versions(tmp_path, "time-sheet") == []
    with pytest.raises(SiteModelError, match="Expected version 4"):
        load_version(tmp_path, "time-sheet", 4)


def test_models_are_kept_per_task(tmp_path: Path) -> None:
    save_model(tmp_path, _model("expense-report"))
    save_model(tmp_path, _model("time-sheet", hosts=("hr.example.com",)))
    assert load_model(tmp_path, "time-sheet").hosts == ("hr.example.com",)  # type: ignore[union-attr]
    assert list_versions(tmp_path, "expense-report") == [1]


def test_the_store_is_private(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    root = tmp_path / "browser-models"
    task_dir = root / "expense-report"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(task_dir.stat().st_mode) == 0o700
    for path in (task_dir / "model.json", *(task_dir / "history").iterdir()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_a_symlinked_store_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "browser-models").symlink_to(elsewhere)
    with pytest.raises(BrowserProfileError, match="symlink"):
        save_model(tmp_path, _model())
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_model_file_is_not_followed(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    current = tmp_path / "browser-models" / "expense-report" / "model.json"
    target = tmp_path / "other.json"
    target.write_text(current.read_text())
    current.unlink()
    current.symlink_to(target)
    with pytest.raises(SiteModelError, match="symlink|regular file"):
        load_model(tmp_path, "expense-report")


def test_a_task_name_that_could_escape_the_store_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BrowserProfileError):
        save_model(tmp_path, _model("../escape"))
    with pytest.raises(BrowserProfileError):
        load_model(tmp_path, "Expense_Report")


def test_a_crash_leftover_temp_file_does_not_break_a_later_save(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    task_dir = tmp_path / "browser-models" / "expense-report"
    (task_dir / "model.json.tmp").write_text("{torn")
    (task_dir / "history" / "model-0002.json.tmp").write_text("{torn")
    save_model(tmp_path, _model())
    assert list_versions(tmp_path, "expense-report") == [1, 2]
    assert load_model(tmp_path, "expense-report") is not None


def test_a_corrupt_current_file_is_an_error_not_a_silent_empty_model(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    (tmp_path / "browser-models" / "expense-report" / "model.json").write_text('{"schema_ver')
    with pytest.raises(SiteModelError, match="JSON"):
        load_model(tmp_path, "expense-report")


def test_a_model_from_a_newer_blumkin_names_the_versions(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    path = tmp_path / "browser-models" / "expense-report" / "model.json"
    payload = json.loads(path.read_text())
    payload["schema_version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(payload))
    with pytest.raises(
        SiteModelError, match=rf"schema_version {SCHEMA_VERSION}.*{SCHEMA_VERSION + 1}"
    ):
        load_model(tmp_path, "expense-report")


def _payload(tmp_path: Path) -> dict[str, Any]:
    save_model(tmp_path, _model())
    path = tmp_path / "browser-models" / "expense-report" / "model.json"
    return json.loads(path.read_text())


def _load_payload(tmp_path: Path, payload: dict[str, Any]) -> SiteModel | None:
    path = tmp_path / "browser-models" / "expense-report" / "model.json"
    path.write_text(json.dumps(payload))
    return load_model(tmp_path, "expense-report")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(cookies=[{"name": "sid"}]),
        lambda p: p["pages"][0].update(value="12.50"),
        lambda p: p["pages"][0]["controls"][0].update(typed_value="secret"),
        lambda p: p["pages"][0]["controls"][0]["locators"][0].update(text_content="x"),
        lambda p: p["flows"][0].update(example_input="y"),
        lambda p: p["flows"][0]["steps"][0].update(value="z"),
    ],
    ids=["top", "page", "control", "locator", "flow", "step"],
)
def test_a_field_outside_the_schema_is_refused_so_values_cannot_be_stored(
    tmp_path: Path, mutate: Any
) -> None:
    payload = _payload(tmp_path)
    mutate(payload)
    with pytest.raises(SiteModelError, match="unexpected"):
        _load_payload(tmp_path, payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["pages"][0].update(url_template="/expenses?id=7"),
        lambda p: p["pages"][0].update(url_template="/expenses#frag"),
        lambda p: p["pages"][0].update(url_template="https://erp.example.com/x"),
        lambda p: p["pages"][0].update(url_template="/a\\b"),
        lambda p: p["pages"][0].update(url_template="/a@b"),
        lambda p: p["pages"][0].update(url_template="/%2e%2e/x"),
        lambda p: p["pages"][0].update(url_template="x"),
        lambda p: p.update(hosts=["erp.example.com:8443"]),
        lambda p: p.update(hosts=["https://erp.example.com"]),
        lambda p: p.update(hosts=["ERP.example.com"]),
        lambda p: p.update(hosts=["erp.example.com/path"]),
        lambda p: p.update(hosts=["user@erp.example.com"]),
        lambda p: p.update(hosts=["*.example.com"]),
        lambda p: p.update(hosts=[]),
        lambda p: p["pages"][0].update(fingerprint=[]),
        lambda p: p["pages"][0]["controls"][0]["locators"].clear(),
        lambda p: p["pages"][0]["controls"][0].update(name="Bad Name"),
        lambda p: p["pages"][0]["controls"][0].update(kind="slider"),
        lambda p: p["pages"][0]["controls"][0].update(safety="maybe"),
        lambda p: p["pages"][0]["controls"][0]["locators"][0].update(strategy="xpath"),
        lambda p: p.update(recorded_at="yesterday"),
        lambda p: p.update(task="time-sheet"),
        lambda p: p.update(pages=[]),
    ],
    ids=[
        "query",
        "fragment",
        "absolute-url",
        "backslash",
        "userinfo-at",
        "percent",
        "no-leading-slash",
        "host-port",
        "host-scheme",
        "host-uppercase",
        "host-path",
        "host-userinfo",
        "host-wildcard",
        "no-hosts",
        "no-fingerprint",
        "no-locators",
        "control-name",
        "control-kind",
        "control-safety",
        "locator-strategy",
        "timestamp",
        "task-mismatch",
        "no-pages",
    ],
)
def test_a_malformed_model_is_refused_with_an_expected_got_message(
    tmp_path: Path, mutate: Any
) -> None:
    payload = _payload(tmp_path)
    mutate(payload)
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


@pytest.mark.parametrize("bad", ["a‮b", "a\x00b", "a\nb", "x" * 201])
def test_text_with_control_or_bidirectional_characters_or_excess_length_is_refused(
    tmp_path: Path, bad: str
) -> None:
    payload = _payload(tmp_path)
    payload["pages"][0]["controls"][0]["locators"][0]["value"] = bad
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


def test_duplicate_page_ids_and_control_names_are_refused(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    duplicate = copy.deepcopy(payload)
    duplicate["pages"][1]["page_id"] = "list"
    with pytest.raises(SiteModelError, match="duplicate|Expected unique"):
        _load_payload(tmp_path, duplicate)
    duplicate = copy.deepcopy(payload)
    duplicate["pages"][1]["controls"][1]["name"] = "expense.amount"
    with pytest.raises(SiteModelError, match="duplicate|Expected unique"):
        _load_payload(tmp_path, duplicate)


def test_a_flow_step_must_name_a_control_and_page_that_exist(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    broken = copy.deepcopy(payload)
    broken["flows"][0]["steps"][0]["control"] = "expense.missing"
    with pytest.raises(SiteModelError, match="Expected a control"):
        _load_payload(tmp_path, broken)
    broken = copy.deepcopy(payload)
    broken["flows"][0]["steps"][0]["page"] = "nowhere"
    with pytest.raises(SiteModelError, match="Expected a page"):
        _load_payload(tmp_path, broken)


def test_too_many_items_are_refused(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["pages"][0]["fingerprint"] = [f"button:b{n}" for n in range(1000)]
    with pytest.raises(SiteModelError, match="Expected at most"):
        _load_payload(tmp_path, payload)


def test_an_oversize_file_is_refused_without_parsing_it(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    path = tmp_path / "browser-models" / "expense-report" / "model.json"
    path.write_text(" " * (3 * 1024 * 1024))
    with pytest.raises(SiteModelError, match="too large"):
        load_model(tmp_path, "expense-report")


def test_hosts_must_pass_the_tasks_allowed_hosts_check(tmp_path: Path) -> None:
    with pytest.raises(SiteModelError, match="evil.example.org"):
        save_model(
            tmp_path,
            _model(hosts=("erp.example.com", "evil.example.org")),
            host_allowed=lambda host: host.endswith(".example.com"),
        )
    assert list_versions(tmp_path, "expense-report") == []


def test_a_failed_save_leaves_the_previous_model_untouched(tmp_path: Path) -> None:
    saved = save_model(tmp_path, _model())
    with pytest.raises(SiteModelError):
        save_model(tmp_path, _model(hosts=()))
    assert load_model(tmp_path, "expense-report") == saved
    assert list_versions(tmp_path, "expense-report") == [1]


def test_files_are_written_atomically_without_leaving_temp_files(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    names = {p.name for p in (tmp_path / "browser-models" / "expense-report").rglob("*")}
    assert not any(name.endswith(".tmp") for name in names)
    assert os.listdir(tmp_path / "browser-models" / "expense-report" / "history") == [
        "model-0001.json"
    ]


def test_the_stored_json_is_deterministic(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    first = (tmp_path / "browser-models" / "expense-report" / "model.json").read_text()
    save_model(tmp_path, _model())
    second = (tmp_path / "browser-models" / "expense-report" / "model.json").read_text()
    assert json.loads(first)["version"] == 1 and json.loads(second)["version"] == 2
    assert first.replace('"version": 1', '"version": 2') == second


# --- hardening from the independent review ---------------------------------------------------


def _write_current(tmp_path: Path, payload: Any) -> None:
    path = tmp_path / "browser-models" / "expense-report" / "model.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["pages"][0]["controls"][0]["locators"][0].update(
            strategy="label", value="Amount=12.50"
        ),
        lambda p: p["pages"][0]["controls"][0]["locators"][0].update(
            strategy="role_name", value="button:'hunter2'"
        ),
        lambda p: p["pages"][0]["fingerprint"].append("textbox:Card 4111111111111111"),
        lambda p: p["pages"][0]["controls"][0]["locators"][0].update(
            strategy="css", value="input[value='1234567']"
        ),
        lambda p: p["flows"][0]["steps"][0].update(expect="Total 12345678"),
        lambda p: p["pages"][0].update(url_template="/orders/Zm9vYmFyU0VTU0lPTg/line"),
        lambda p: p["pages"][0].update(url_template="/orders/2026/line"),
    ],
    ids=[
        "equals-in-label",
        "quotes-in-role-name",
        "digit-run-in-fingerprint",
        "digit-run-in-css",
        "digit-run-in-expect",
        "token-path-segment",
        "numeric-path-segment",
    ],
)
def test_values_that_look_like_typed_data_are_refused_even_inside_allowed_fields(
    tmp_path: Path, mutate: Any
) -> None:
    payload = _payload(tmp_path)
    mutate(payload)
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


@pytest.mark.parametrize(
    "template", ["/a/../x", "/a//x", "/a/{}/x", "/a/{b/x", "/a/b}/x", "/a/./x"]
)
def test_malformed_url_templates_are_refused(tmp_path: Path, template: str) -> None:
    payload = _payload(tmp_path)
    payload["pages"][0]["url_template"] = template
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_schema_version_must_be_the_integer_itself(tmp_path: Path, version: Any) -> None:
    payload = _payload(tmp_path)
    payload["schema_version"] = version
    with pytest.raises(SiteModelError, match="schema_version"):
        _load_payload(tmp_path, payload)


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "10.0.0.1", "intranet", "a.1234", "1.1.1.1", "erp.example.com."],
)
def test_hosts_that_are_not_public_looking_names_are_refused(tmp_path: Path, host: str) -> None:
    payload = _payload(tmp_path)
    payload["hosts"] = [host]
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


def test_loading_can_check_hosts_against_the_tasks_allowed_hosts_too(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    with pytest.raises(SiteModelError, match="erp.example.com"):
        load_model(tmp_path, "expense-report", host_allowed=lambda host: False)
    assert load_model(tmp_path, "expense-report", host_allowed=lambda host: True) is not None


def test_deeply_nested_json_is_a_model_error_not_a_crash(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    _write_current(tmp_path, "[" * 100_000 + "]" * 100_000)
    with pytest.raises(SiteModelError, match="Expected"):
        load_model(tmp_path, "expense-report")


@pytest.mark.parametrize(
    "text",
    [
        '{"task": "expense-report", "task": "time-sheet"}',
        '{"version": NaN}',
        '{"version": Infinity}',
    ],
    ids=["duplicate-key", "nan", "infinity"],
)
def test_duplicate_keys_and_non_finite_numbers_are_refused(tmp_path: Path, text: str) -> None:
    save_model(tmp_path, _model())
    _write_current(tmp_path, text)
    with pytest.raises(SiteModelError, match="Expected"):
        load_model(tmp_path, "expense-report")


def test_a_fifo_in_place_of_the_model_file_does_not_hang_the_loader(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    path = tmp_path / "browser-models" / "expense-report" / "model.json"
    path.unlink()
    os.mkfifo(path)
    outcome: list[BaseException] = []

    def attempt() -> None:
        try:
            load_model(tmp_path, "expense-report")
        except BaseException as exc:  # noqa: BLE001 - the thread reports whatever it got
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the loader blocked on a FIFO"
    assert isinstance(outcome[0], SiteModelError)
    assert "regular file" in str(outcome[0])


@pytest.mark.parametrize("which", ["root", "task"])
def test_a_symlinked_store_or_task_directory_is_not_followed_when_loading(
    tmp_path: Path, which: str
) -> None:
    save_model(tmp_path, _model())
    root = tmp_path / "browser-models"
    real = tmp_path / "real"
    if which == "root":
        root.rename(real)
        root.symlink_to(real)
    else:
        (root / "expense-report").rename(real)
        (root / "expense-report").symlink_to(real)
    with pytest.raises(SiteModelError, match="symlink"):
        load_model(tmp_path, "expense-report")
    with pytest.raises(SiteModelError, match="symlink"):
        list_versions(tmp_path, "expense-report")


def test_filesystem_errors_while_saving_are_model_errors(tmp_path: Path) -> None:
    (tmp_path / "browser-models").write_text("a file where the store directory belongs")
    with pytest.raises(SiteModelError, match="Expected"):
        save_model(tmp_path, _model())


def test_a_temp_path_that_is_a_directory_is_a_model_error_not_a_crash(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    (tmp_path / "browser-models" / "expense-report" / "model.json.tmp").mkdir()
    with pytest.raises(SiteModelError, match="Expected"):
        save_model(tmp_path, _model())


@pytest.mark.parametrize("bad", ["a b", "a b", "a b", "é"])
def test_line_separators_nbsp_and_unnormalized_text_are_refused(tmp_path: Path, bad: str) -> None:
    payload = _payload(tmp_path)
    payload["pages"][0]["fingerprint"].append(f"button:{bad}")
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


def test_non_ascii_digits_do_not_count_as_a_version_or_a_timestamp(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    history = tmp_path / "browser-models" / "expense-report" / "history"
    (history / "model-٠٠٠٥.json").write_text("{}")
    assert list_versions(tmp_path, "expense-report") == [1]
    payload = _payload(tmp_path)
    payload["recorded_at"] = "٢026-10-24T12:00:00Z"
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


def test_a_history_file_whose_version_disagrees_with_its_name_is_refused(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    save_model(tmp_path, _model())
    history = tmp_path / "browser-models" / "expense-report" / "history"
    payload = json.loads((history / "model-0002.json").read_text())
    payload["version"] = 7
    (history / "model-0002.json").write_text(json.dumps(payload))
    with pytest.raises(SiteModelError, match="Expected version 2"):
        load_version(tmp_path, "expense-report", 2)


def test_a_current_file_behind_the_newest_history_rolls_forward_to_the_newest_version(
    tmp_path: Path,
) -> None:
    save_model(tmp_path, _model())
    save_model(tmp_path, _model(recorded_at="2026-10-25T12:00:00Z"))
    task_dir = tmp_path / "browser-models" / "expense-report"
    (task_dir / "model.json").write_text((task_dir / "history" / "model-0001.json").read_text())
    loaded = load_model(tmp_path, "expense-report")
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.recorded_at == "2026-10-25T12:00:00Z"


def test_a_current_file_ahead_of_the_history_is_refused(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    task_dir = tmp_path / "browser-models" / "expense-report"
    payload = json.loads((task_dir / "model.json").read_text())
    payload["version"] = 5
    (task_dir / "model.json").write_text(json.dumps(payload))
    with pytest.raises(SiteModelError, match="Expected model.json to be version 1"):
        load_model(tmp_path, "expense-report")


def test_the_returned_model_is_what_a_reload_returns(tmp_path: Path) -> None:
    messy = _model()
    page = messy.pages[0]
    messy = _model(
        pages=(
            Page(
                page_id=page.page_id,
                url_template=page.url_template,
                fingerprint=("heading:Expenses", "button:Add line", "button:Add line"),
                controls=page.controls,
            ),
            messy.pages[1],
        )
    )
    saved = save_model(tmp_path, messy)
    assert saved == load_model(tmp_path, "expense-report")
    assert saved.pages[0].fingerprint == ("button:Add line", "heading:Expenses")


def test_a_malformed_dataclass_is_a_model_error_not_an_attribute_error(tmp_path: Path) -> None:
    broken = _model(
        pages=(
            Page(
                page_id="list",
                url_template="/x",
                fingerprint=("a:b",),
                controls=(Control(name="c.x", kind="slider", safety="safe", locators=()),),  # type: ignore[arg-type]
            ),
        )
    )
    with pytest.raises(SiteModelError, match="Expected"):
        save_model(tmp_path, broken)


def test_an_error_about_a_huge_value_does_not_render_the_whole_value(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["task"] = ["x" * 1_000_000]
    with pytest.raises(SiteModelError) as caught:
        _load_payload(tmp_path, payload)
    assert len(str(caught.value)) < 400


def test_the_module_docstring_does_not_promise_more_than_the_schema_enforces() -> None:
    from blumkin.browser import model

    doc = " ".join((model.__doc__ or "").split())
    assert "never holds" not in doc
    assert "heuristic" in doc


# --- second review of the store --------------------------------------------------------------


def test_blumkin_version_must_look_like_a_version(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["blumkin_version"] = "token=abcDEF"
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)
    for fine in ("1.2.3", "1.2.3+g1a2b3c", "2.0.0-rc1"):
        payload["blumkin_version"] = fine
        assert _load_payload(tmp_path, payload) is not None


@pytest.mark.parametrize(
    "value",
    [
        "Total \u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668",
        "Card 4111 1111 1111 1111",
        "SSN 123-45-6789",
        "Order 2026.10.24.1234",
        "Contact john@example.org",
        "Mark\ufe0f",
    ],
    ids=["arabic-digits", "spaced-card", "dashed-number", "dotted-number", "email", "variation"],
)
def test_more_ways_to_carry_typed_data_are_refused_in_plain_fields(
    tmp_path: Path, value: str
) -> None:
    payload = _payload(tmp_path)
    payload["pages"][0]["fingerprint"].append(f"button:{value}")
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


@pytest.mark.parametrize(
    "name",
    [
        "xk3j9d8f7s6a5q4w3e2r1t",
        "abcdefghijklmnopqrstuv",
        "a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p.q.r.s.t.u.v.w.x.y.z.a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p.q.r.s",
    ],
)
def test_a_logical_name_that_looks_like_a_token_is_refused(tmp_path: Path, name: str) -> None:
    payload = _payload(tmp_path)
    payload["pages"][0]["controls"][0]["name"] = name
    payload["flows"] = []
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)


def test_ordinary_logical_names_are_still_accepted(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    for name in ("timesheet.hours[day]", "expense.line-item_amount", "dialog.cancel"):
        payload["pages"][0]["controls"][0]["name"] = name
        payload["flows"] = []
        assert _load_payload(tmp_path, payload) is not None


def test_a_mixed_case_path_word_that_looks_like_a_token_is_refused(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["pages"][0]["url_template"] = "/aBcDeFgHiJkLm"
    with pytest.raises(SiteModelError, match="Expected"):
        _load_payload(tmp_path, payload)
    payload["pages"][0]["url_template"] = "/expenseReport/{id}"
    assert _load_payload(tmp_path, payload) is not None


def test_a_flow_step_must_name_a_control_that_belongs_to_its_page(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["flows"][0]["steps"][0]["page"] = "form"
    payload["flows"][0]["steps"][0]["control"] = "expense.add"
    with pytest.raises(SiteModelError, match="Expected a control that belongs to page"):
        _load_payload(tmp_path, payload)


def test_duplicate_hosts_are_refused(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["hosts"] = ["erp.example.com", "erp.example.com"]
    with pytest.raises(SiteModelError, match="Expected unique hosts"):
        _load_payload(tmp_path, payload)


def test_a_model_too_large_to_load_is_refused_on_save_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.browser.model._MAX_FILE_BYTES", 1000)
    with pytest.raises(SiteModelError, match="too large"):
        save_model(tmp_path, _model())
    history = tmp_path / "browser-models" / "expense-report" / "history"
    assert not history.exists() or list(history.iterdir()) == []


def test_loading_while_another_process_saves_never_reports_an_interrupted_save(
    tmp_path: Path,
) -> None:
    save_model(tmp_path, _model())
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                load_model(tmp_path, "expense-report")
            except BaseException as exc:  # noqa: BLE001 - the thread reports whatever it got
                errors.append(exc)
                return

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    for _ in range(25):
        save_model(tmp_path, _model())
    stop.set()
    worker.join(30)
    assert not worker.is_alive()
    assert errors == []


def test_list_versions_ignores_directories_links_and_noncanonical_names(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    history = tmp_path / "browser-models" / "expense-report" / "history"
    (history / "model-0777.json").mkdir()
    (history / "model-00001.json").write_text("{}")
    (history / "model-0009.json").symlink_to(history / "model-0001.json")
    assert list_versions(tmp_path, "expense-report") == [1]
    assert save_model(tmp_path, _model()).version == 2


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_a_byte_order_mark_or_utf16_model_file_is_refused(tmp_path: Path, encoding: str) -> None:
    payload = _payload(tmp_path)
    path = tmp_path / "browser-models" / "expense-report" / "model.json"
    path.write_bytes(json.dumps(payload).encode(encoding))
    with pytest.raises(SiteModelError, match="Expected"):
        load_model(tmp_path, "expense-report")


def test_a_non_text_task_in_a_model_is_a_model_error(tmp_path: Path) -> None:
    with pytest.raises(SiteModelError, match="Expected"):
        save_model(tmp_path, _model(task=["a"]))  # type: ignore[arg-type]


# --- first review of the store on GitHub ------------------------------------------------------


def test_a_missing_current_file_recovers_the_newest_saved_version(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    second = save_model(tmp_path, _model(recorded_at="2026-10-25T12:00:00Z"))
    (tmp_path / "browser-models" / "expense-report" / "model.json").unlink()
    assert load_model(tmp_path, "expense-report") == second


def test_a_first_save_interrupted_before_model_json_is_recovered(tmp_path: Path) -> None:
    saved = save_model(tmp_path, _model())
    (tmp_path / "browser-models" / "expense-report" / "model.json").unlink()
    assert load_model(tmp_path, "expense-report") == saved


def test_no_current_file_and_no_history_is_still_none(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    task_dir = tmp_path / "browser-models" / "expense-report"
    (task_dir / "model.json").unlink()
    (task_dir / "history" / "model-0001.json").unlink()
    assert load_model(tmp_path, "expense-report") is None


def test_a_save_syncs_the_directories_it_may_have_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr("blumkin.browser.model._fsync_dir", lambda path: synced.append(path))
    save_model(tmp_path, _model())
    root = tmp_path / "browser-models"
    assert {root / "expense-report" / "history", root / "expense-report", root, tmp_path} <= set(
        synced
    )


# --- the in-progress draft kept between exploration commands ----------------------------------


def test_a_draft_round_trips_and_is_not_a_saved_version(tmp_path: Path) -> None:
    assert load_draft(tmp_path, "expense-report") is None
    save_draft(tmp_path, _model())
    assert load_draft(tmp_path, "expense-report") == _model()
    assert list_versions(tmp_path, "expense-report") == []
    assert load_model(tmp_path, "expense-report") is None
    path = tmp_path / "browser-models" / "expense-report" / "draft.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert discard_draft(tmp_path, "expense-report") is True
    assert discard_draft(tmp_path, "expense-report") is False
    assert load_draft(tmp_path, "expense-report") is None


def test_saving_a_model_leaves_the_draft_alone(tmp_path: Path) -> None:
    save_draft(tmp_path, _model())
    save_model(tmp_path, _model())
    assert load_draft(tmp_path, "expense-report") == _model()


def test_a_corrupt_draft_is_a_model_error(tmp_path: Path) -> None:
    save_draft(tmp_path, _model())
    (tmp_path / "browser-models" / "expense-report" / "draft.json").write_text("{bad")
    with pytest.raises(SiteModelError, match="JSON"):
        load_draft(tmp_path, "expense-report")


def test_an_invalid_draft_is_refused_and_a_symlinked_draft_is_not_followed(tmp_path: Path) -> None:
    with pytest.raises(SiteModelError, match="Expected"):
        save_draft(tmp_path, _model(hosts=()))
    save_draft(tmp_path, _model())
    path = tmp_path / "browser-models" / "expense-report" / "draft.json"
    other = tmp_path / "other.json"
    other.write_text(path.read_text())
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(SiteModelError, match="regular file|symlink"):
        load_draft(tmp_path, "expense-report")


# --- updating the draft atomically -----------------------------------------------------------


def _with_page(page_id: str) -> Any:
    def change(current: SiteModel | None) -> SiteModel:
        base = current or _model(pages=(), flows=())
        extra = Page(
            page_id=page_id,
            url_template="/x",
            fingerprint=("button:Go",),
            controls=(),
        )
        return replace(base, pages=(*base.pages, extra), flows=())

    return change


def test_update_draft_gets_nothing_when_no_draft_or_model_exists_and_stores_the_result(
    tmp_path: Path,
) -> None:
    seen: list[SiteModel | None] = []

    def change(current: SiteModel | None) -> SiteModel:
        seen.append(current)
        return _model()

    update_draft(tmp_path, "expense-report", change)
    assert seen == [None]
    assert load_draft(tmp_path, "expense-report") == _model()


def test_update_draft_starts_from_the_stored_model_when_there_is_no_draft(tmp_path: Path) -> None:
    saved = save_model(tmp_path, _model())
    seen: list[SiteModel | None] = []

    def change(current: SiteModel | None) -> SiteModel:
        seen.append(current)
        return current  # type: ignore[return-value]

    update_draft(tmp_path, "expense-report", change)
    assert seen == [saved]
    assert load_draft(tmp_path, "expense-report") is not None


def test_update_draft_prefers_the_draft_over_the_stored_model(tmp_path: Path) -> None:
    save_model(tmp_path, _model())
    save_draft(tmp_path, _model(recorded_at="2026-10-26T00:00:00Z"))
    seen: list[SiteModel | None] = []

    def change(current: SiteModel | None) -> SiteModel:
        seen.append(current)
        return current  # type: ignore[return-value]

    update_draft(tmp_path, "expense-report", change)
    assert seen[0] is not None
    assert seen[0].recorded_at == "2026-10-26T00:00:00Z"


def test_concurrent_draft_updates_all_land(tmp_path: Path) -> None:
    names = [f"page-{word}" for word in ("a", "b", "c", "d", "e", "f", "g", "h")]
    errors: list[BaseException] = []

    def run(name: str) -> None:
        try:
            update_draft(tmp_path, "expense-report", _with_page(name))
        except BaseException as exc:  # noqa: BLE001 - the thread reports whatever it got
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    draft = load_draft(tmp_path, "expense-report")
    assert draft is not None
    assert sorted(page.page_id for page in draft.pages) == sorted(names)


def test_a_change_that_returns_nothing_writes_nothing(tmp_path: Path) -> None:
    assert update_draft(tmp_path, "expense-report", lambda current: None) is None
    assert load_draft(tmp_path, "expense-report") is None


def test_a_change_that_makes_an_invalid_draft_keeps_the_previous_one(tmp_path: Path) -> None:
    save_draft(tmp_path, _model())
    with pytest.raises(SiteModelError, match="Expected"):
        update_draft(tmp_path, "expense-report", lambda current: _model(hosts=()))
    assert load_draft(tmp_path, "expense-report") == _model()
    leftovers = [p.name for p in (tmp_path / "browser-models" / "expense-report").iterdir()]
    assert not any(name.endswith(".tmp") for name in leftovers)
