"""`blumkin browser explore`: observe, click, show, save and discard (#486)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from blumkin.browser.explore import Inspection, ModelDraft, parse_aria_snapshot
from blumkin.browser.profiles import create_profile
from blumkin.cli import main
from blumkin.exit_codes import EXIT_OTHER, EXIT_SUCCESS, EXIT_USAGE

_SNAPSHOT = """\
- heading "Expenses" [level=1]
- link "Help":
  - /url: /help
- button "Add line"
- button "Submit report"
- textbox "Amount"
- heading "Report 20261024123"
"""


class _Driver:
    def __init__(self) -> None:
        self.url = "https://erp.example.com/expenses"
        self.clicks: list[tuple[str, str]] = []
        self.closed = 0
        self.marks: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        self.lands_on: str | None = None
        self.refusal: str | None = None
        self.href = ""
        self.snapshot = _SNAPSHOT

    def aria_snapshot(self) -> str:
        return self.snapshot

    def mark_window(self, task: str, hosts: Any = (), blocked: Any = ()) -> None:
        self.marks.append((task, tuple(hosts), tuple(blocked)))

    def inspect(self, role: str, name: str) -> Inspection:
        return Inspection(refusal=self.refusal, href=self.href)

    def click_vetted(self, role: str, name: str, expected: Inspection) -> None:
        self.clicks.append((role, name))
        if self.lands_on is not None:
            self.url = self.lands_on

    def close(self) -> None:
        self.closed += 1


class _Confirmer:
    answers: list[bool] = []
    asked: list[str] = []
    unavailable = False
    timed_out = False

    def confirm(self, prompt: str) -> bool:
        type(self).asked.append(prompt)
        if not type(self).answers:
            type(self).unavailable = True
            return False
        return type(self).answers.pop(0)


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> _Driver:
    fake = _Driver()
    monkeypatch.setattr(
        "blumkin.browser.explore_commands.attach_exploring", lambda profile, **kwargs: fake
    )
    _Confirmer.answers = []
    _Confirmer.asked = []
    _Confirmer.unavailable = False
    _Confirmer.timed_out = False
    monkeypatch.setattr("blumkin.browser.explore_commands.TerminalConfirmer", _Confirmer)
    return fake


@pytest.fixture(autouse=True)
def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("BLUMKIN_CONFIG_DIR", str(config))
    _policy(config, 'allowed_hosts = ["erp.example.com"]')
    create_profile(config, "expense-report")
    return config


def _policy(config: Path, body: str) -> None:
    (config / "config.toml").write_text(
        f'[browser.tasks.expense-report]\n{body}\n\n[profiles.default]\nclient_id = "abc-123"\n'
    )


def _run(*args: str) -> Any:
    return CliRunner().invoke(main, ["--json", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def _draft_path(config: Path) -> Path:
    return config / "browser-models" / "expense-report" / "draft.json"


def test_observe_records_a_page_in_the_draft_and_reports_what_it_kept(
    driver: _Driver, _config_dir: Path
) -> None:
    result = _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["page"] == "expense-list"
    assert payload["url_template"] == "/expenses"
    assert payload["skipped"] == 1
    assert payload["controls"] >= 3
    assert driver.closed == 1
    assert _draft_path(_config_dir).is_file()
    assert driver.marks and driver.marks[0][0] == "expense-report"
    assert "login.microsoftonline.com" in driver.marks[0][2]


def test_observe_refuses_a_task_without_allowed_hosts(driver: _Driver, _config_dir: Path) -> None:
    _policy(_config_dir, 'allowed_controls = ["Add line"]')
    result = _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert result.exit_code == EXIT_OTHER
    payload = _json(result)
    assert payload["error"] == "explore_refused"
    assert "allowed_hosts" in payload["message"]
    assert not _draft_path(_config_dir).exists()


def test_observe_refuses_a_page_off_the_allowed_hosts(driver: _Driver, _config_dir: Path) -> None:
    driver.url = "https://evil.example.net/x"
    result = _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "explore_refused"
    assert not _draft_path(_config_dir).exists()


def test_observing_twice_updates_the_page_and_a_second_page_is_added(driver: _Driver) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    driver.url = "https://erp.example.com/expenses/42/line"
    _run("browser", "explore", "observe", "expense-report", "expense-form")
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert [p["page_id"] for p in shown["pages"]] == ["expense-list", "expense-form"]
    assert shown["pages"][1]["url_template"] == "/expenses/{id}/line"


def test_a_bad_page_id_is_a_usage_error(driver: _Driver) -> None:
    result = _run("browser", "explore", "observe", "expense-report", "Page One")
    assert result.exit_code == EXIT_USAGE
    assert _json(result)["error"] == "usage_error"


def test_a_read_only_click_follows_a_link_without_asking(driver: _Driver) -> None:
    result = _run("browser", "explore", "click", "expense-report", "Help", "--role", "link")
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = _json(result)
    assert payload["clicked"] is True
    assert payload["decision"] == "click"
    assert driver.clicks == [("link", "Help")]
    assert _Confirmer.asked == []


def test_a_state_changing_click_is_clicked_only_after_a_yes(driver: _Driver) -> None:
    _Confirmer.answers = [True]
    result = _run("browser", "explore", "click", "expense-report", "Add line")
    payload = _json(result)
    assert payload["clicked"] is True
    assert payload["decision"] == "ask"
    assert driver.clicks == [("button", "Add line")]
    assert "Add line" in _Confirmer.asked[0]


def test_a_no_answer_means_no_click(driver: _Driver) -> None:
    _Confirmer.answers = [False]
    result = _run("browser", "explore", "click", "expense-report", "Add line")
    payload = _json(result)
    assert result.exit_code == EXIT_SUCCESS
    assert payload["clicked"] is False
    assert driver.clicks == []


def test_without_a_terminal_the_control_is_not_clicked_and_the_user_gets_the_command(
    driver: _Driver,
) -> None:
    result = _run("browser", "explore", "click", "expense-report", "Add line")
    payload = _json(result)
    assert payload["clicked"] is False
    assert payload["terminal_unavailable"] is True
    assert "blumkin browser explore click expense-report" in payload["hint"]
    assert driver.clicks == []


def test_a_declared_control_is_clicked_with_no_terminal_and_recorded_as_a_step(
    driver: _Driver, _config_dir: Path
) -> None:
    _policy(_config_dir, 'allowed_hosts = ["erp.example.com"]\nallowed_controls = ["Add line"]')
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    result = _run(
        "browser", "explore", "click", "expense-report", "Add line", "--page", "expense-list"
    )
    payload = _json(result)
    assert payload["clicked"] is True
    assert payload["approved_by"] == "allowed_controls"
    assert payload["recorded_step"] is True
    assert driver.clicks == [("button", "Add line")]
    assert _Confirmer.asked == []


def test_a_declared_finalizing_name_is_refused_and_not_clicked(
    driver: _Driver, _config_dir: Path
) -> None:
    _policy(_config_dir, 'allowed_hosts = ["erp.example.com"]\nallowed_controls = ["Submit"]')
    payload = _json(_run("browser", "explore", "click", "expense-report", "Submit"))
    assert payload["clicked"] is False
    assert payload["approved_by"] is None
    assert driver.clicks == []


def test_a_loose_declared_pattern_is_refused_before_the_browser_is_touched(
    driver: _Driver, _config_dir: Path
) -> None:
    _policy(_config_dir, 'allowed_hosts = ["erp.example.com"]\nallowed_controls = ["*"]')
    result = _run("browser", "explore", "click", "expense-report", "Archive")
    assert result.exit_code != EXIT_SUCCESS
    assert driver.clicks == []


def test_the_no_terminal_hint_names_allowed_controls_as_the_alternative(
    driver: _Driver,
) -> None:
    payload = _json(_run("browser", "explore", "click", "expense-report", "Add line"))
    assert payload["approved_by"] is None
    assert "allowed_controls" in payload["hint"]


def test_a_finalizing_control_is_never_clicked_or_asked_about(driver: _Driver) -> None:
    _Confirmer.answers = [True]
    result = _run("browser", "explore", "click", "expense-report", "Submit report")
    payload = _json(result)
    assert payload["clicked"] is False
    assert payload["decision"] == "record_only"
    assert driver.clicks == []
    assert _Confirmer.asked == []


def test_a_click_that_lands_off_the_allowed_hosts_is_reported_as_refused(driver: _Driver) -> None:
    driver.lands_on = "https://evil.example.net/x"
    result = _run("browser", "explore", "click", "expense-report", "Help", "--role", "link")
    assert result.exit_code == EXIT_OTHER
    payload = _json(result)
    assert payload["error"] == "explore_refused"
    assert "evil.example.net" in payload["message"]


def test_a_click_records_a_step_when_a_page_is_named(driver: _Driver) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    result = _run(
        "browser",
        "explore",
        "click",
        "expense-report",
        "Help",
        "--role",
        "link",
        "--page",
        "expense-list",
    )
    assert _json(result)["recorded_step"] is True
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert shown["steps"] == 1


def test_save_needs_yes_then_stores_a_version_and_clears_the_draft(
    driver: _Driver, _config_dir: Path
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    refused = _run("browser", "explore", "save", "expense-report")
    assert refused.exit_code == EXIT_USAGE
    assert "--yes" in _json(refused)["message"]
    saved = _run("browser", "explore", "save", "expense-report", "--yes")
    assert saved.exit_code == EXIT_SUCCESS, saved.output
    assert _json(saved)["version"] == 1
    assert not _draft_path(_config_dir).exists()
    assert (_config_dir / "browser-models" / "expense-report" / "model.json").is_file()


def test_save_without_a_draft_is_not_found(driver: _Driver) -> None:
    result = _run("browser", "explore", "save", "expense-report", "--yes")
    assert result.exit_code != EXIT_SUCCESS
    assert _json(result)["error"] == "not_found"


def test_discard_removes_the_draft(driver: _Driver, _config_dir: Path) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert _json(_run("browser", "explore", "discard", "expense-report"))["discarded"] is True
    assert _json(_run("browser", "explore", "discard", "expense-report"))["discarded"] is False
    assert not _draft_path(_config_dir).exists()


def test_show_without_a_draft_says_so(driver: _Driver) -> None:
    payload = _json(_run("browser", "explore", "show", "expense-report"))
    assert payload["pages"] == []


def test_an_unknown_task_is_not_found_and_a_bad_name_is_a_usage_error(driver: _Driver) -> None:
    assert _json(_run("browser", "explore", "show", "time-sheet"))["pages"] == []
    result = _run("browser", "explore", "observe", "time-sheet", "expense-list")
    assert result.exit_code != EXIT_SUCCESS
    assert _json(result)["error"] in {"not_found", "explore_refused"}
    bad = _run("browser", "explore", "observe", "Bad_Name", "expense-list")
    assert bad.exit_code == EXIT_USAGE


def test_the_human_output_names_the_controls_and_never_a_value(driver: _Driver) -> None:
    result = CliRunner().invoke(
        main, ["browser", "explore", "observe", "expense-report", "expense-list"]
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "expense-list" in result.output
    assert os.linesep in result.output


def test_a_control_the_page_refuses_is_reported_with_its_reason_not_as_the_users_no(
    driver: _Driver,
) -> None:
    driver.refusal = "it carries a formaction override"
    _Confirmer.answers = [True]
    payload = _json(_run("browser", "explore", "click", "expense-report", "Add line"))
    assert payload["clicked"] is False
    assert payload["outcome"] == "refused: it carries a formaction override"
    assert "formaction" in payload["hint"]
    assert "answer was no" not in payload["hint"]
    assert _Confirmer.asked == []


def test_a_link_to_another_host_is_reported_with_the_host(driver: _Driver) -> None:
    driver.href = "https://other.example.org/logout"
    payload = _json(_run("browser", "explore", "click", "expense-report", "Help", "--role", "link"))
    assert payload["clicked"] is False
    assert "other.example.org" in payload["hint"]


def test_a_timed_out_question_is_reported_as_no_answer(
    driver: _Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _TimedOut:
        timed_out = False
        unavailable = False

        def confirm(self, prompt: str) -> bool:
            self.timed_out = True
            return False

    monkeypatch.setattr("blumkin.browser.explore_commands.TerminalConfirmer", _TimedOut)
    payload = _json(_run("browser", "explore", "click", "expense-report", "Add line"))
    assert payload["clicked"] is False
    assert "no answer" in payload["hint"]


def test_observe_continues_the_stored_model_instead_of_dropping_it(
    driver: _Driver, _config_dir: Path
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    _run("browser", "explore", "save", "expense-report", "--yes")
    driver.url = "https://erp.example.com/expenses/42/line"
    _run("browser", "explore", "observe", "expense-report", "expense-form")
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert [p["page_id"] for p in shown["pages"]] == ["expense-list", "expense-form"]
    saved = _json(_run("browser", "explore", "save", "expense-report", "--yes"))
    assert saved["version"] == 2
    assert saved["pages"] == 2


def test_a_page_that_moves_while_it_is_observed_is_refused_and_leaves_no_draft(
    driver: _Driver, _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Moving(_Driver):
        def aria_snapshot(self) -> str:
            self.url = "https://erp.example.com/elsewhere"
            return _SNAPSHOT

    moving = _Moving()
    monkeypatch.setattr(
        "blumkin.browser.explore_commands.attach_exploring", lambda profile, **kwargs: moving
    )
    result = _run("browser", "explore", "observe", "expense-report", "expense-list")
    assert result.exit_code == EXIT_OTHER
    assert _json(result)["error"] == "explore_refused"
    assert not _draft_path(_config_dir).exists()


# --------------------------------------------------------------------------- explore fixes (#608)


def test_forget_drops_one_page_from_the_draft(driver: _Driver, _config_dir: Path) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    _run("browser", "explore", "observe", "expense-report", "old-page")

    result = _run("browser", "explore", "forget", "expense-report", "old-page")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["forgotten"] is True
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert [page["page_id"] for page in shown["pages"]] == ["expense-list"]


def test_forget_of_a_page_that_is_not_there_says_so(driver: _Driver, _config_dir: Path) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")

    result = _run("browser", "explore", "forget", "expense-report", "nope")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["forgotten"] is False


def test_forget_with_no_draft_or_model_is_a_clean_no_op(driver: _Driver) -> None:
    result = _run("browser", "explore", "forget", "expense-report", "any-page")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["forgotten"] is False


def test_forget_refuses_a_page_id_that_is_not_words(driver: _Driver) -> None:
    result = _run("browser", "explore", "forget", "expense-report", "Not A Page")

    assert result.exit_code == EXIT_USAGE


def test_observing_a_second_page_at_the_same_address_warns(
    driver: _Driver, _config_dir: Path
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    driver.snapshot = '- heading "Other"\n- button "Something else"\n- textbox "Purpose"\n'

    payload = _json(_run("browser", "explore", "observe", "expense-report", "report-edit"))

    assert payload["shared_addresses"] == {"/expenses": ["expense-list", "report-edit"]}
    assert "landmarks" in payload["warnings"][0]


def test_forget_will_not_drop_the_last_page(driver: _Driver, _config_dir: Path) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")

    result = _run("browser", "explore", "forget", "expense-report", "expense-list")

    assert result.exit_code != EXIT_SUCCESS
    assert "discard" in _json(result)["message"]
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert [page["page_id"] for page in shown["pages"]] == ["expense-list"]


def test_forget_says_which_flow_lost_steps(driver: _Driver, _config_dir: Path) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    _run("browser", "explore", "observe", "expense-report", "old-page")
    _run("browser", "explore", "click", "expense-report", "Help", "--role", "link",
         "--page", "old-page")  # fmt: skip

    payload = _json(_run("browser", "explore", "forget", "expense-report", "old-page"))

    assert payload["forgotten"] is True
    assert payload["steps_dropped"] == {"recorded": 1}
    assert "lost 1 step" in payload["warnings"][0]


def test_forget_can_drop_a_stored_page_whose_id_exploration_would_not_make(
    driver: _Driver, _config_dir: Path
) -> None:
    import dataclasses

    from blumkin.browser.model import default_store, save_model

    draft = ModelDraft(task="expense-report", hosts=("erp.example.com",))
    draft.add_page("expense-list", "https://erp.example.com/a", parse_aria_snapshot(_SNAPSHOT))
    draft.add_page("expense-item", "https://erp.example.com/b", parse_aria_snapshot(_SNAPSHOT))
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    pages = tuple(
        dataclasses.replace(page, page_id="expense-item-2")
        if page.page_id == "expense-item"
        else page
        for page in model.pages
    )
    save_model(default_store(_config_dir), dataclasses.replace(model, pages=pages))

    result = _run("browser", "explore", "forget", "expense-report", "expense-item-2")

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["forgotten"] is True


def test_observe_records_a_popup_as_a_dialog_with_the_landmarks_that_mark_its_container(
    driver: _Driver, _config_dir: Path
) -> None:
    result = _run(
        "browser", "explore", "observe", "expense-report", "attachments", "--kind", "dialog",
        "--landmark", "Add line", "--landmark", "Help",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["kind"] == "dialog"
    stored = json.loads(_draft_path(_config_dir).read_text())
    (page,) = stored["pages"]
    assert page["kind"] == "dialog" and page["landmarks"] == ["Add line", "Help"]
    shown = _json(_run("browser", "explore", "show", "expense-report"))
    assert shown["pages"][0]["kind"] == "dialog"
    assert shown["pages"][0]["dialog_landmarks"] == ["Add line", "Help"]
    text = CliRunner().invoke(main, ["browser", "explore", "show", "expense-report"]).output
    assert "attachments [dialog]" in text
    assert 'config: dialog_landmarks = ["Add line", "Help"]' in text


def test_observe_records_a_list_of_values_dialog_as_its_own_page_kind(
    driver: _Driver, _config_dir: Path
) -> None:
    result = _run(
        "browser", "explore", "observe", "expense-report", "location-search",
        "--kind", "list-of-values",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    (page,) = json.loads(_draft_path(_config_dir).read_text())["pages"]
    assert page["kind"] == "list-of-values" and "landmarks" not in page


def test_an_ordinary_page_keeps_its_stored_shape_with_no_kind(
    driver: _Driver, _config_dir: Path
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")

    (page,) = json.loads(_draft_path(_config_dir).read_text())["pages"]

    assert "kind" not in page and "landmarks" not in page


@pytest.mark.parametrize(
    "extra",
    [
        ["--kind", "dialog"],  # no landmarks
        ["--kind", "dialog", "--landmark", "Add line"],  # only one
        ["--kind", "dialog", "--landmark", "Add line", "--landmark", "Submit report"],
        ["--kind", "dialog", "--landmark", "Add line", "--landmark", "Not on the page"],
        ["--landmark", "Add line", "--landmark", "Help"],  # landmarks on an ordinary page
        ["--kind", "list-of-values", "--landmark", "Add line", "--landmark", "Help"],
    ],
)
def test_a_dialog_without_good_landmarks_is_refused_and_leaves_no_draft(
    driver: _Driver, _config_dir: Path, extra: list[str]
) -> None:
    result = _run("browser", "explore", "observe", "expense-report", "attachments", *extra)

    assert result.exit_code != EXIT_SUCCESS
    assert not _draft_path(_config_dir).exists()


def test_observing_a_dialog_page_again_without_a_kind_keeps_its_kind_and_landmarks(
    driver: _Driver, _config_dir: Path
) -> None:
    first = ["browser", "explore", "observe", "expense-report", "attachments"]
    _run(*first, "--kind", "dialog", "--landmark", "Add line", "--landmark", "Help")
    driver.snapshot = _SNAPSHOT + '- button "Extra"\n'

    again = _run(*first)

    assert again.exit_code == EXIT_SUCCESS, again.output
    assert _json(again)["kind"] == "dialog"
    (page,) = json.loads(_draft_path(_config_dir).read_text())["pages"]
    assert page["kind"] == "dialog" and page["landmarks"] == ["Add line", "Help"]
    assert any("Extra" in str(control) for control in page["controls"])


def test_a_kind_that_is_given_again_replaces_the_old_one_and_page_demotes_on_purpose(
    driver: _Driver, _config_dir: Path
) -> None:
    first = ["browser", "explore", "observe", "expense-report", "attachments"]
    _run(*first, "--kind", "dialog", "--landmark", "Add line", "--landmark", "Help")

    demoted = _run(*first, "--kind", "page")

    assert demoted.exit_code == EXIT_SUCCESS and _json(demoted)["kind"] == "page"
    (page,) = json.loads(_draft_path(_config_dir).read_text())["pages"]
    assert "kind" not in page and "landmarks" not in page


def test_a_landmark_must_be_a_storable_control_not_a_heading_and_ignores_case(
    driver: _Driver, _config_dir: Path
) -> None:
    base = ["browser", "explore", "observe", "expense-report", "attachments", "--kind", "dialog"]

    heading = _run(*base, "--landmark", "Add line", "--landmark", "Expenses")  # a heading
    typed = _run(*base, "--landmark", "Add line", "--landmark", "Report 20261024123")  # skipped
    cased = _run(*base, "--landmark", "add LINE", "--landmark", "help")

    assert heading.exit_code != EXIT_SUCCESS and typed.exit_code != EXIT_SUCCESS
    assert cased.exit_code == EXIT_SUCCESS, cased.output


def test_observing_a_dialog_again_with_kind_dialog_and_no_landmarks_keeps_its_landmarks(
    driver: _Driver, _config_dir: Path
) -> None:
    first = ["browser", "explore", "observe", "expense-report", "attachments", "--kind", "dialog"]
    _run(*first, "--landmark", "Add line", "--landmark", "Help")

    again = _run(*first)

    assert again.exit_code == EXIT_SUCCESS, again.output
    (page,) = json.loads(_draft_path(_config_dir).read_text())["pages"]
    assert page["landmarks"] == ["Add line", "Help"]
    assert (
        _run(
            *first[:-2], "--kind", "dialog", "--landmark", "Help", "--landmark", "Amount"
        ).exit_code
        == 0
    )
    (page,) = json.loads(_draft_path(_config_dir).read_text())["pages"]
    assert page["landmarks"] == ["Help", "Amount"]  # given again: replaced


def test_a_landmark_past_the_stored_control_cap_is_refused(
    driver: _Driver, _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("blumkin.browser.explore.MAX_DRAFT_CONTROLS", 3)
    driver.snapshot = (
        '- button "One"\n- button "Two"\n- button "Three"\n- button "Four"\n- button "Five"\n'
    )
    base = ["browser", "explore", "observe", "expense-report", "attachments", "--kind", "dialog"]

    late = _run(*base, "--landmark", "One", "--landmark", "Five")
    early = _run(*base, "--landmark", "One", "--landmark", "Two")

    assert late.exit_code != EXIT_SUCCESS and early.exit_code == EXIT_SUCCESS, early.output


def test_the_policy_line_is_valid_toml_for_a_name_with_a_backslash_or_quote_free_text(
    driver: _Driver, _config_dir: Path
) -> None:
    import tomllib

    driver.snapshot = '- button "Add\\\\More"\n- button "Delete"\n'
    base = ["browser", "explore", "observe", "expense-report", "attachments", "--kind", "dialog"]
    assert _run(*base, "--landmark", "Add\\More", "--landmark", "Delete").exit_code == EXIT_SUCCESS

    text = CliRunner().invoke(main, ["browser", "explore", "show", "expense-report"]).output
    line = (
        next(row for row in text.splitlines() if "dialog_landmarks" in row)
        .split("config:")[1]
        .strip()
    )

    assert tomllib.loads(line)["dialog_landmarks"] == ["Add\\More", "Delete"]


def _fake_click(monkeypatch: pytest.MonkeyPatch, role: str = "button") -> list[str]:
    clicked: list[str] = []

    def fake(config: Any, task: str, name: str, role_arg: Any, *rest: Any) -> dict[str, Any]:
        clicked.append(name)
        return {"action": "click", "name": name, "ok": True, "role": role, "task": task}

    monkeypatch.setattr("blumkin.cli.click_task", fake)
    return clicked


def _draft_steps(config: Path) -> list[dict[str, Any]]:
    stored = json.loads(_draft_path(config).read_text())
    return [step for flow in stored["flows"] for step in flow["steps"]]


def test_browser_click_with_record_adds_the_click_as_a_step_of_the_draft(
    driver: _Driver, _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    clicked = _fake_click(monkeypatch)

    result = _run(
        "browser", "click", "expense-report", "Add line", "--record", "expense-list",
        "--expect-heading", "Edit Expense Report",
    )  # fmt: skip

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert _json(result)["recorded_step"] is True and clicked == ["Add line"]
    (step,) = _draft_steps(_config_dir)
    assert step["action"] == "click" and step["page"] == "expense-list"
    assert step["control"].endswith("add-line") or "add" in step["control"]
    assert step["expect_heading"] == "Edit Expense Report"


def test_a_click_that_cannot_be_recorded_is_refused_before_it_is_made(
    driver: _Driver, _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clicked = _fake_click(monkeypatch)

    no_draft = _run("browser", "click", "expense-report", "Add line", "--record", "expense-list")
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    no_page = _run("browser", "click", "expense-report", "Add line", "--record", "nowhere")
    no_control = _run("browser", "click", "expense-report", "Not there", "--record", "expense-list")

    assert no_draft.exit_code != EXIT_SUCCESS
    assert no_page.exit_code != EXIT_SUCCESS and no_control.exit_code != EXIT_SUCCESS
    assert clicked == []  # no click was made for any of them
    assert _draft_steps(_config_dir) == []


def test_a_click_without_record_changes_nothing_in_the_draft(
    driver: _Driver, _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    _fake_click(monkeypatch)

    result = _run("browser", "click", "expense-report", "Add line")

    assert "recorded_step" not in _json(result) and _draft_steps(_config_dir) == []


def test_a_recording_that_fails_after_the_click_is_reported_next_to_it(
    driver: _Driver, _config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run("browser", "explore", "observe", "expense-report", "expense-list")
    _fake_click(monkeypatch)

    def boom(*_a: Any, **_k: Any) -> bool:
        raise OSError("disk full at https://x.example/secret?token=abc")

    monkeypatch.setattr("blumkin.cli.record_browser_click", boom)

    result = _run("browser", "click", "expense-report", "Add line", "--record", "expense-list")
    text = (
        CliRunner()
        .invoke(
            main, ["browser", "click", "expense-report", "Add line", "--record", "expense-list"]
        )
        .output
    )

    payload = _json(result)
    assert result.exit_code == EXIT_SUCCESS and payload["recorded_step"] is False
    assert "disk full" in payload["record_error"] and "secret" not in result.output
    assert "the click was made but not recorded" in text
