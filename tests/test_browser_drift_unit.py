"""Drift check: has the website changed since the site model was recorded? (issue #487)."""

from __future__ import annotations

import pytest

from blumkin.browser.drift import (
    FindingKind,
    Grade,
    check_page,
    diff_models,
    worst,
)
from blumkin.browser.explore import (
    MAX_DRAFT_CONTROLS,
    ControlObservation,
    ModelDraft,
    parse_aria_snapshot,
)
from blumkin.browser.model import Control, Locator, LocatorStrategy, Page

_RECORDED = """\
- heading "Expenses" [level=1]
- navigation:
  - link "Help"
  - link "Reports"
- main:
  - button "Add line"
  - textbox "Amount"
  - combobox "Category"
  - checkbox "Billable"
  - button "Submit report"
"""


def _obs(text: str = _RECORDED) -> tuple[ControlObservation, ...]:
    return parse_aria_snapshot(text)


def _recorded_page() -> Page:
    draft = ModelDraft(task="expense-report", hosts=("erp.example.com",))
    draft.add_page("expense-list", "https://erp.example.com/expenses", _obs())
    return draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0").pages[0]


def _findings(text: str, **kwargs: object) -> tuple[Grade, dict[FindingKind, list[str | None]]]:
    check = check_page(_recorded_page(), _obs(text))
    found: dict[FindingKind, list[str | None]] = {}
    for finding in check.findings:
        found.setdefault(finding.kind, []).append(finding.control)
    return check.grade, found


def test_an_unchanged_page_is_compatible_with_no_findings() -> None:
    check = check_page(_recorded_page(), _obs())
    assert check.grade is Grade.COMPATIBLE
    assert check.findings == ()
    assert check.page_id == "expense-list"


def test_a_layout_only_change_is_ignored() -> None:
    reordered = "\n".join(reversed(_RECORDED.strip().splitlines()))
    grade, found = _findings(reordered)
    assert grade is Grade.COMPATIBLE
    assert found == {}


def test_a_renamed_control_is_incompatible_and_named() -> None:
    grade, found = _findings(_RECORDED.replace('button "Add line"', 'button "Add row"'))
    assert grade is Grade.INCOMPATIBLE
    assert found[FindingKind.MISSING] == ["expense-list.add-line"]


def test_a_removed_control_is_incompatible() -> None:
    grade, found = _findings(_RECORDED.replace('  - textbox "Amount"\n', ""))
    assert grade is Grade.INCOMPATIBLE
    assert found[FindingKind.MISSING] == ["expense-list.amount"]


def test_a_new_field_is_incompatible_because_it_may_be_required() -> None:
    grade, found = _findings(_RECORDED + '  - textbox "Cost center"\n')
    assert grade is Grade.INCOMPATIBLE
    assert found[FindingKind.NEW_FIELD] == [None]


def test_a_new_button_or_link_is_not_drift() -> None:
    grade, found = _findings(_RECORDED + '  - button "Export"\n  - link "Support"\n')
    assert grade is Grade.COMPATIBLE
    assert found == {}


def test_a_control_found_only_by_its_fallback_locator_is_degraded() -> None:
    # The textbox became a combobox with the same label: the label locator still finds it.
    grade, found = _findings(_RECORDED.replace('textbox "Amount"', 'combobox "Amount"'))
    assert grade is Grade.DEGRADED
    assert found[FindingKind.FALLBACK] == ["expense-list.amount"]


def test_a_control_with_only_selector_locators_cannot_be_verified() -> None:
    page = _recorded_page()
    controls = tuple(
        Control(
            kind=c.kind,
            locators=(Locator(strategy=LocatorStrategy.CSS, value="#x"),)
            if c.name == "expense-list.add-line"
            else c.locators,
            name=c.name,
            safety=c.safety,
        )
        for c in page.controls
    )
    check = check_page(
        Page(
            controls=controls,
            fingerprint=page.fingerprint,
            page_id=page.page_id,
            url_template=page.url_template,
        ),
        _obs(),
    )
    assert check.grade is Grade.DEGRADED
    assert [f.kind for f in check.findings] == [FindingKind.UNVERIFIED]


def test_a_page_that_looks_entirely_different_is_incompatible() -> None:
    other = '- heading "Sign in"\n- textbox "Username"\n- textbox "Password"\n- button "Next"\n'
    check = check_page(_recorded_page(), _obs(other))
    assert check.grade is Grade.INCOMPATIBLE
    assert FindingKind.LOOKS_DIFFERENT in {f.kind for f in check.findings}


def test_an_empty_observation_is_incompatible() -> None:
    check = check_page(_recorded_page(), ())
    assert check.grade is Grade.INCOMPATIBLE
    assert FindingKind.LOOKS_DIFFERENT in {f.kind for f in check.findings}


def test_a_changed_heading_on_an_otherwise_intact_page_stays_compatible() -> None:
    changed = _RECORDED.replace('heading "Expenses"', 'heading "My expenses"')
    check = check_page(_recorded_page(), _obs(changed))
    assert check.grade is Grade.COMPATIBLE
    assert check.findings == ()


def test_several_changed_headings_make_the_page_degraded_not_incompatible() -> None:
    # Eight recorded landmarks: losing two leaves 75 %, below the unchanged threshold.
    changed = _RECORDED.replace('heading "Expenses"', 'heading "My expenses"')
    page = _recorded_page()
    page = Page(
        controls=page.controls,
        fingerprint=(*page.fingerprint, "heading:Totals"),
        page_id=page.page_id,
        url_template=page.url_template,
    )
    check = check_page(page, _obs(changed))
    assert check.grade is Grade.DEGRADED
    assert [f.kind for f in check.findings] == [FindingKind.LOOKS_DIFFERENT]


def test_data_like_labels_on_the_live_page_are_not_new_fields() -> None:
    grade, found = _findings(_RECORDED + '  - textbox "Card 4111111111111111"\n')
    assert grade is Grade.COMPATIBLE
    assert FindingKind.NEW_FIELD not in found


def test_the_worst_grade_wins() -> None:
    assert worst([]) is Grade.COMPATIBLE
    assert worst([Grade.COMPATIBLE, Grade.DEGRADED]) is Grade.DEGRADED
    assert worst([Grade.DEGRADED, Grade.INCOMPATIBLE, Grade.COMPATIBLE]) is Grade.INCOMPATIBLE


def test_every_finding_says_what_to_do() -> None:
    check = check_page(
        _recorded_page(), _obs(_RECORDED.replace('button "Add line"', 'button "Add row"'))
    )
    assert all(f.note for f in check.findings)
    assert (
        "expense-list.add-line" in check.findings[0].note
        or check.findings[0].control == "expense-list.add-line"
    )


def _model(*, extra_page: bool = False, rename: bool = False, host: str = "erp.example.com"):
    draft = ModelDraft(task="expense-report", hosts=(host,))
    text = _RECORDED.replace('"Add line"', '"Add row"') if rename else _RECORDED
    draft.add_page("expense-list", f"https://{host}/expenses", _obs(text))
    if extra_page:
        draft.add_page(
            "expense-form", f"https://{host}/expenses/1/line", _obs('- textbox "Amount"\n')
        )
    return draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0")


def test_identical_models_have_an_empty_diff() -> None:
    diff = diff_models(_model(), _model())
    assert diff.empty is True
    assert diff.lines() == ["no differences"]


def test_a_diff_lists_pages_controls_and_hosts_that_changed() -> None:
    diff = diff_models(_model(), _model(extra_page=True, rename=True, host="erp2.example.com"))
    assert diff.pages_added == ("expense-form",)
    assert diff.hosts_added == ("erp2.example.com",)
    assert diff.hosts_removed == ("erp.example.com",)
    assert diff.controls_added["expense-list"] == ("expense-list.add-row",)
    assert diff.controls_removed["expense-list"] == ("expense-list.add-line",)
    assert diff.empty is False
    text = "\n".join(diff.lines())
    assert "expense-form" in text
    assert "expense-list.add-row" in text


def test_a_changed_locator_or_safety_flag_is_a_changed_control() -> None:
    before = _model()
    page = before.pages[0]
    changed_controls = tuple(
        Control(
            kind=c.kind,
            locators=(Locator(strategy=LocatorStrategy.ROLE_NAME, value="button:Insert line"),),
            name=c.name,
            safety=c.safety,
        )
        if c.name == "expense-list.add-line"
        else c
        for c in page.controls
    )
    from dataclasses import replace

    after = replace(before, pages=(replace(page, controls=changed_controls),))
    diff = diff_models(before, after)
    assert diff.controls_changed["expense-list"] == ("expense-list.add-line",)
    assert diff.controls_added == {}


@pytest.mark.parametrize("grade", list(Grade))
def test_grades_have_stable_text(grade: Grade) -> None:
    assert grade.value in {"compatible", "degraded", "incompatible"}


# --- review pass: data-driven pages, tiny pages, aliases, truncation, richer diffs ------------


_INBOX = """\
- heading "Inbox" [level=1]
- button "Compose"
- link "Mail from Bob"
- link "Mail from Amy"
- link "Mail from Cy"
"""


def _inbox_page() -> Page:
    draft = ModelDraft(task="mail", hosts=("mail.example.com",))
    draft.add_page("inbox", "https://mail.example.com/inbox", parse_aria_snapshot(_INBOX))
    return draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0").pages[0]


def test_a_list_page_whose_data_links_changed_is_degraded_not_incompatible() -> None:
    live = '- heading "Inbox"\n- button "Compose"\n- link "Mail from Dee"\n- link "Mail from Eve"\n'
    check = check_page(_inbox_page(), parse_aria_snapshot(live))
    assert check.grade is Grade.DEGRADED
    assert {f.kind for f in check.findings} == {FindingKind.MISSING}
    assert all(f.grade is Grade.DEGRADED for f in check.findings)


def test_a_missing_button_on_a_list_page_is_still_incompatible() -> None:
    live = (
        '- heading "Inbox"\n- link "Mail from Bob"\n- link "Mail from Amy"\n- link "Mail from Cy"\n'
    )
    check = check_page(_inbox_page(), parse_aria_snapshot(live))
    assert check.grade is Grade.INCOMPATIBLE


@pytest.mark.parametrize("role", ["slider", "menuitemcheckbox", "menuitemradio"])
def test_a_new_input_of_any_kind_may_be_required(role: str) -> None:
    grade, found = _findings(_RECORDED + f'  - {role} "Quantity"\n')
    assert grade is Grade.INCOMPATIBLE
    assert FindingKind.NEW_FIELD in found


def test_a_page_with_too_few_landmarks_is_judged_by_whether_any_is_present() -> None:
    draft = ModelDraft(task="login", hosts=("erp.example.com",))
    draft.add_page(
        "login", "https://erp.example.com/login", (ControlObservation("heading", "Login"),)
    )
    page = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0").pages[0]
    same = check_page(page, (ControlObservation("heading", "Login"),))
    assert same.grade is Grade.COMPATIBLE
    renamed = check_page(page, (ControlObservation("heading", "Sign in"),))
    assert renamed.grade is Grade.INCOMPATIBLE
    assert check_page(page, ()).grade is Grade.INCOMPATIBLE


def test_a_page_recorded_with_the_control_cap_does_not_report_every_extra_field() -> None:
    names = [f"Field {chr(97 + n // 26)}{chr(97 + n % 26)}" for n in range(MAX_DRAFT_CONTROLS + 5)]
    observed = tuple(ControlObservation("textbox", name) for name in names)
    draft = ModelDraft(task="big", hosts=("erp.example.com",))
    draft.add_page("big-form", "https://erp.example.com/big", observed)
    page = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0").pages[0]
    check = check_page(page, observed)
    assert FindingKind.NEW_FIELD not in {f.kind for f in check.findings}


@pytest.mark.parametrize(
    ("recorded", "live"),
    [("textbox", "searchbox"), ("searchbox", "textbox"), ("combobox", "listbox")],
)
def test_a_role_that_the_page_treats_alike_is_a_fallback_not_a_loss(
    recorded: str, live: str
) -> None:
    draft = ModelDraft(task="t", hosts=("erp.example.com",))
    draft.add_page(
        "p",
        "https://erp.example.com/p",
        (
            ControlObservation("heading", "One"),
            ControlObservation("heading", "Two"),
            ControlObservation("heading", "Three"),
            ControlObservation(recorded, "Amount"),
        ),
    )
    page = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0").pages[0]
    observed = (
        ControlObservation("heading", "One"),
        ControlObservation("heading", "Two"),
        ControlObservation("heading", "Three"),
        ControlObservation(live, "Amount"),
    )
    check = check_page(page, observed)
    assert check.grade is Grade.DEGRADED
    assert [f.kind for f in check.findings] == [FindingKind.FALLBACK]


def test_a_diff_reports_a_changed_page_address_fingerprint_and_flows() -> None:
    from dataclasses import replace

    from blumkin.browser.model import Flow, FlowStep

    before = _model()
    page = before.pages[0]
    after = replace(
        before,
        pages=(replace(page, url_template="/expenses/all", fingerprint=("heading:Other",)),),
        flows=(Flow(name="recorded", steps=(FlowStep("click", None, None, "expense-list"),)),),
    )
    diff = diff_models(before, after)
    assert diff.pages_changed == ("expense-list",)
    assert diff.flows_changed == ("recorded",)
    assert diff.empty is False
    text = "\n".join(diff.lines())
    assert "page changed: expense-list" in text
    assert "flow changed: recorded" in text


def test_a_diff_names_a_safety_flip() -> None:
    from dataclasses import replace

    from blumkin.browser.model import ControlSafety

    before = _model()
    page = before.pages[0]
    flipped = tuple(
        replace(c, safety=ControlSafety.FINALIZING) if c.name == "expense-list.add-line" else c
        for c in page.controls
    )
    after = replace(before, pages=(replace(page, controls=flipped),))
    diff = diff_models(before, after)
    assert diff.safety_changed == {"expense-list": ("expense-list.add-line",)}
    assert "now finalizing" in "\n".join(diff.lines()) or "safety" in "\n".join(diff.lines())


def test_a_thin_page_whose_recorded_landmarks_are_all_gone_is_incompatible() -> None:
    draft = ModelDraft(task="t", hosts=("erp.example.com",))
    draft.add_page(
        "p",
        "https://erp.example.com/p",
        (ControlObservation("heading", "Expenses"), ControlObservation("link", "Help")),
    )
    page = draft.to_model(recorded_at="2026-10-25T12:00:00Z", blumkin_version="1.0.0").pages[0]
    observed = (
        ControlObservation("heading", "Sign in"),
        ControlObservation("textbox", "Password"),
    )
    check = check_page(page, observed)
    assert check.grade is Grade.INCOMPATIBLE
    assert FindingKind.LOOKS_DIFFERENT in {f.kind for f in check.findings}
