"""The submit guard: a browser task can fill and upload but never finalize (issue #467)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from blumkin.browser.cdp import (
    BrowserActionError,
    ControlInfo,
    DownloadedFile,
    FrameCapture,
    FrameInfo,
    PageText,
)
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
        #: What the guard's vet callback is shown when `select` clicks something.
        self.select_probe: ControlInfo | None = None
        #: The accessible name the field resolved to, shown to the guard's name check.
        self.select_field_name: str | None = None
        self.frame_chunks: tuple[str, ...] = ()
        self.net_installed = 0
        #: name -> roles a control of that name exists in.
        self.present: dict[str, tuple[str, ...]] = {}
        self.probes: list[tuple[str, tuple[str, ...], float]] = []
        #: The `nth` each lookup and each click was given.
        self.nths: list[int | None] = []
        self.url = "https://erp.example.com/expenses"

    def matching_roles(
        self, name: str, roles: Sequence[str], wait_s: float, nth: int | None = None
    ) -> tuple[str, ...]:
        self.probes.append((name, tuple(roles), wait_s))
        self.nths.append(nth)
        present = self.present.get(name, tuple(roles) if len(roles) == 1 else ("button",))
        return tuple(role for role in roles if role in present)

    def checked_download(
        self,
        name: str,
        role: str,
        vet: Callable[[ControlInfo], None],
        *,
        save_to: Path,
        timeout_s: float,
    ) -> DownloadedFile:
        vet(self.controls.get(name, _PLAIN))
        self.calls.append(("download", role, name, timeout_s))
        return DownloadedFile(url="https://a.example.com/f", filename="f.vtt", path=save_to / "x")

    def aria_snapshot(self) -> str:
        self.calls.append(("aria",))
        return '- button "Go"'

    def block_form_submission(self, *, background_writes: bool = False) -> None:
        self.net_installed += 1
        self.background_writes = background_writes

    def checked_click(
        self, name: str, role: str, vet: Callable[[ControlInfo], None], nth: int | None = None
    ) -> None:
        vet(self.controls.get(name, _PLAIN))
        self.nths.append(nth)
        self.calls.append(("click", role, name))

    def close(self) -> None:
        self.calls.append(("close",))

    def describe_field(self, label: str) -> ControlInfo:
        return self.fields.get(label, ControlInfo(tag="input", input_type="text", in_form=True))

    def fill(self, label: str, value: str, target: object = None) -> str | None:
        self.calls.append(("fill", label, value))
        return value

    def frame_aria_snapshot(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> str:
        self.calls.append(("frame-aria", index, is_allowed("a.example.com")))
        return '- tab "Transcript"'

    def frame_list(
        self, is_allowed: Callable[[str], bool], *, wait_s: float = 0.0
    ) -> tuple[FrameInfo, ...]:
        self.calls.append(("frames", is_allowed("a.example.com")))
        return (
            FrameInfo(allowed=True, host="a.example.com", index=1, origin="https://a.example.com"),
        )

    def goto(self, url: str) -> None:
        self.calls.append(("goto", url))

    def raw_press(self, label: str, key: str) -> None:
        self.calls.append(("press", label, key))

    def read_frame_text(
        self,
        index: int,
        is_allowed: Callable[[str], bool],
        expect_host: str | None = None,
        *,
        wait_s: float = 0.0,
    ) -> PageText:
        self.calls.append(("frame-text", index, is_allowed("a.example.com")))
        return PageText(text="pane", injection_warning=None)

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

    def select(
        self,
        label: str,
        option: str,
        target: object = None,
        vet: Any = None,
        vet_name: Any = None,
    ) -> str | None:
        self.calls.append(("select", label, option))
        if vet_name is not None and self.select_field_name is not None:
            vet_name(self.select_field_name)
        if self.select_probe is not None and vet is not None:
            vet(self.select_probe)
        return option

    def upload(self, label: str, path: Path, target: object = None) -> None:
        self.calls.append(("upload", label, str(path)))


def _guard(
    *allowed: str, deny: tuple[str, ...] = (), confirm: tuple[str, ...] = ()
) -> tuple[_Raw, GuardedDriver]:
    raw = _Raw()
    policy = GuardPolicy(
        allowed_controls=frozenset(allowed),
        dialog_confirm=frozenset(confirm),
        extra_deny_names=deny,
    )
    return raw, GuardedDriver(raw, policy)


def test_building_a_guarded_driver_installs_the_network_safety_net_once() -> None:
    raw, _ = _guard()
    assert raw.net_installed == 1


def test_a_declared_ordinary_button_is_clicked() -> None:
    raw, driver = _guard("Add line")
    driver.click("Add line")
    assert raw.calls == [("click", "button", "Add line")]


def test_nth_reaches_the_lookup_and_the_click() -> None:
    raw, driver = _guard("More actions")
    driver.click("More actions", "button", nth=3)
    assert raw.nths == [3, 3]
    assert raw.calls == [("click", "button", "More actions")]


def test_nth_never_widens_what_a_task_may_click() -> None:
    """The name must still be declared, and a finalizing name is still refused (#573)."""
    raw, driver = _guard("Add line")
    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.click("More actions", "button", nth=2)
    raw2, driver2 = _guard("Submit report")
    with pytest.raises(SubmitBlockedError, match="finalizing"):
        driver2.click("Submit report", "button", nth=1)
    assert raw.calls == [] and raw2.calls == []


def test_nth_without_a_role_still_asks_for_one_when_the_name_is_in_two_roles() -> None:
    raw, driver = _guard("More actions")
    raw.present["More actions"] = ("button", "link")
    with pytest.raises(BrowserActionError, match="pass --role"):
        driver.click("More actions", nth=2)
    assert raw.calls == []


def test_nth_must_be_a_positive_whole_number() -> None:
    _, driver = _guard("More actions")
    for bad in (0, -1, True, "2", 1.5):
        with pytest.raises(ValueError, match="nth"):
            driver.click("More actions", "button", nth=bad)  # type: ignore[arg-type]


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
        "check_click",
        "click",
        "download",
        "close",
        "fill",
        "frame_aria_snapshot",
        "frame_list",
        "goto",
        "press",
        "read_frame_text",
        "read_text",
        "scroll_frame_text",
        "select",
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
        def block_form_submission(self, *, background_writes: bool = False) -> None:
            raise BrowserActionError("Expected the safety net, got an error")

    raw = _NetFails()
    with pytest.raises(BrowserActionError):
        attach_guarded(
            BrowserProfile(task="x", path=Path("/nonexistent")),
            GuardPolicy(),
            attach_raw=lambda profile, **kwargs: raw,
        )
    assert ("close",) in raw.calls


def test_frame_reads_pass_through_the_guard_with_the_hosts_check() -> None:
    raw, driver = _guard()
    assert driver.frame_list(lambda host: True)[0].host == "a.example.com"
    assert driver.read_frame_text(1, lambda host: True).text == "pane"
    assert driver.frame_aria_snapshot(1, lambda host: False) == '- tab "Transcript"'
    assert driver.aria_snapshot() == '- button "Go"'
    assert raw.calls == [
        ("frames", True),
        ("frame-text", 1, True),
        ("frame-aria", 1, False),
        ("aria",),
    ]


def test_click_without_a_role_finds_the_one_role_and_reports_it() -> None:
    raw, driver = _guard("Notes")
    raw.present["Notes"] = ("tab",)
    assert driver.click("Notes") == "tab"
    assert ("click", "tab", "Notes") in raw.calls


def test_click_without_a_role_refuses_an_ambiguous_name_listing_the_roles() -> None:
    raw, driver = _guard("Notes")
    raw.present["Notes"] = ("link", "tab")
    with pytest.raises(BrowserActionError, match="link, tab"):
        driver.click("Notes")
    assert not any(call[0] == "click" for call in raw.calls)


def test_click_without_a_role_fails_when_no_role_has_the_control() -> None:
    raw, driver = _guard("Notes")
    raw.present["Notes"] = ()
    with pytest.raises(BrowserActionError, match="named 'Notes'"):
        driver.click("Notes", wait_s=2.0)
    assert [probe[0::2] for probe in raw.probes] == [("Notes", 2.0)]


def test_click_with_a_role_probes_only_that_role_and_fails_fast_when_it_is_absent() -> None:
    raw, driver = _guard("Notes")
    raw.present["Notes"] = ("tab",)
    with pytest.raises(BrowserActionError, match="link named 'Notes'"):
        driver.click("Notes", "link", wait_s=3.0)
    assert raw.probes == [("Notes", ("link",), 3.0)]
    assert not any(call[0] == "click" for call in raw.calls)


def test_the_policy_is_checked_before_the_page_is_probed() -> None:
    raw, driver = _guard("Notes")
    with pytest.raises(SubmitBlockedError):
        driver.click("Submit")
    with pytest.raises(SubmitBlockedError):
        driver.click("Other")
    assert raw.calls == []
    assert raw.probes == []


def test_check_click_applies_the_policy_without_touching_the_page() -> None:
    raw, driver = _guard("Notes")
    driver.check_click("Notes", "tab")
    with pytest.raises(SubmitBlockedError):
        driver.check_click("Other", "tab")
    with pytest.raises(SubmitBlockedError):
        driver.check_click("Notes", "heading")
    assert raw.calls == [] and raw.probes == []


def test_download_applies_the_click_policy_before_the_page_is_probed(tmp_path: Path) -> None:
    raw, driver = _guard("Download")
    for bad in ("Submit", "Other"):
        with pytest.raises(SubmitBlockedError):
            driver.download(bad, save_to=tmp_path)
    assert raw.calls == [] and raw.probes == []


def test_download_finds_the_control_by_name_and_returns_the_file(tmp_path: Path) -> None:
    raw, driver = _guard("Download")
    raw.present["Download"] = ("button",)
    got = driver.download("Download", save_to=tmp_path, wait_s=3.0)
    assert got.filename == "f.vtt"
    assert raw.probes[0][2] == 3.0
    assert ("download", "button", "Download", 30.0) in raw.calls


def test_download_refuses_a_control_that_submits(tmp_path: Path) -> None:
    raw, driver = _guard("Download")
    raw.controls["Download"] = ControlInfo(tag="button", input_type="submit", in_form=True)
    with pytest.raises(SubmitBlockedError):
        driver.download("Download", save_to=tmp_path)


def test_the_guard_asks_for_the_lenient_net_unless_the_policy_is_strict() -> None:
    raw = _Raw()
    GuardedDriver(raw, GuardPolicy())
    assert raw.background_writes is False
    strict = _Raw()
    GuardedDriver(strict, GuardPolicy(block_background_writes=True))
    assert strict.background_writes is True


def test_select_passes_through_and_returns_what_the_field_shows() -> None:
    raw, driver = _guard()

    assert driver.select("Type", "Taxi") == "Taxi"
    assert raw.calls == [("select", "Type", "Taxi")]


@pytest.mark.parametrize(
    ("label", "option"), [("Type", "Submit"), ("Pay now", "Card"), ("Action", "Confirm")]
)
def test_select_refuses_a_finalizing_label_or_option_before_the_page_is_touched(
    label: str, option: str
) -> None:
    raw, driver = _guard()

    with pytest.raises(SubmitBlockedError):
        driver.select(label, option)

    assert raw.calls == []


def test_a_bad_nth_or_a_host_without_a_frame_is_refused_before_the_page_is_touched() -> None:
    from blumkin.browser.cdp import FieldTarget

    raw, driver = _guard()

    for target in (FieldTarget(nth=0), FieldTarget(nth=True), FieldTarget(frame_host="a.example")):
        with pytest.raises(ValueError, match="Expected"):
            driver.fill("Date", "x", target)
        with pytest.raises(ValueError, match="Expected"):
            driver.select("Type", "Taxi", target)
        with pytest.raises(ValueError, match="Expected"):
            driver.upload("File", Path("/tmp/x"), target)

    assert raw.calls == []


def test_select_hands_the_driver_a_vet_that_refuses_a_submit_control() -> None:
    raw, driver = _guard()
    raw.select_probe = ControlInfo(tag="button", input_type="submit", in_form=True)

    with pytest.raises(SubmitBlockedError, match="submit control"):
        driver.select("Continue", "x")


def test_select_never_goes_through_a_clickable_role_that_is_not_a_dropdown() -> None:
    from blumkin.browser.cdp import FieldTarget

    raw, driver = _guard()

    with pytest.raises(SubmitBlockedError, match="not one a dropdown has"):
        driver.select("Save draft", "x", FieldTarget(role="button"))

    assert raw.calls == []


def test_a_role_that_is_not_a_field_role_is_refused_for_fill_and_upload_too() -> None:
    from blumkin.browser.cdp import FieldTarget

    raw, driver = _guard()

    with pytest.raises(ValueError, match="field role"):
        driver.fill("Date", "x", FieldTarget(role="textbx"))
    with pytest.raises(ValueError, match="field role"):
        driver.upload("File", Path("/tmp/x"), FieldTarget(role="button"))

    assert raw.calls == []


def test_select_shows_the_resolved_field_name_to_the_finalizing_check() -> None:
    raw, driver = _guard()
    raw.select_field_name = "Submit action"

    with pytest.raises(SubmitBlockedError, match="field's own name"):
        driver.select("Action", "Draft")


def test_a_harmless_resolved_field_name_passes_the_name_check() -> None:
    raw, driver = _guard()
    raw.select_field_name = "Expense type"

    assert driver.select("Type", "Taxi") == "Taxi"


# --------------------------------------------------------------------------- dialog confirm (#587)


def _ok_button(
    *,
    in_dialog: bool,
    form_in_dialog: bool,
    has_formaction: bool = False,
    inline_handler: str = "",
    dialog_controls: tuple[str, ...] = (),
    complete: bool = True,
    input_type: str = "submit",
) -> ControlInfo:
    return ControlInfo(
        tag="button",
        input_type=input_type,
        in_form=True,
        in_dialog=in_dialog,
        form_in_dialog=form_in_dialog,
        has_formaction=has_formaction,
        inline_handler=inline_handler,
        dialog_controls=dialog_controls,
        dialog_controls_complete=complete,
    )


def test_a_dialog_confirm_button_is_clicked_inside_a_dialog_with_its_own_form() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=True)

    driver.click("OK")

    assert raw.calls == [("click", "button", "OK")]


def test_a_dialog_confirm_button_outside_any_dialog_is_refused() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=False, form_in_dialog=False)

    with pytest.raises(SubmitBlockedError, match="not in a dialog"):
        driver.click("OK")

    assert raw.calls == []


def test_a_dialog_nested_in_the_page_form_is_not_a_safe_place_to_confirm() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=False)

    with pytest.raises(SubmitBlockedError, match="its form is not the dialog's"):
        driver.click("OK")

    assert raw.calls == []


def test_the_same_name_declared_only_as_a_plain_control_stays_refused_as_a_submit() -> None:
    raw, driver = _guard("OK")
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=True)

    with pytest.raises(SubmitBlockedError, match="submit control"):
        driver.click("OK")


def test_a_name_that_is_not_declared_at_all_is_still_refused() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["Close"] = _ok_button(in_dialog=True, form_in_dialog=True)

    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.click("Close")


@pytest.mark.parametrize(
    "extra",
    [{"has_formaction": True}, {"inline_handler": "this.form.submit()"}],
)
def test_a_dialog_confirm_is_still_refused_with_a_formaction_or_a_submitting_handler(
    extra: dict[str, Any],
) -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=True, **extra)

    with pytest.raises(SubmitBlockedError):
        driver.click("OK")

    assert raw.calls == []


@pytest.mark.parametrize("name", ["Submit", "Pay now", "Send", "Confirm payment", "File report"])
def test_a_finalizing_name_can_never_be_a_dialog_confirm(name: str) -> None:
    with pytest.raises(SubmitBlockedError, match="not finalizing"):
        _guard(confirm=(name,))


def test_a_task_extra_deny_name_cannot_be_a_dialog_confirm_either() -> None:
    with pytest.raises(SubmitBlockedError, match="not finalizing"):
        _guard(confirm=("Release",), deny=("release",))


def test_a_dialog_confirm_never_unlocks_the_report_own_submit_button() -> None:
    raw, driver = _guard("Add line", confirm=("OK",))
    raw.controls["Submit"] = _ok_button(in_dialog=True, form_in_dialog=True)

    with pytest.raises(SubmitBlockedError):
        driver.click("Submit")

    assert raw.calls == []


def test_a_modal_wrapped_around_the_whole_report_does_not_qualify() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(
        in_dialog=True, form_in_dialog=True, dialog_controls=("Add line", "OK", "Submit report")
    )

    with pytest.raises(SubmitBlockedError, match="finalizing control"):
        driver.click("OK")

    assert raw.calls == []


def test_a_small_dialog_with_its_own_buttons_qualifies() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(
        in_dialog=True, form_in_dialog=True, dialog_controls=("Add", "Cancel", "OK")
    )

    driver.click("OK")

    assert raw.calls == [("click", "button", "OK")]


def test_a_confirm_only_name_cannot_start_a_download(tmp_path: Path) -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = ControlInfo(
        tag="button", input_type="button", in_form=False, in_dialog=True, form_in_dialog=True
    )

    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.download("OK", save_to=tmp_path)

    assert raw.calls == []


def test_check_click_accepts_a_confirm_name_but_not_when_a_download_asks() -> None:
    _, driver = _guard(confirm=("OK",))

    driver.check_click("OK")
    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.check_click("OK", confirm_ok=False)


def test_a_name_in_both_lists_is_spared_in_a_dialog_and_a_plain_control_elsewhere() -> None:
    raw, driver = _guard("OK", confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=True)
    driver.click("OK")
    raw.controls["OK"] = _ok_button(in_dialog=False, form_in_dialog=False)

    with pytest.raises(SubmitBlockedError, match="submit control"):
        driver.click("OK")


def test_a_submit_input_in_a_dialog_is_spared_like_a_submit_button() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = ControlInfo(
        tag="input", input_type="submit", in_form=True, in_dialog=True, form_in_dialog=True
    )

    driver.click("OK")

    assert raw.calls == [("click", "button", "OK")]


def test_nth_picks_a_match_but_each_match_is_vetted_on_its_own() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=False, form_in_dialog=False)

    with pytest.raises(SubmitBlockedError, match="not in a dialog"):
        driver.click("OK", nth=2)

    assert not any(call[0] == "click" for call in raw.calls)


# --------------------------------------------------------------------------- review (#587)


def test_a_confirm_only_ok_that_is_a_plain_button_still_needs_the_dialogs_own_form() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=False, input_type="button")

    with pytest.raises(SubmitBlockedError, match="its form is not the dialog's"):
        driver.click("OK")

    assert raw.calls == []


def test_a_name_that_is_also_a_plain_control_keeps_the_ordinary_rules_for_a_plain_button() -> None:
    raw, driver = _guard("OK", confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=False, input_type="button")

    driver.click("OK")

    assert raw.calls == [("click", "button", "OK")]


def test_a_dialog_with_more_controls_than_could_be_collected_does_not_qualify() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(
        in_dialog=True, form_in_dialog=True, dialog_controls=("Add", "OK"), complete=False
    )

    with pytest.raises(SubmitBlockedError, match="more controls than can be checked"):
        driver.click("OK")

    assert raw.calls == []


@pytest.mark.parametrize(
    "names",
    [
        ("OK", "Submit report"),
        ("OK", "\u2713", "Submit"),  # an icon button whose aria-label is the finalizing word
        ("OK", "pay-button"),  # an id or name that says what the button does
        ("OK", "Cancel", "Send for approval"),
    ],
)
def test_any_name_a_dialog_control_goes_by_that_is_finalizing_disqualifies_the_dialog(
    names: tuple[str, ...],
) -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(in_dialog=True, form_in_dialog=True, dialog_controls=names)

    with pytest.raises(SubmitBlockedError, match="finalizing control"):
        driver.click("OK")


def test_an_icon_only_control_with_no_finalizing_name_does_not_disqualify_the_dialog() -> None:
    raw, driver = _guard(confirm=("OK",))
    raw.controls["OK"] = _ok_button(
        in_dialog=True, form_in_dialog=True, dialog_controls=("OK", "\u00d7", "Close", "close-x")
    )

    driver.click("OK")

    assert raw.calls == [("click", "button", "OK")]


# --------------------------------------------------------------------------- patterns (#588)


@pytest.mark.parametrize(
    "name",
    ["Actions for Trip to Lisbon Report", "Actions for Q3 Offsite Report", "Actions for A Report"],
)
def test_a_declared_pattern_matches_any_trip_name(name: str) -> None:
    raw, driver = _guard("Actions for * Report")

    driver.click(name)

    assert [call[2] for call in raw.calls] == [name]


def test_a_pattern_matches_the_whole_name_and_keeps_its_fixed_text_and_case() -> None:
    _, driver = _guard("Actions for * Report")

    for bad in (
        "Actions for Trip",
        "actions for Trip Report",
        "Trip Actions for X Report",
        "Actions for Report",
        "Open",
    ):
        with pytest.raises(SubmitBlockedError, match="not declared"):
            driver.click(bad)


def test_pattern_characters_other_than_the_star_are_literal() -> None:
    raw, driver = _guard("Row (1) *")

    driver.click("Row (1) details")
    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.click("Row 1 details")


def test_a_pattern_never_reaches_a_finalizing_name() -> None:
    raw, driver = _guard("Actions for *")

    for name in ("Actions for Submit Report", "Actions for Payment Report"):
        with pytest.raises(SubmitBlockedError, match="finalizing"):
            driver.click(name)

    assert raw.calls == []


def test_menu_items_stay_individually_declared() -> None:
    raw, driver = _guard("Actions for * Report", "Open")

    driver.click("Open")
    for item in ("Submit", "Delete", "Duplicate"):
        with pytest.raises(SubmitBlockedError):
            driver.click(item)


def test_a_matched_control_is_still_vetted_like_any_other() -> None:
    raw, driver = _guard("Actions for * Report")
    raw.controls["Actions for Trip Report"] = ControlInfo(
        tag="button", input_type="submit", in_form=True
    )

    with pytest.raises(SubmitBlockedError, match="submit control"):
        driver.click("Actions for Trip Report")

    assert raw.calls == []


@pytest.mark.parametrize(
    "pattern",
    ["*", "**", "a*", "* *", "Submit *", "* Pay", "Send * now", "*Report", "* Report"],
)
def test_a_pattern_that_is_too_loose_or_finalizing_is_refused_up_front(pattern: str) -> None:
    with pytest.raises(SubmitBlockedError, match="control pattern"):
        _guard(pattern)


def test_a_task_extra_deny_word_cannot_hide_in_a_pattern() -> None:
    with pytest.raises(SubmitBlockedError, match="finalizing"):
        _guard("Release *", deny=("release",))


def test_the_refusal_lists_what_the_task_declares() -> None:
    _, driver = _guard("Add line", "Actions for * Report")

    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Delete everything")

    message = str(excinfo.value)
    assert "'Add line'" in message
    assert "'Actions for * Report'" in message
    assert "`*` stands for any text" in message


def test_the_refusal_says_so_when_nothing_is_declared() -> None:
    _, driver = _guard()

    with pytest.raises(SubmitBlockedError, match="declares no controls"):
        driver.click("Anything")


def test_a_long_declared_list_is_cut_short_in_the_refusal() -> None:
    names = tuple(f"Control {number:02d}" for number in range(15))
    _, driver = _guard(*names)

    with pytest.raises(SubmitBlockedError, match="and 5 more"):
        driver.click("Other")


def test_a_pattern_does_not_make_a_download_or_a_confirm_easier() -> None:
    raw, driver = _guard("Actions for * Report", confirm=("OK",))
    raw.controls["OK"] = ControlInfo(tag="button", input_type="button", in_form=False)

    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.download("OK", save_to=Path("/tmp"))


def test_a_pattern_may_hold_at_most_two_stars() -> None:
    _guard("Actions for * Report *")
    with pytest.raises(SubmitBlockedError, match="at most 2"):
        _guard("Actions * for * Report *")


def test_a_pattern_with_many_stars_cannot_hang_on_a_long_hostile_name() -> None:
    import time

    _, driver = _guard("aaaa*a*a")
    started = time.monotonic()

    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.click("a" * 5000 + "b")

    assert time.monotonic() - started < 1.0


def test_consecutive_stars_count_as_one() -> None:
    raw, driver = _guard("Actions for ** Report")

    driver.click("Actions for Trip Report")

    assert [call[2] for call in raw.calls] == ["Actions for Trip Report"]


def test_a_name_longer_than_the_cap_is_never_matched_by_a_pattern() -> None:
    _, driver = _guard("Actions for * Report")

    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.click("Actions for " + "x" * 400 + " Report")


def test_a_fullwidth_star_is_not_a_wildcard() -> None:
    raw, driver = _guard("Actions for \uff0a Report")

    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.click("Actions for Trip Report")


def test_dialog_confirm_names_are_listed_apart_and_a_star_in_one_is_literal() -> None:
    raw, driver = _guard("Add line", confirm=("OK *",))

    with pytest.raises(SubmitBlockedError) as excinfo:
        driver.click("Other")

    message = str(excinfo.value)
    assert "dialog confirms: 'OK *'" in message
    assert "a `*` stands for any text" not in message
    with pytest.raises(SubmitBlockedError, match="not declared"):
        driver.click("OK whatever")


def test_the_star_note_appears_only_when_a_pattern_is_declared() -> None:
    _, plain = _guard("Add line")
    _, patterned = _guard("Actions for * Report")

    with pytest.raises(SubmitBlockedError) as plain_error:
        plain.click("Other")
    with pytest.raises(SubmitBlockedError) as patterned_error:
        patterned.click("Other")

    assert "stands for any text" not in str(plain_error.value)
    assert "stands for any text" in str(patterned_error.value)


def test_a_pattern_can_have_a_leading_and_trailing_fixed_part() -> None:
    raw, driver = _guard("Actions for * Report (draft)")

    driver.click("Actions for Trip Report (draft)")

    assert [call[2] for call in raw.calls] == ["Actions for Trip Report (draft)"]
