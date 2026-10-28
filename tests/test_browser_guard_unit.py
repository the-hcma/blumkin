"""The submit guard: a browser task can fill and upload but never finalize (issue #467)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from blumkin.browser.cdp import BrowserActionError, ControlInfo, FrameCapture, PageText
from blumkin.browser.guard import (
    DEFAULT_DENY_NAMES,
    GuardedDriver,
    GuardPolicy,
    SubmitBlockedError,
    attach_guarded,
)
from blumkin.browser.profiles import BrowserProfile

_PLAIN = ControlInfo(tag="button", input_type="button", in_form=False)


class _Raw:
    """A stand-in for the raw driver: records what reaches the page."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.controls: dict[str, ControlInfo] = {}
        self.fields: dict[str, ControlInfo] = {}
        self.frame_chunks: tuple[str, ...] = ()
        self.net_installed = 0
        self.url = "https://erp.example.com/expenses"

    def block_form_submission(self) -> None:
        self.net_installed += 1

    def checked_click(self, name: str, role: str, vet: Callable[[ControlInfo], None]) -> None:
        vet(self.controls.get(name, _PLAIN))
        self.calls.append(("click", role, name))

    def close(self) -> None:
        self.calls.append(("close",))

    def describe_field(self, label: str) -> ControlInfo:
        return self.fields.get(label, ControlInfo(tag="input", input_type="text", in_form=True))

    def fill(self, label: str, value: str) -> None:
        self.calls.append(("fill", label, value))

    def goto(self, url: str) -> None:
        self.calls.append(("goto", url))

    def raw_press(self, label: str, key: str) -> None:
        self.calls.append(("press", label, key))

    def read_text(self) -> PageText:
        return PageText(text="Total 12.50", injection_warning=None)

    def scroll_frame_text(
        self, is_allowed: Callable[[str], bool], *, max_steps: int, wait_s: float
    ) -> FrameCapture:
        self.calls.append(("scroll", is_allowed("x.sharepoint.com"), max_steps))
        return FrameCapture(
            chunks=self.frame_chunks, reached_end=True, reached_top=True, steps=1, url="u"
        )

    def title(self) -> str:
        return "Expenses"

    def upload(self, label: str, path: Path) -> None:
        self.calls.append(("upload", label, str(path)))


def _guard(*allowed: str, deny: tuple[str, ...] = ()) -> tuple[_Raw, GuardedDriver]:
    raw = _Raw()
    policy = GuardPolicy(allowed_controls=frozenset(allowed), extra_deny_names=deny)
    return raw, GuardedDriver(raw, policy)


def test_building_a_guarded_driver_installs_the_network_safety_net_once() -> None:
    raw, _ = _guard()
    assert raw.net_installed == 1


def test_a_declared_ordinary_button_is_clicked() -> None:
    raw, driver = _guard("Add line")
    driver.click("Add line")
    assert raw.calls == [("click", "button", "Add line")]


def test_a_declared_button_outside_any_form_is_clicked_even_with_the_default_submit_type() -> None:
    raw, driver = _guard("Add line")
    raw.controls["Add line"] = ControlInfo(tag="button", input_type="submit", in_form=False)
    driver.click("Add line")
    assert raw.calls == [("click", "button", "Add line")]


def test_an_undeclared_control_is_refused() -> None:
    raw, driver = _guard("Add line")
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Delete everything")
    assert "not declared" in str(excinfo.value)
    assert raw.calls == []


@pytest.mark.parametrize("input_type", ["submit", "image"])
def test_a_submit_input_is_refused_even_when_declared(input_type: str) -> None:
    raw, driver = _guard("Go")
    raw.controls["Go"] = ControlInfo(tag="input", input_type=input_type, in_form=False)
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Go")
    assert "submit" in str(excinfo.value).lower()
    assert raw.calls == []


def test_a_submit_button_inside_a_form_is_refused_even_when_declared() -> None:
    raw, driver = _guard("Continue")
    raw.controls["Continue"] = ControlInfo(tag="button", input_type="submit", in_form=True)
    with pytest.raises(SubmitBlockedError):
        driver.click("Continue")
    assert raw.calls == []


def test_a_control_with_a_formaction_is_refused_even_when_declared() -> None:
    raw, driver = _guard("Alt")
    raw.controls["Alt"] = ControlInfo(
        tag="button", input_type="button", in_form=True, has_formaction=True
    )
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Alt")
    assert "formaction" in str(excinfo.value)
    assert raw.calls == []


@pytest.mark.parametrize(
    "href",
    [
        "javascript:document.forms[0].submit()",
        " JavaScript:void(0)",
        "data:text/html,<form></form>",
        "vbscript:x",
        "java\tscript:alert(1)",
    ],
)
def test_a_link_that_runs_script_is_refused_even_when_declared(href: str) -> None:
    raw, driver = _guard("Continue")
    raw.controls["Continue"] = ControlInfo(tag="a", input_type="", in_form=False, href=href)
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Continue", "link")
    assert "script" in str(excinfo.value)
    assert raw.calls == []


@pytest.mark.parametrize("href", ["https://erp.example.com/next", "/next", "#section", ""])
def test_an_ordinary_declared_link_is_clicked(href: str) -> None:
    raw, driver = _guard("Next week")
    raw.controls["Next week"] = ControlInfo(tag="a", input_type="", in_form=False, href=href)
    driver.click("Next week", "link")
    assert raw.calls == [("click", "link", "Next week")]


def test_a_control_whose_click_handler_submits_a_form_is_refused() -> None:
    raw, driver = _guard("Tidy")
    raw.controls["Tidy"] = ControlInfo(
        tag="button",
        input_type="button",
        in_form=True,
        inline_handler="document.forms[0].submit()",
    )
    with pytest.raises(SubmitBlockedError):
        driver.click("Tidy")
    assert raw.calls == []


@pytest.mark.parametrize("role", ["textbox", "heading", "row", "form", "", "BUTTON "])
def test_only_clickable_roles_are_accepted(role: str) -> None:
    raw, driver = _guard("Add line")
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Add line", role)
    assert "role" in str(excinfo.value)
    assert raw.calls == []


@pytest.mark.parametrize("role", ["button", "checkbox", "link", "menuitem", "radio", "tab"])
def test_the_clickable_roles_work(role: str) -> None:
    raw, driver = _guard("Add line")
    driver.click("Add line", role)
    assert raw.calls == [("click", role, "Add line")]


@pytest.mark.parametrize(
    "name",
    [
        "Submit",
        "SUBMIT",
        " submit ",
        "Sub​mit",
        "Send report",
        "File report",
        "Confirm",
        "Confirmation",
        "Pay now",
        "Payment",
        "Save and Submit",
        "Place order",
        "Finish",
        "Complete",
        "Approve",
        "Resend",
        "Resubmit",
        "Sending",
        "Authorize",
        "Checkout",
        "Buy now",
        "Publish",
        "Register",
        "Transmit",
    ],
)
def test_a_control_named_like_a_finalizing_action_is_refused_even_when_declared(
    name: str,
) -> None:
    raw, driver = _guard(name)
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click(name)
    assert "never submits" in str(excinfo.value)
    assert raw.calls == []


@pytest.mark.parametrize("name", ["Add line", "Profile", "Resubmission notes", "Next week", "Save"])
def test_ordinary_names_are_not_mistaken_for_finalizing_ones(name: str) -> None:
    raw, driver = _guard(name)
    driver.click(name)
    assert raw.calls == [("click", "button", name)]


def test_the_task_can_extend_the_deny_list() -> None:
    raw, driver = _guard("Release", deny=("release",))
    with pytest.raises(SubmitBlockedError):
        driver.click("Release")
    assert raw.calls == []


def test_declared_names_match_exactly_like_the_browser_does() -> None:
    raw, driver = _guard("Add Line")
    with pytest.raises(SubmitBlockedError):
        driver.click("add line")
    with pytest.raises(SubmitBlockedError):
        driver.click("ADD LINE")
    driver.click("Add Line")
    assert raw.calls == [("click", "button", "Add Line")]


def test_a_declared_name_with_odd_spacing_still_matches_after_normalization() -> None:
    raw, driver = _guard("Add  line")
    driver.click(" Add line ")
    assert raw.calls == [("click", "button", " Add line ")]


@pytest.mark.parametrize("key", ["Tab", "Shift+Tab", "Escape", "ArrowDown", "Backspace", "a", "7"])
def test_only_navigation_and_printable_keys_are_allowed(key: str) -> None:
    raw, driver = _guard()
    driver.press("Amount", key)
    assert raw.calls == [("press", "Amount", key)]


@pytest.mark.parametrize(
    "key",
    [
        "Enter",
        "NumpadEnter",
        "Return",
        "Control+Enter",
        "Shift+Enter",
        "Meta+Enter",
        "enter",
        "\n",
        "\r",
        " ",
        "Space",
        "Control+a",
        "Alt+Tab",
        "F5",
        "",
    ],
)
def test_enter_space_modifier_combinations_and_unknown_keys_are_refused(key: str) -> None:
    raw, driver = _guard()
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.press("Amount", key)
    assert "key" in str(excinfo.value)
    assert raw.calls == []


@pytest.mark.parametrize(
    "info",
    [
        ControlInfo(tag="button", input_type="submit", in_form=True),
        ControlInfo(tag="button", input_type="button", in_form=False),
        ControlInfo(tag="input", input_type="submit", in_form=True),
        ControlInfo(tag="input", input_type="image", in_form=True),
        ControlInfo(tag="input", input_type="button", in_form=True),
        ControlInfo(tag="input", input_type="reset", in_form=True),
        ControlInfo(tag="a", input_type="", in_form=False, href="/x"),
    ],
)
def test_a_key_is_never_pressed_on_a_button_like_element(info: ControlInfo) -> None:
    raw, driver = _guard()
    raw.fields["Go now"] = info
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.press("Go now", "a")
    assert "field" in str(excinfo.value)
    assert raw.calls == []


def test_filling_uploading_navigating_and_reading_pass_through(tmp_path: Path) -> None:
    raw, driver = _guard()
    receipt = tmp_path / "r.png"
    driver.goto("https://erp.example.com/expenses")
    driver.fill("Amount", "12.50")
    driver.upload("Receipt", receipt)
    assert driver.read_text().text == "Total 12.50"
    assert driver.title() == "Expenses"
    assert driver.url == "https://erp.example.com/expenses"
    driver.close()
    assert [c[0] for c in raw.calls] == ["goto", "fill", "upload", "close"]


def test_a_refusal_is_logged_by_control_name_only(caplog: pytest.LogCaptureFixture) -> None:
    raw, driver = _guard()
    raw.url = "https://erp.example.com/expenses?token=SECRET-PAGE-VALUE"
    with (
        caplog.at_level(logging.INFO, logger="blumkin.browser.guard"),
        pytest.raises(SubmitBlockedError),
    ):
        driver.click("Submit")
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "Submit" in messages
    assert "refused" in messages
    assert "SECRET-PAGE-VALUE" not in messages
    assert "Total 12.50" not in messages


def test_refusals_follow_the_expected_got_wording() -> None:
    _, driver = _guard()
    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Submit")
    message = str(excinfo.value)
    assert message.startswith("Expected ")
    assert ", got " in message


def test_the_public_surface_has_no_way_to_finalize_a_form() -> None:
    _, driver = _guard()
    public = {name for name in dir(driver) if not name.startswith("_")}
    assert public == {
        "aria_snapshot",
        "click",
        "close",
        "fill",
        "frame_aria_snapshot",
        "frame_list",
        "goto",
        "press",
        "read_frame_text",
        "read_text",
        "scroll_frame_text",
        "title",
        "upload",
        "url",
    }


def test_the_default_deny_list_names_the_finalizing_words() -> None:
    for word in ("submit", "send", "file", "confirm", "pay", "finish", "complete", "approve"):
        assert word in DEFAULT_DENY_NAMES


@pytest.mark.parametrize(
    ("extra", "name"),
    [
        ("e-mail", "E-mail it"),
        ("e-mail", "Email it"),
        ("re-open", "Re-open case"),
        ("sign off", "Sign-off now"),
        ("e_sign", "E sign"),
        ("Re­-Open", "re-open"),
    ],
)
def test_a_deny_word_with_punctuation_matches_the_same_way_a_name_is_read(
    extra: str, name: str
) -> None:
    raw, driver = _guard(name, deny=(extra,))
    with pytest.raises(SubmitBlockedError):
        driver.click(name)
    assert raw.calls == []


def test_attach_guarded_closes_the_raw_driver_when_the_safety_net_cannot_be_installed() -> None:
    class _NetFails(_Raw):
        def block_form_submission(self) -> None:
            raise BrowserActionError("Expected the safety net, got an error")

    raw = _NetFails()
    with pytest.raises(BrowserActionError):
        attach_guarded(
            BrowserProfile(task="x", path=Path("/nonexistent")),
            GuardPolicy(),
            attach_raw=lambda profile, **kwargs: raw,
        )
    assert ("close",) in raw.calls
