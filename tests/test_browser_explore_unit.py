"""Exploration core: observe a page, draft a site model and decide what may be clicked (#486)."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.cdp import BrowserActionError, ControlInfo
from blumkin.browser.explore import (
    ClickDecision,
    ControlObservation,
    Explorer,
    ExploreRefusedError,
    ExploringDriver,
    Inspection,
    ModelDraft,
    Terminal,
    TerminalConfirmer,
    attach_exploring,
    decide_click,
    parse_aria_snapshot,
    url_template,
)
from blumkin.browser.model import (
    ControlKind,
    ControlSafety,
    Flow,
    FlowStep,
    load_model,
    save_model,
)

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
        ("menuitem", "Reports", ClickDecision.ASK),
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
    """A stand-in for the exploring driver: records what is clicked and what the page shows."""

    def __init__(self) -> None:
        self.url = "https://erp.example.com/expenses"
        self.snapshot = _SNAPSHOT
        self.clicks: list[tuple[str, str]] = []
        self.lands_on: str | None = None
        self.refusal: str | None = None
        self.href = ""
        self.inspected: list[tuple[str, str]] = []
        self.expected: Inspection | None = None

    @property
    def submit_control(self) -> bool:
        return self.refusal is not None

    @submit_control.setter
    def submit_control(self, value: bool) -> None:
        self.refusal = "it is a submit control" if value else None

    def aria_snapshot(self) -> str:
        return self.snapshot

    def click_vetted(self, role: str, name: str, expected: Inspection) -> None:
        self.clicks.append((role, name))
        self.expected = expected
        if self.lands_on is not None:
            self.url = self.lands_on

    def inspect(self, role: str, name: str) -> Inspection:
        self.inspected.append((role, name))
        return Inspection(refusal=self.refusal, href=self.href, tag="a")


class _Confirmer:
    def __init__(self, answers: list[bool]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.before_answer: Any = None

    def confirm(self, prompt: str) -> bool:
        self.asked.append(prompt)
        if self.before_answer is not None:
            self.before_answer()
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
        self.restricted: Any = None
        self.settled = 0
        self.settled_from = ""
        self.marks: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    def block_form_submission(self, *, background_writes: bool = False) -> None:
        self.net += 1

    def aria_snapshot(self) -> str:
        return _SNAPSHOT

    def mark_window(self, task: str, hosts: Any = (), blocked: Any = ()) -> None:
        self.marks.append((task, tuple(hosts), tuple(blocked)))

    def restrict_navigation(self, is_allowed: Any) -> None:
        self.restricted = is_allowed

    def settle(self, previous_url: str) -> None:
        self.settled += 1
        self.settled_from = previous_url

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
    driver.click_vetted("link", "Help", Inspection(refusal=None, href="", tag="a"))
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
        ExploringDriver(raw).click_vetted(
            "link", "Help", Inspection(refusal=None, href="", tag="a")
        )
    assert raw.clicked == []


def test_attach_exploring_closes_the_raw_driver_when_wrapping_fails() -> None:
    class _Broken(_Raw):
        def block_form_submission(self, *, background_writes: bool = False) -> None:
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


# --- second review of the exploration core ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Delete",
        "Remove line",
        "Cancel",
        "Log out",
        "Sign out",
        "Logout",
        "Unsubscribe",
        "Revoke access",
        "Close account",
        "Deactivate",
    ],
)
def test_a_read_only_role_with_a_destructive_name_still_asks_first(name: str) -> None:
    assert decide_click("link", name) is ClickDecision.ASK
    assert decide_click("menuitem", name) is ClickDecision.ASK


@pytest.mark.parametrize("name", ["\u00d7", "S\u0435nd", "\u9001\u4fe1"])
def test_a_read_only_role_with_an_icon_or_non_ascii_name_asks_first(name: str) -> None:
    assert decide_click("link", name) is ClickDecision.ASK


@pytest.mark.parametrize("name", ["Sub mit", "S u b m i t report", "Pa y now"])
def test_a_finalizing_name_split_by_spaces_is_still_finalizing(name: str) -> None:
    assert decide_click("button", name) is ClickDecision.RECORD_ONLY


def test_ordinary_navigation_names_are_still_followed_without_asking() -> None:
    for name in ("Help", "Expenses", "Reports", "Receipts"):
        assert decide_click("link", name) is ClickDecision.CLICK


def test_escaped_names_in_a_snapshot_are_decoded() -> None:
    text = '- button "Re\\nsend"\n- link "caf\\u00e9"\n- button "Say \\"hi\\""\n'
    assert [c.name for c in parse_aria_snapshot(text)] == ["Re\nsend", "caf\u00e9", 'Say "hi"']


def test_a_name_that_matches_the_tasks_extra_deny_words_is_stored_as_finalizing() -> None:
    draft = ModelDraft(task="expense-report", hosts=("erp.example.com",), extra_deny=("release",))
    draft.add_page(
        "expense-list",
        "https://erp.example.com/expenses",
        (ControlObservation("button", "Release hold"),),
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    assert model.pages[0].controls[0].safety is ControlSafety.FINALIZING


def test_content_roles_are_not_recorded_as_landmarks() -> None:
    draft = _draft()
    controls = (
        ControlObservation("button", "Add line"),
        ControlObservation("option", "alice"),
        ControlObservation("row", "Alice Paid"),
        ControlObservation("cell", "Alice"),
        ControlObservation("listitem", "Alice"),
        ControlObservation("paragraph", "Dear Alice"),
    )
    draft.add_page("expense-list", "https://erp.example.com/expenses", controls)
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    assert model.pages[0].fingerprint == ("button:Add line",)


def test_the_page_is_refused_when_it_changes_while_it_is_observed() -> None:
    class _Moving(_Driver):
        def aria_snapshot(self) -> str:
            self.url = "https://erp.example.com/other"
            return _SNAPSHOT

    explorer, _ = _explorer(_Moving())
    with pytest.raises(ExploreRefusedError, match="stay put"):
        explorer.observe_with_url()


def test_observe_with_url_returns_the_url_the_snapshot_belongs_to() -> None:
    explorer, _ = _explorer(_Driver())
    url, controls = explorer.observe_with_url()
    assert url == "https://erp.example.com/expenses"
    assert controls[0].name == "Expenses"


def test_the_host_is_checked_again_after_the_answer_and_before_the_click() -> None:
    driver = _Driver()
    explorer, confirmer = _explorer(driver, [True])

    def navigate_away() -> None:
        driver.url = "https://evil.example.net/"

    confirmer.before_answer = navigate_away
    with pytest.raises(ExploreRefusedError, match="evil.example.net"):
        explorer.click("button", "Add line")
    assert driver.clicks == []


def test_a_control_the_page_refuses_is_vetted_before_the_user_is_asked() -> None:
    driver = _Driver()
    driver.refusal = "it carries a formaction override"
    explorer, confirmer = _explorer(driver, [True])
    assert explorer.click("button", "Add line") is False
    assert confirmer.asked == []
    assert explorer.last_outcome == "refused: it carries a formaction override"


def test_a_link_to_another_host_is_not_followed() -> None:
    driver = _Driver()
    driver.href = "https://other.example.org/logout"
    explorer, _ = _explorer(driver)
    assert explorer.click("link", "Help") is False
    assert driver.clicks == []
    assert "other.example.org" in explorer.last_outcome


def test_a_relative_link_and_a_link_to_an_allowed_host_are_followed() -> None:
    driver = _Driver()
    explorer, _ = _explorer(driver)
    for href in ("/help", "help.html", "https://erp.example.com/x", "#top"):
        driver.href = href
        assert explorer.click("link", "Help") is True


def test_the_prompt_shows_the_link_target_and_a_truncated_name() -> None:
    driver = _Driver()
    driver.href = "https://erp.example.com/account/close?confirm=1"
    explorer, confirmer = _explorer(driver, [False])
    explorer.click("link", "Close account " + "x" * 200)
    prompt = confirmer.asked[0]
    assert "/account/close?confirm=1" in prompt
    assert len(prompt) < 400


def test_the_outcome_says_what_happened() -> None:
    driver = _Driver()
    explorer, _ = _explorer(driver, [False])
    explorer.click("button", "Submit")
    assert explorer.last_outcome == "record_only"
    explorer.click("button", "Add line")
    assert explorer.last_outcome == "declined"
    explorer.click("link", "Help")
    assert explorer.last_outcome == "clicked"


def test_a_terminal_that_never_answers_times_out_as_no() -> None:
    read_end, write_end = os.pipe()

    class _Silent:
        def __init__(self) -> None:
            self.written = ""

        def fileno(self) -> int:
            return read_end

        def write(self, text: str) -> int:
            self.written += text
            return len(text)

        def flush(self) -> None:
            return None

        def readline(self) -> str:
            raise AssertionError("must not block on a silent terminal")

        def close(self) -> None:
            return None

    try:
        confirmer = TerminalConfirmer(open_tty=_Silent, timeout_s=0.05)
        assert confirmer.confirm("Click 'Add line'?") is False
        assert confirmer.timed_out is True
    finally:
        os.close(read_end)
        os.close(write_end)


def test_the_exploring_driver_inspects_without_clicking_and_reports_the_href() -> None:
    raw = _Raw(ControlInfo(tag="a", input_type="", in_form=False, href="/help"))
    driver = ExploringDriver(raw)
    inspection = driver.inspect("link", "Help")
    assert inspection == Inspection(refusal=None, href="/help", tag="a")
    assert raw.clicked == []
    raw.info = ControlInfo(tag="button", input_type="submit", in_form=True)
    assert driver.inspect("button", "Add").refusal == "it is a submit control"
    assert raw.clicked == []


def test_the_exploring_driver_restricts_navigation_to_the_allowed_hosts() -> None:
    raw = _Raw()
    ExploringDriver(raw, allowed_hosts=("erp.example.com", "*.example.org"))
    allowed = raw.restricted
    assert allowed is not None
    assert allowed("erp.example.com") is True
    assert allowed("a.example.org") is True
    assert allowed("evil.example.net") is False
    assert allowed("") is False


# --- third pass: menus, hrefs, flows, stuck drafts, typed-ahead input -------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Log-out",
        "Sign off",
        "Exit",
        "delete_account",
        "Archive",
        "Empty trash",
        "Move to trash",
        "Mark as read",
        "log_out",
    ],
)
def test_more_destructive_spellings_ask_first(name: str) -> None:
    assert decide_click("link", name) is ClickDecision.ASK


@pytest.mark.parametrize(
    "href",
    ["/account/delete?confirm=1", "/logout", "/users/42/remove", "/sign-out", "/api/trash/empty"],
)
def test_a_link_whose_address_looks_destructive_asks_first(href: str) -> None:
    driver = _Driver()
    driver.href = href
    explorer, confirmer = _explorer(driver, [False])
    assert explorer.click("link", "Settings") is False
    assert len(confirmer.asked) == 1
    assert explorer.last_outcome == "declined"


@pytest.mark.parametrize(
    "href",
    [
        "file:///etc/passwd",
        "mailto:a@b.example",
        "ftp://erp.example.com/x",
        "view-source:https://erp.example.com/",
    ],
)
def test_a_link_that_leaves_the_web_is_not_followed(href: str) -> None:
    driver = _Driver()
    driver.href = href
    explorer, _ = _explorer(driver)
    assert explorer.click("link", "Help") is False
    assert driver.clicks == []
    assert "leaves the web" in explorer.last_outcome


def test_the_click_receives_what_the_user_was_shown() -> None:
    driver = _Driver()
    driver.href = "/help"
    explorer, _ = _explorer(driver)
    explorer.click("link", "Help")
    assert driver.expected == Inspection(refusal=None, href="/help", tag="a")


def test_the_exploring_driver_refuses_an_element_that_changed_since_the_question() -> None:
    raw = _Raw(ControlInfo(tag="a", input_type="", in_form=False, href="/other"))
    with pytest.raises(ExploreRefusedError, match="changed"):
        ExploringDriver(raw).click_vetted(
            "link", "Help", Inspection(refusal=None, href="/help", tag="a")
        )
    assert raw.clicked == []
    raw.info = ControlInfo(tag="button", input_type="button", in_form=False, href="")
    with pytest.raises(ExploreRefusedError, match="changed"):
        ExploringDriver(raw).click_vetted(
            "link", "Help", Inspection(refusal=None, href="", tag="a")
        )


def test_a_click_that_lands_off_host_says_the_click_happened() -> None:
    driver = _Driver()
    driver.lands_on = "https://evil.example.net/x"
    explorer, _ = _explorer(driver)
    with pytest.raises(ExploreRefusedError, match="click happened"):
        explorer.click("link", "Help")
    assert driver.clicks == [("link", "Help")]


def test_continuing_a_stored_model_keeps_every_flow_and_its_name(tmp_path: Path) -> None:
    draft = _draft()
    draft.add_page(
        "expense-list",
        "https://erp.example.com/expenses",
        (ControlObservation("button", "Add line"), ControlObservation("button", "Edit")),
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    flows = (
        Flow(
            name="login", steps=(FlowStep("click", "expense-list.add-line", None, "expense-list"),)
        ),
        Flow(name="other", steps=(FlowStep("click", "expense-list.edit", None, "expense-list"),)),
    )
    stored_model = replace(model, flows=flows)
    again = ModelDraft.from_model(stored_model)
    again.add_step("click", page_id="expense-list", control="expense-list.add-line")
    rebuilt = again.to_model(recorded_at="2026-10-25T13:00:00Z", blumkin_version="1.0.0")
    assert [(f.name, len(f.steps)) for f in rebuilt.flows] == [
        ("login", 1),
        ("other", 1),
        ("recorded", 1),
    ]
    again2 = ModelDraft.from_model(rebuilt)
    rebuilt2 = again2.to_model(recorded_at="2026-10-25T14:00:00Z", blumkin_version="1.0.0")
    assert [(f.name, len(f.steps)) for f in rebuilt2.flows] == [
        ("login", 1),
        ("other", 1),
        ("recorded", 1),
    ]


def test_observing_a_page_again_drops_steps_whose_control_is_gone(tmp_path: Path) -> None:
    draft = _draft()
    draft.add_page("home", "https://erp.example.com/", (ControlObservation("button", "Go"),))
    draft.add_step("click", page_id="home", control="home.go")
    draft.add_page("home", "https://erp.example.com/", (ControlObservation("button", "Start"),))
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    assert model.flows == ()
    save_model(tmp_path, model)


def test_controls_beyond_the_draft_cap_are_counted_as_skipped() -> None:
    draft = _draft()
    controls = tuple(
        ControlObservation("button", f"Action {chr(97 + n // 26)}{chr(97 + n % 26)}")
        for n in range(230)
    )
    draft.add_page("expense-list", "https://erp.example.com/expenses", controls)
    assert draft.skipped >= 30


def test_pending_terminal_input_is_flushed_before_the_question() -> None:
    try:
        master, slave = os.openpty()
    except OSError:
        pytest.skip("no pseudo-terminal in this environment")
    try:
        os.write(master, b"y\n")  # typed ahead, before the question
        path = os.ttyname(slave)
        confirmer = TerminalConfirmer(open_tty=lambda: Terminal(path), timeout_s=0.05)
        assert confirmer.confirm("Click 'Add line'?") is False
        assert confirmer.timed_out is True
    finally:
        os.close(master)
        os.close(slave)


def test_a_real_terminal_is_asked_and_the_answer_read() -> None:
    try:
        master, slave = os.openpty()
    except OSError:
        pytest.skip("no pseudo-terminal in this environment")
    asked: list[str] = []

    def user() -> None:
        seen = b""
        while b"[y/N]" not in seen:
            seen += os.read(master, 1024)
        asked.append(seen.decode())
        os.write(master, b"y\n")

    helper = threading.Thread(target=user, daemon=True)
    helper.start()
    try:
        path = os.ttyname(slave)
        confirmer = TerminalConfirmer(open_tty=lambda: Terminal(path), timeout_s=10)
        assert confirmer.confirm("Click 'Add line'?") is True
        helper.join(10)
        assert "Add line" in asked[0]
        assert confirmer.unavailable is False
    finally:
        os.close(master)
        os.close(slave)


def test_a_missing_terminal_path_is_unavailable_not_a_crash() -> None:
    confirmer = TerminalConfirmer(open_tty=lambda: Terminal("/nonexistent/tty"))
    assert confirmer.confirm("Click?") is False
    assert confirmer.unavailable is True


# --- fourth pass: background jobs, query-string verbs, new windows, ambiguity ------------------


class _FdTty:
    """A terminal double with a real descriptor, so process-group checks can be exercised."""

    def __init__(self, descriptor: int, reply: str = "y\n") -> None:
        self._descriptor = descriptor
        self.reply = reply
        self.read = False

    def fileno(self) -> int:
        return self._descriptor

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        self.read = True
        return self.reply

    def close(self) -> None:
        return None


def test_a_background_process_group_gets_no_question_and_is_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_end, write_end = os.pipe()
    tty = _FdTty(read_end)
    monkeypatch.setattr("os.tcgetpgrp", lambda fd: os.getpgrp() + 1)
    try:
        confirmer = TerminalConfirmer(open_tty=lambda: tty, timeout_s=0.05)
        assert confirmer.confirm("Click?") is False
        assert confirmer.unavailable is True
        assert tty.read is False
    finally:
        os.close(read_end)
        os.close(write_end)


@pytest.mark.parametrize(
    "error", [OSError(5, "I/O error"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")]
)
def test_a_terminal_that_fails_while_answering_means_no(error: Exception) -> None:
    class _Failing(_Tty):
        def readline(self) -> str:
            raise error

    confirmer = TerminalConfirmer(open_tty=lambda: _Failing(""))
    assert confirmer.confirm("Click?") is False


def test_closing_a_terminal_closes_both_handles_even_when_one_fails() -> None:
    try:
        master, slave = os.openpty()
    except OSError:
        pytest.skip("no pseudo-terminal in this environment")
    try:
        terminal = Terminal(os.ttyname(slave))
        original = terminal._writer.close

        def failing_close() -> None:
            original()
            raise OSError(5, "I/O error")

        terminal._writer.close = failing_close  # type: ignore[method-assign]
        with pytest.raises(OSError):
            terminal.close()
        assert terminal._reader.closed
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "href",
    [
        "/items/5?action=delete",
        "/do?op=remove",
        "/x#delete",
        "/deleteItem/5",
        "/removeUser",
        "/logOut",
        "/destroy/7",
        "/purge",
        "/admin/wipe",
        "/session/terminate",
        "/account/close-account",
    ],
)
def test_destructive_words_anywhere_in_a_link_address_ask_first(href: str) -> None:
    driver = _Driver()
    driver.href = href
    explorer, confirmer = _explorer(driver, [False])
    assert explorer.click("link", "Open") is False
    assert len(confirmer.asked) == 1


@pytest.mark.parametrize(
    "href", ["/help", "/expenses/42/line", "help.html", "#top", "/reports?page=2"]
)
def test_ordinary_link_addresses_do_not_ask(href: str) -> None:
    driver = _Driver()
    driver.href = href
    explorer, confirmer = _explorer(driver)
    assert explorer.click("link", "Open") is True
    assert confirmer.asked == []


def test_a_link_that_opens_a_new_window_is_not_followed() -> None:
    raw = _Raw(ControlInfo(tag="a", input_type="", in_form=False, href="/help", target="_blank"))
    driver = ExploringDriver(raw)
    inspection = driver.inspect("link", "Help")
    assert inspection.refusal == "it opens a new window"
    with pytest.raises(ExploreRefusedError, match="new window"):
        driver.click_vetted("link", "Help", Inspection(refusal=None, href="/help", tag="a"))
    assert raw.clicked == []


def test_a_link_that_stays_in_the_window_is_followed() -> None:
    for target in ("", "_self"):
        raw = _Raw(ControlInfo(tag="a", input_type="", in_form=False, href="/help", target=target))
        driver = ExploringDriver(raw)
        assert driver.inspect("link", "Help").refusal is None


def test_a_control_that_cannot_be_found_uniquely_is_a_refusal_not_a_crash() -> None:
    class _Ambiguous(_Driver):
        def inspect(self, role: str, name: str) -> Inspection:
            raise BrowserActionError("Expected one match, got 2 (strict mode)")

    explorer, confirmer = _explorer(_Ambiguous(), [True])
    assert explorer.click("button", "Add line") is False
    assert explorer.last_outcome.startswith("refused: ")
    assert "strict mode" in explorer.last_outcome
    assert confirmer.asked == []


def test_a_long_word_in_a_control_name_still_gives_a_storable_draft() -> None:
    draft = ModelDraft(task="t", hosts=("erp.example.com",))
    draft.add_page(
        "p",
        "https://erp.example.com/p",
        (ControlObservation("button", "Telecommunications"),),
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")
    assert [c.name for c in model.pages[0].controls] == ["p.telecommunicatio"]


def test_the_exploring_driver_hands_the_indicator_request_to_the_raw_driver() -> None:
    raw = _Raw()
    driver = ExploringDriver(raw)

    driver.mark_window("expense-report", ("erp.example.com",), ("login.example.net",))

    assert raw.marks == [("expense-report", ("erp.example.com",), ("login.example.net",))]


# --------------------------------------------------------------------------- explore fixes (#608)


@pytest.mark.parametrize(
    ("role", "name"),
    [
        ("combobox", "Payment Method"),
        ("textbox", "Checkout Date"),
        ("textbox", "File Name"),
        ("searchbox", "Pay to"),
        ("checkbox", "Send me a copy"),
        ("spinbutton", "Pay amount"),
    ],
)
def test_a_field_is_not_finalizing_whatever_words_its_name_holds(role: str, name: str) -> None:
    draft = _draft()
    draft.add_page(
        "expense-item", "https://erp.example.com/item", (ControlObservation(role, name),)
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")

    assert model.pages[0].controls[0].safety is ControlSafety.SAFE


@pytest.mark.parametrize(
    ("role", "name"),
    [
        ("button", "Pay now"),
        ("link", "Submit report"),
        ("menuitem", "Send"),
        ("tab", "Checkout"),
        ("button", "Sub mit"),
    ],
)
def test_an_actionable_control_with_a_finalizing_name_is_still_finalizing(
    role: str, name: str
) -> None:
    draft = _draft()
    draft.add_page(
        "expense-item", "https://erp.example.com/item", (ControlObservation(role, name),)
    )
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")

    assert model.pages[0].controls[0].safety is ControlSafety.FINALIZING


def test_forgetting_a_page_drops_it_and_the_steps_that_used_it() -> None:
    draft = _draft()
    draft.add_page("expense-list", "https://erp.example.com/a", parse_aria_snapshot(_SNAPSHOT))
    draft.add_page("old-page", "https://erp.example.com/b", parse_aria_snapshot(_SNAPSHOT))
    draft.add_step("click", page_id="old-page", control="old-page.add-line")
    draft.add_step("click", page_id="expense-list", control="expense-list.add-line")

    assert draft.forget("old-page") == {"recorded": 1}
    model = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")

    assert [page.page_id for page in model.pages] == ["expense-list"]
    assert [step.page for flow in model.flows for step in flow.steps] == ["expense-list"]


def test_forgetting_a_page_the_draft_does_not_have_changes_nothing() -> None:
    draft = _draft()
    draft.add_page("expense-list", "https://erp.example.com/a", parse_aria_snapshot(_SNAPSHOT))

    assert draft.forget("nope") is None
    assert len(draft.to_model(recorded_at="t", blumkin_version="1").pages) == 1


def test_pages_that_share_an_address_but_differ_by_landmarks_are_reported() -> None:
    draft = _draft()
    draft.add_page(
        "overview", "https://erp.example.com/ui/Overview", parse_aria_snapshot(_SNAPSHOT)
    )
    draft.add_page(
        "report-edit",
        "https://erp.example.com/ui/Overview",
        (ControlObservation("button", "Save and Close"), ControlObservation("textbox", "Purpose")),
    )
    draft.add_page("other", "https://erp.example.com/ui/Other", parse_aria_snapshot(_SNAPSHOT))

    assert draft.shared_addresses() == {"/ui/Overview": ("overview", "report-edit")}


def test_pages_with_the_same_address_and_landmarks_are_not_a_warning() -> None:
    draft = _draft()
    draft.add_page("a-page", "https://erp.example.com/x", parse_aria_snapshot(_SNAPSHOT))
    draft.add_page("b-page", "https://erp.example.com/x", parse_aria_snapshot(_SNAPSHOT))

    assert draft.shared_addresses() == {}


def test_forgetting_a_middle_page_reports_which_flow_lost_steps() -> None:
    draft = _draft()
    for page in ("first-page", "middle-page", "last-page"):
        draft.add_page(page, f"https://erp.example.com/{page}", parse_aria_snapshot(_SNAPSHOT))
        draft.add_step("click", page_id=page, control=f"{page}.add-line")

    assert draft.forget("middle-page") == {"recorded": 1}
    assert draft.forget("last-page") == {"recorded": 1}
    assert draft.forget("first-page") == {"recorded": 1}
