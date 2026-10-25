"""Exploration core: observe a page, draft a site model and decide what may be clicked (#486)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.cdp import ControlInfo
from blumkin.browser.explore import (
    ClickDecision,
    ControlObservation,
    Explorer,
    ExploreRefusedError,
    ExploringDriver,
    ModelDraft,
    TerminalConfirmer,
    attach_exploring,
    decide_click,
    parse_aria_snapshot,
    url_template,
)
from blumkin.browser.model import ControlKind, ControlSafety, load_model, save_model

_SNAPSHOT = """\
- banner:
  - heading "Expenses" [level=1]
  - link "Help":
    - /url: /help
- main:
  - button "Add line"
  - textbox "Amount"
  - combobox "Category"
  - button "Create report"
  - tab "Receipts" [selected]
  - menuitem "Reports"
  - text: Total 12.50
  - img "logo"
"""


def test_a_snapshot_is_parsed_into_named_controls_in_order() -> None:
    controls = parse_aria_snapshot(_SNAPSHOT)
    assert [(c.role, c.name) for c in controls] == [
        ("heading", "Expenses"),
        ("link", "Help"),
        ("button", "Add line"),
        ("textbox", "Amount"),
        ("combobox", "Category"),
        ("button", "Create report"),
        ("tab", "Receipts"),
        ("menuitem", "Reports"),
        ("img", "logo"),
    ]


def test_parsing_ignores_properties_unnamed_nodes_text_and_repeats() -> None:
    text = '- button "A"\n- button "A"\n- button\n- /url: /x\n- text: hello\n- link "B\\"q":\n'
    assert [(c.role, c.name) for c in parse_aria_snapshot(text)] == [
        ("button", "A"),
        ("link", 'B"q'),
    ]


@pytest.mark.parametrize(
    ("url", "template"),
    [
        ("https://erp.example.com/expenses", "/expenses"),
        ("https://erp.example.com/expenses/12345/line", "/expenses/{id}/line"),
        ("https://erp.example.com/expenses/ab12cd34ef56/edit", "/expenses/{id}/edit"),
        ("https://erp.example.com/expenses?token=abc#frag", "/expenses"),
        ("https://erp.example.com/", "/"),
        ("https://erp.example.com/a/B3k9Zq2LmX7vN1pQ4rT8/x", "/a/{id}/x"),
        ("https://erp.example.com/expenseReport/summary", "/expenseReport/summary"),
        ("https://erp.example.com/a%20b/c", "/a{id}b/c".replace("a{id}b", "{id}")),
    ],
)
def test_a_url_becomes_a_path_template_without_values(url: str, template: str) -> None:
    assert url_template(url) == template


def _draft() -> ModelDraft:
    return ModelDraft(task="expense-report", hosts=("erp.example.com",))


def test_a_draft_turns_observations_into_a_valid_stored_model(tmp_path: Path) -> None:
    draft = _draft()
    observed = (*parse_aria_snapshot(_SNAPSHOT), ControlObservation("button", "Submit report"))
    draft.add_page("expense-list", "https://erp.example.com/expenses", observed)
    draft.add_step("click", page_id="expense-list", control="expense-list.add-line")
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    saved = save_model(tmp_path, model)
    loaded = load_model(tmp_path, "expense-report")
    assert loaded == saved
    page = loaded.pages[0]  # type: ignore[union-attr]
    by_name = {c.name: c for c in page.controls}
    assert by_name["expense-list.add-line"].kind is ControlKind.BUTTON
    assert by_name["expense-list.add-line"].safety is ControlSafety.SAFE
    assert by_name["expense-list.create-report"].safety is ControlSafety.SAFE
    assert by_name["expense-list.submit-report"].safety is ControlSafety.FINALIZING
    assert by_name["expense-list.amount"].kind is ControlKind.TEXT
    assert by_name["expense-list.category"].kind is ControlKind.SELECT
    assert page.url_template == "/expenses"
    assert "button:Add line" in page.fingerprint


def test_a_draft_drops_labels_that_look_like_typed_data_and_counts_them() -> None:
    draft = _draft()
    controls = (
        ControlObservation("button", "Add line"),
        ControlObservation("heading", "Report 20261024123"),
        ControlObservation("link", "mail john@example.org"),
    )
    draft.add_page("expense-list", "https://erp.example.com/expenses", controls)
    assert draft.skipped == 2
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    assert [c.name for c in model.pages[0].controls] == ["expense-list.add-line"]


def test_two_controls_with_the_same_label_get_distinct_logical_names() -> None:
    draft = _draft()
    controls = (ControlObservation("button", "Add"), ControlObservation("link", "Add"))
    draft.add_page("expense-list", "https://erp.example.com/expenses", controls)
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    assert len({c.name for c in model.pages[0].controls}) == 2


def test_observing_a_page_again_updates_it_instead_of_duplicating_it() -> None:
    draft = _draft()
    draft.add_page(
        "expense-list", "https://erp.example.com/expenses", (ControlObservation("button", "Add"),)
    )
    draft.add_page(
        "expense-list",
        "https://erp.example.com/expenses",
        (ControlObservation("button", "Add"), ControlObservation("button", "Edit")),
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    assert len(model.pages) == 1
    assert len(model.pages[0].controls) == 2


@pytest.mark.parametrize(
    ("role", "name", "expected"),
    [
        ("link", "Help", ClickDecision.CLICK),
        ("tab", "Receipts", ClickDecision.CLICK),
        ("menuitem", "Reports", ClickDecision.CLICK),
        ("button", "Add line", ClickDecision.ASK),
        ("checkbox", "Remember me", ClickDecision.ASK),
        ("button", "Create report", ClickDecision.ASK),
        ("button", "Submit", ClickDecision.RECORD_ONLY),
        ("link", "Send to approver", ClickDecision.RECORD_ONLY),
        ("button", "Pay now", ClickDecision.RECORD_ONLY),
        ("heading", "Expenses", ClickDecision.RECORD_ONLY),
    ],
)
def test_click_decisions_follow_the_exploration_rules(
    role: str, name: str, expected: ClickDecision
) -> None:
    assert decide_click(role, name) is expected


def test_extra_deny_names_make_a_control_record_only() -> None:
    assert decide_click("button", "Release", extra_deny=("release",)) is ClickDecision.RECORD_ONLY


class _Driver:
    """A stand-in for the raw driver: records what is clicked and what the page looks like."""

    def __init__(self) -> None:
        self.url = "https://erp.example.com/expenses"
        self.snapshot = _SNAPSHOT
        self.clicks: list[tuple[str, str]] = []
        self.lands_on: str | None = None
        self.submit_control = False

    def aria_snapshot(self) -> str:
        return self.snapshot

    def click_vetted(self, role: str, name: str) -> None:
        if self.submit_control:
            raise ExploreRefusedError("it is a submit control")
        self.clicks.append((role, name))
        if self.lands_on is not None:
            self.url = self.lands_on


class _Confirmer:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def confirm(self, prompt: str) -> bool:
        self.asked.append(prompt)
        return self.answers.pop(0)


def _explorer(
    driver: _Driver, answers: list[bool] | None = None, **kwargs: Any
) -> tuple[Explorer, _Confirmer]:
    confirmer = _Confirmer(answers or [])
    return Explorer(driver, confirmer, allowed_hosts=("erp.example.com",), **kwargs), confirmer


def test_a_read_only_click_follows_a_link_without_asking() -> None:
    driver = _Driver()
    explorer, confirmer = _explorer(driver)
    assert explorer.click("link", "Help") is True
    assert driver.clicks == [("link", "Help")]
    assert confirmer.asked == []


def test_a_state_changing_click_asks_first_and_clicks_only_on_yes() -> None:
    driver = _Driver()
    explorer, confirmer = _explorer(driver, [True, False])
    assert explorer.click("button", "Add line") is True
    assert explorer.click("button", "Create report") is False
    assert driver.clicks == [("button", "Add line")]
    assert len(confirmer.asked) == 2
    assert "Add line" in confirmer.asked[0]


def test_a_finalizing_control_is_never_clicked_even_when_the_user_would_say_yes() -> None:
    driver = _Driver()
    explorer, confirmer = _explorer(driver, [True])
    assert explorer.click("button", "Submit") is False
    assert driver.clicks == []
    assert confirmer.asked == []


def test_a_control_the_page_inspection_refuses_is_recorded_not_clicked() -> None:
    driver = _Driver()
    driver.submit_control = True
    explorer, _ = _explorer(driver)
    assert explorer.click("link", "Help") is False
    assert driver.clicks == []


def test_an_approval_applies_to_one_click_only() -> None:
    driver = _Driver()
    explorer, confirmer = _explorer(driver, [True, False])
    assert explorer.click("button", "Add line") is True
    assert explorer.click("button", "Add line") is False
    assert len(confirmer.asked) == 2


def test_landing_off_the_allowed_hosts_stops_the_exploration() -> None:
    driver = _Driver()
    driver.lands_on = "https://evil.example.net/x"
    explorer, _ = _explorer(driver)
    with pytest.raises(ExploreRefusedError, match="evil.example.net"):
        explorer.click("link", "Help")


def test_observing_returns_the_controls_and_refuses_an_off_host_page() -> None:
    driver = _Driver()
    explorer, _ = _explorer(driver)
    assert [c.name for c in explorer.observe()][:2] == ["Expenses", "Help"]
    driver.url = "https://evil.example.net/"
    with pytest.raises(ExploreRefusedError, match="evil.example.net"):
        explorer.observe()


class _Tty:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.written = ""

    def write(self, text: str) -> int:
        self.written += text
        return len(text)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        return self.reply

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("y\n", True),
        ("yes\n", True),
        ("Y\n", True),
        ("n\n", False),
        ("\n", False),
        ("maybe\n", False),
        ("", False),
    ],
)
def test_the_terminal_confirmer_defaults_to_no_and_reads_the_tty(
    reply: str, expected: bool
) -> None:
    tty = _Tty(reply)
    assert TerminalConfirmer(open_tty=lambda: tty).confirm("Click 'Add line'?") is expected
    assert "Add line" in tty.written


def test_without_a_terminal_the_confirmer_says_no_and_reports_it() -> None:
    def no_tty() -> Any:
        raise OSError("no controlling terminal")

    confirmer = TerminalConfirmer(open_tty=no_tty)
    assert confirmer.confirm("Click 'Add line'?") is False
    assert confirmer.unavailable is True


# --- the exploring driver and the guard's shared inspection -----------------------------------


class _Raw:
    """A stand-in for the raw browser driver."""

    def __init__(self, info: Any = None) -> None:
            self.info = info or ControlInfo(tag="a", input_type="", in_form=False)
        self.url = "https://erp.example.com/expenses"
        self.net = 0
        self.clicked: list[tuple[str, str]] = []
        self.closed = 0

    def block_form_submission(self) -> None:
        self.net += 1

    def aria_snapshot(self) -> str:
        return _SNAPSHOT

    def checked_click(self, name: str, role: str, vet: Any) -> None:
        vet(self.info)
        self.clicked.append((role, name))

    def close(self) -> None:
        self.closed += 1


def test_the_exploring_driver_installs_the_safety_net_and_clicks_through_the_inspection() -> None:
    raw = _Raw()
    driver = ExploringDriver(raw)
    assert raw.net == 1
    assert driver.url == "https://erp.example.com/expenses"
    assert driver.aria_snapshot() == _SNAPSHOT
    driver.click_vetted("link", "Help")
    assert raw.clicked == [("link", "Help")]
    driver.close()
    assert raw.closed == 1


@pytest.mark.parametrize(
    "info_kwargs",
    [
        {"tag": "button", "input_type": "submit", "in_form": True},
        {"tag": "button", "input_type": "button", "in_form": True, "has_formaction": True},
        {"tag": "a", "input_type": "", "in_form": False, "href": "javascript:void(0)"},
        {"tag": "a", "input_type": "", "in_form": False, "inline_handler": "this.form.submit()"},
    ],
)
def test_the_exploring_driver_refuses_what_the_guard_refuses(info_kwargs: dict[str, Any]) -> None:
    raw = _Raw(ControlInfo(**info_kwargs))
    with pytest.raises(ExploreRefusedError):
        ExploringDriver(raw).click_vetted("link", "Help")
    assert raw.clicked == []


def test_attach_exploring_closes_the_raw_driver_when_wrapping_fails() -> None:
    class _Broken(_Raw):
        def block_form_submission(self) -> None:
            raise RuntimeError("no route")

    raw = _Broken()
    with pytest.raises(RuntimeError):
        attach_exploring(object(), attach_raw=lambda profile, **kwargs: raw)  # type: ignore[arg-type]
    assert raw.closed == 1


def test_a_draft_can_be_rebuilt_from_a_stored_model_and_extended(tmp_path: Path) -> None:
    draft = _draft()
    draft.add_page(
        "expense-list",
        "https://erp.example.com/expenses",
        (ControlObservation("button", "Add line"),),
    )
    draft.add_step("click", page_id="expense-list", control="expense-list.add-line")
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    again = ModelDraft.from_model(model)
    again.add_page(
        "expense-form",
        "https://erp.example.com/expenses/1/line",
        (ControlObservation("textbox", "Amount"),),
    )
    rebuilt = again.to_model(recorded_at="2026-10-25T12:30:00Z", blumkin_version="1.0.0")
    assert [p.page_id for p in rebuilt.pages] == ["expense-list", "expense-form"]
    assert rebuilt.flows[0].steps == model.flows[0].steps
    save_model(tmp_path, rebuilt)
