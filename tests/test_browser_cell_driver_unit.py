"""The real `PlaywrightDriver.edit_cell` path, driven against a fake page (no browser).

The guard and CLI tests replace `edit_cell` with a fake; these run the driver's own code for a
data-grid cell: what is clicked, what is typed, how a dropdown option is chosen and what
happens when the page does not behave.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from blumkin.browser import cdp
from blumkin.browser.cdp import BrowserActionError, ControlInfo, PlaywrightDriver

_CELL_INFO = {
    "tag": "div",
    "type": "",
    "form": False,
    "formaction": False,
    "href": "",
    "onclick": "",
}


class _Keyboard:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self._events = events

    def press(self, key: str) -> None:
        self._events.append(("press", key))

    def type(self, text: str) -> None:
        self._events.append(("type", text))


class _OptionHandle:
    """One dropdown option element: read, described and clicked through the same handle."""

    def __init__(self, page: _Page, index: int) -> None:
        self._page = page
        self._index = index

    def click(self, *, timeout: float) -> None:
        self._page.events.append(("option-click", str(self._index)))

    def evaluate(self, script: str, arg: Any = None) -> Any:
        assert script is cdp._DESCRIBE_CONTROL_JS
        self._page.events.append(("option-describe", str(self._index)))
        return self._page.option_info

    def inner_text(self) -> str:
        texts = self._page.current_options()
        return self._page.row_override.get(self._index, texts[self._index])


class _Row:
    def __init__(self, page: _Page, index: int) -> None:
        self._page = page
        self._index = index

    def element_handle(self, *, timeout: float) -> _OptionHandle:
        self._page.option_handles += 1
        return _OptionHandle(self._page, self._index)


class _Rows:
    def __init__(self, page: _Page) -> None:
        self._page = page

    def all_inner_texts(self) -> list[str]:
        return list(self._page.poll_options())

    def nth(self, index: int) -> _Row:
        return _Row(self._page, index)


class _Drop:
    def __init__(self, page: _Page) -> None:
        self._page = page

    @property
    def first(self) -> _Drop:
        return self

    def wait_for(self, *, state: str, timeout: float) -> None:
        if not self._page.drop_visible:
            raise PlaywrightTimeout("no dropdown")

    def count(self) -> int:
        return self._page.drop_count

    def locator(self, selector: str) -> _Rows:
        assert selector == "li[role=row]"
        return _Rows(self._page)


class _Handle:
    def __init__(self, page: _Page) -> None:
        self._page = page

    def scroll_into_view_if_needed(self, *, timeout: float) -> None:
        self._page.events.append(("scroll", ""))

    def click(self, *, timeout: float) -> None:
        self._page.events.append(("click", ""))

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is cdp._DESCRIBE_CONTROL_JS:
            return self._page.cell_info
        if script is cdp._CELL_CLICK_TARGET_JS:
            return self._page.click_target
        if script is cdp._CELL_TEXT_JS:
            return self._page.cell_text
        raise AssertionError(f"unexpected handle script: {script[:40]!r}")


class _Found:
    def __init__(self, page: _Page, error: str) -> None:
        self._page = page
        self._error = error

    def evaluate(self, script: str) -> Any:
        if script == "o => o.error || ''":
            return self._error
        assert script == "o => o.meta"
        return {
            "column": 5,
            "columnLabel": "Mon,Nov 02",
            "columns": 17,
            "row": 2,
            "rowLabel": "2",
            "rows": 12,
        }

    def get_property(self, name: str) -> Any:
        assert name == "element"
        return SimpleNamespace(as_element=lambda: _Handle(self._page))


class _Page:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.keyboard = _Keyboard(self.events)
        self.context = SimpleNamespace()
        self.cell_info: dict[str, Any] = dict(_CELL_INFO)
        #: What the dropdown option element reports when it is described.
        self.option_info: dict[str, Any] = {**_CELL_INFO, "tag": "li"}
        self.cell_text = "8"
        self.click_target = "ok"
        #: Successive answers to the focus question; the last one repeats.
        self.focus: list[str] = ["cell", "editor"]
        #: Successive answers to "is an editor still open?"; the last one repeats.
        self.editing: list[bool] = [False]
        self.drop_visible = False
        self.drop_count = 1
        #: Successive polls of the dropdown rows; the last one repeats.
        self.options: list[list[str]] = [[]]
        self.row_override: dict[int, str] = {}
        self.finds = 0
        #: How many option handles were taken from the dropdown rows.
        self.option_handles = 0
        #: The error `_find_cell` reports from this call on (1-based), if any.
        self.find_error_from: int | None = None
        self.waits = 0
        #: Whether a Delete left an editor open behind the cell that can be given focus.
        self.editor_behind_cell = False
        self.focused_editors = 0
        self._polls = 0

    def current_options(self) -> list[str]:
        return self.options[min(self._polls, len(self.options)) - 1]

    def poll_options(self) -> list[str]:
        self._polls += 1
        return self.options[min(self._polls, len(self.options)) - 1]

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is cdp._CELL_FOCUS_JS:
            value = self.focus.pop(0) if len(self.focus) > 1 else self.focus[0]
            self.events.append(("focus", value))
            return value
        if script is cdp._CELL_EDITING_JS:
            return self.editing.pop(0) if len(self.editing) > 1 else self.editing[0]
        if script is cdp._CELL_FOCUS_EDITOR_JS:
            if self.editor_behind_cell:
                self.focused_editors += 1
            return self.editor_behind_cell
        raise AssertionError(f"unexpected page script: {script[:40]!r}")

    def evaluate_handle(self, script: str, arg: Any = None) -> _Found:
        assert script is cdp._CELL_JS
        self.finds += 1
        broken = self.find_error_from is not None and self.finds >= self.find_error_from
        return _Found(self, "Expected one cell at row 2, column 5, got 0" if broken else "")

    def locator(self, selector: str) -> _Drop:
        assert selector == ".oj-listbox-drop:visible"
        return _Drop(self)

    def wait_for_timeout(self, ms: float) -> None:
        self.waits += 1


def _driver(page: _Page) -> PlaywrightDriver:
    return PlaywrightDriver(
        browser=SimpleNamespace(), page=page, playwright=SimpleNamespace(), timeout_s=1
    )


def _refuse_name(name: str) -> None:
    raise BrowserActionError(f"refused {name!r}")


def _edit(
    page: _Page,
    value: str | None,
    vetted: list[ControlInfo] | None = None,
    names: list[str] | None = None,
    refuse_names: tuple[str, ...] = (),
) -> Any:
    seen = vetted if vetted is not None else []
    named = names if names is not None else []

    def vet_name(name: str) -> None:
        named.append(name)
        if name in refuse_names:
            _refuse_name(name)

    return _driver(page).edit_cell("2", "5", value, 1, seen.append, vet_name)


def _kinds(page: _Page) -> list[str]:
    return [kind for kind, _ in page.events]


def test_a_read_clicks_nothing_and_types_nothing() -> None:
    page = _Page()
    edit = _edit(page, None)
    assert (edit.edited, edit.text) == (False, "8")
    assert page.events == []


def test_the_cell_is_vetted_as_the_element_that_is_clicked() -> None:
    page = _Page()
    page.cell_info = {**_CELL_INFO, "tag": "input", "type": "submit"}
    seen: list[ControlInfo] = []
    page.focus = ["cell", "editor"]
    _edit(page, "8", seen)
    assert [(info.tag, info.input_type) for info in seen] == [("input", "submit")]


def test_a_plain_cell_is_typed_in_and_committed_with_tab() -> None:
    page = _Page()
    edit = _edit(page, "8")
    assert edit.edited is True
    typed = [(k, v) for k, v in page.events if k in ("type", "press", "click")]
    assert typed == [("click", ""), ("type", "8"), ("press", "Tab")]


def test_only_the_first_key_is_typed_before_an_editor_is_confirmed() -> None:
    page = _Page()
    _edit(page, "8.5")
    typed = [(k, v) for k, v in page.events if k == "type"]
    assert typed == [("type", "8"), ("type", ".5")]
    order = _kinds(page)
    assert order.index("focus") < order.index("type")


@pytest.mark.parametrize("focus", ["outside", "other"])
def test_nothing_is_typed_when_focus_is_not_on_the_cell_or_an_editor(focus: str) -> None:
    page = _Page()
    page.focus = [focus]
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "Paid Time Off")
    assert "nothing was typed" in str(excinfo.value)
    assert "type" not in _kinds(page)
    assert "press" not in _kinds(page)


def test_a_space_is_never_typed_when_the_first_key_opens_no_editor() -> None:
    page = _Page()
    page.focus = ["cell", "other"]
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "Paid Time Off")
    assert "open an editor" in str(excinfo.value)
    assert [v for k, v in page.events if k == "type"] == ["P"]


@pytest.mark.parametrize(
    "target", ["a control inside the cell", "another element on top of the cell"]
)
def test_nothing_is_clicked_when_a_control_or_a_cover_is_at_the_click_point(target: str) -> None:
    page = _Page()
    page.click_target = target
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "8")
    assert "nothing was clicked" in str(excinfo.value)
    assert page.events == [("scroll", "")]


def test_an_empty_value_clears_the_cell_with_delete_and_tab() -> None:
    page = _Page()
    page.focus = ["cell"]
    _edit(page, "")
    assert [(k, v) for k, v in page.events if k in ("type", "press")] == [
        ("press", "Delete"),
        ("press", "Tab"),
    ]


def test_a_dropdown_option_is_clicked_by_its_position_not_by_its_text() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Paid Time Off - Holiday", "Paid Time Off", "Paid Time Off Extra"]]
    _edit(page, "Paid Time Off")
    assert ("option-click", "1") in page.events
    order = [(k, v) for k, v in page.events if k in ("option-click", "press")]
    assert order == [("option-click", "1"), ("press", "Tab")]


def test_a_prefix_is_enough_only_when_exactly_one_option_starts_with_it() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["INDADM1000001083 - Non-Specific Project Work"]]
    _edit(page, "INDADM1000001083")
    assert ("option-click", "0") in page.events


def test_the_same_list_must_be_seen_twice_before_an_option_is_clicked() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Project A-Internal"], ["Project A", "Project A-Internal"]]
    _edit(page, "Project A")
    assert ("option-click", "0") in page.events
    assert page.waits >= 2


def test_an_option_that_changed_before_the_click_is_not_clicked() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Paid Time Off"]]
    page.row_override = {0: "Something else"}
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "Paid Time Off")
    assert "dropdown option" in str(excinfo.value)
    assert not any(kind == "option-click" for kind in _kinds(page))


def test_no_matching_option_is_refused_with_the_options_listed_and_no_enter() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Regular Pay\x1b[31m", "Holiday"]]
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "Paid Time Off")
    message = str(excinfo.value)
    assert "got 2" in message and "Regular Pay" in message
    assert "\x1b" not in message
    assert ("press", "Enter") not in page.events
    assert ("press", "Tab") not in page.events


def test_two_visible_dropdown_lists_are_refused() -> None:
    page = _Page()
    page.drop_visible = True
    page.drop_count = 2
    page.options = [["Paid Time Off"]]
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "Paid Time Off")
    assert "got 2" in str(excinfo.value)
    assert not any(kind == "option-click" for kind in _kinds(page))


def test_a_value_with_nothing_to_match_on_is_committed_with_tab_not_matched_to_an_option() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Only option"]]
    _edit(page, "-")
    assert ("press", "Tab") in page.events
    assert not any(kind == "option-click" for kind in _kinds(page))


def test_an_editor_that_stays_open_cancels_the_edit_and_says_so() -> None:
    page = _Page()
    page.editing = [True]
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "8")
    assert "still open" in str(excinfo.value)
    assert ("press", "Escape") in page.events


def test_a_failed_read_after_the_edit_says_the_edit_was_made() -> None:
    page = _Page()
    page.find_error_from = 2
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "8")
    assert "the edit itself was made" in str(excinfo.value)


@pytest.mark.parametrize("value", [None, "8", "", "Paid Time Off", "-"])
def test_enter_is_never_pressed(value: str | None) -> None:
    page = _Page()
    page.drop_visible = value == "Paid Time Off"
    page.options = [["Paid Time Off"]]
    page.focus = ["cell", "editor"] if value else ["cell"]
    _edit(page, value)
    assert ("press", "Enter") not in page.events


def test_the_chosen_dropdown_option_is_vetted_by_element_and_by_text_before_the_click() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Paid Time Off"]]
    seen: list[ControlInfo] = []
    names: list[str] = []
    _edit(page, "Paid Time Off", seen, names)
    assert names == ["Paid Time Off"]
    assert [info.tag for info in seen] == ["div", "li"]
    assert ("option-click", "0") in page.events


def test_an_option_named_like_a_finalizing_action_is_not_clicked() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Submit"]]
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "Sub", refuse_names=("Submit",))
    assert "refused 'Submit'" in str(excinfo.value)
    assert not any(kind == "option-click" for kind in _kinds(page))


def test_an_option_that_is_a_submit_control_is_not_clicked() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Paid Time Off"]]
    page.option_info = {**_CELL_INFO, "tag": "input", "type": "submit"}

    def refuse_submit(info: ControlInfo) -> None:
        if info.input_type == "submit":
            raise BrowserActionError("a submit control")

    with pytest.raises(BrowserActionError) as excinfo:
        _driver(page).edit_cell("2", "5", "Paid Time Off", 1, refuse_submit, lambda name: None)
    assert "a submit control" in str(excinfo.value)
    assert not any(kind == "option-click" for kind in _kinds(page))


def test_the_option_is_read_vetted_and_clicked_through_one_handle() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["Paid Time Off"]]
    _edit(page, "Paid Time Off")
    assert page.option_handles == 1
    steps = [(k, v) for k, v in page.events if k in ("option-describe", "option-click")]
    assert steps == [("option-describe", "0"), ("option-click", "0")]


def test_an_editor_left_open_is_emptied_before_a_retry_types_into_it() -> None:
    page = _Page()
    page.focus = ["editor", "editor"]
    _edit(page, "8")
    keys = [(k, v) for k, v in page.events if k in ("press", "type")]
    assert keys[0] == ("press", "ControlOrMeta+A")
    assert keys[1] == ("type", "8")


def test_a_cell_that_is_not_in_an_editor_is_not_emptied_first() -> None:
    page = _Page()
    _edit(page, "8")
    assert ("press", "ControlOrMeta+A") not in page.events


def test_clearing_an_editor_that_stays_open_cancels_and_says_so() -> None:
    page = _Page()
    page.focus = ["editor"]
    page.editing = [True]
    with pytest.raises(BrowserActionError) as excinfo:
        _edit(page, "")
    assert "still open" in str(excinfo.value)
    assert ("press", "Escape") in page.events


def test_clearing_selects_an_open_editor_before_delete() -> None:
    page = _Page()
    page.focus = ["editor"]
    _edit(page, "")
    keys = [v for k, v in page.events if k == "press"]
    assert keys[:3] == ["ControlOrMeta+A", "Delete", "Tab"]


def test_a_non_ascii_value_is_matched_to_its_option_not_committed_blind() -> None:
    page = _Page()
    page.drop_visible = True
    page.options = [["休暇", "休暇申請"]]
    _edit(page, "休暇")
    assert ("option-click", "0") in page.events


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [("休暇", "休暇", True), ("项目", "工时", False), ("A-B", "ab", True), ("-", "", True)],
)
def test_option_keys_keep_letters_of_any_script_and_drop_punctuation(
    a: str, b: str, same: bool
) -> None:
    assert (cdp._option_key(a) == cdp._option_key(b)) is same
    assert cdp._option_key("休暇") != ""


def test_the_page_script_keeps_unicode_letters_and_digits_when_comparing_headers() -> None:
    assert "[^\\p{L}\\p{N}]" in cdp._CELL_JS


def test_clearing_focuses_an_editor_that_delete_opened_behind_the_cell() -> None:
    page = _Page()
    page.focus = ["cell", "cell", "editor"]
    page.editor_behind_cell = True
    _edit(page, "")
    keys = [v for k, v in page.events if k == "press"]
    assert keys == ["Delete", "ControlOrMeta+A", "Delete", "Tab"]
    assert page.focused_editors == 1


def test_clearing_presses_tab_when_delete_opens_no_editor() -> None:
    page = _Page()
    page.focus = ["cell"]
    _edit(page, "")
    assert [v for k, v in page.events if k == "press"] == ["Delete", "Tab"]
    assert page.focused_editors == 0


def test_an_editor_tab_left_open_is_committed_by_selecting_the_next_cell() -> None:
    page = _Page()
    page.editing = [True, True, True, True, False]
    vetted: list[ControlInfo] = []
    _edit(page, "8", vetted)
    assert [k for k, _ in page.events if k == "click"] == ["click", "click"]
    assert ("press", "Escape") not in page.events
    assert len(vetted) == 2


def test_an_editor_stays_cancelled_when_there_is_no_next_cell_to_select() -> None:
    page = _Page()
    page.editing = [True]
    page.find_error_from = 2
    with pytest.raises(BrowserActionError):
        _edit(page, "8")
    assert [k for k, _ in page.events if k == "click"] == ["click"]
    assert ("press", "Escape") in page.events
