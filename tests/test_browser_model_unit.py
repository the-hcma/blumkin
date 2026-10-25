"""Site model store: a task's recorded interaction map, kept on disk (issue #485)."""

from __future__ import annotations

import copy
import json
import os
import stat
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
    list_versions,
    load_model,
    load_version,
    save_model,
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
