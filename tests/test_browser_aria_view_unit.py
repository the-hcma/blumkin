"""Scoping, collapsing and paging an ARIA snapshot for `browser read` (issue #606)."""

from __future__ import annotations

import pytest

from blumkin.browser.aria_view import collapse_options, parse_within, scope, window

_PAGE = """\
- heading "Expenses"
- table "Items":
  - row "Lodging 120.00":
    - combobox "Expense Type":
      - option "Lodging" [selected]
      - option "Meals"
      - option "Travel"
    - button "Edit"
- dialog "Manage Attachments":
  - table "Rows":
    - row "r1":
      - combobox "Category":
        - option "Receipt"
        - option "Invoice" [selected]
  - button "OK"
  - button "Cancel"
- button "Save"
"""


def test_parse_within_takes_a_role_alone_or_with_a_name() -> None:
    assert parse_within("dialog") == ("dialog", None)
    assert parse_within("dialog:Manage Attachments") == ("dialog", "Manage Attachments")
    assert parse_within(" Dialog : A:B ") == ("dialog", "A:B")


@pytest.mark.parametrize("spec", ["", ":", ":name", "two words", "dia log:x"])
def test_parse_within_refuses_a_spec_without_a_role(spec: str) -> None:
    with pytest.raises(ValueError, match="ROLE"):
        parse_within(spec)


def test_scope_keeps_one_subtree_and_nothing_else() -> None:
    text, matches = scope(_PAGE, "dialog", "Manage Attachments")

    assert matches == 1

    assert text.splitlines()[0] == '- dialog "Manage Attachments":'
    assert '- button "OK"' in text
    assert "Expenses" not in text and '- button "Save"' not in text
    assert 'button "Edit"' not in text


def test_scope_by_role_alone_takes_the_first_match() -> None:
    text, matches = scope(_PAGE, "table")

    assert text.splitlines()[0] == '- table "Items":'
    assert matches == 2


def test_scope_that_finds_nothing_names_what_is_there() -> None:
    with pytest.raises(LookupError) as caught:
        scope(_PAGE, "dialog", "Nope")

    message = str(caught.value)
    assert "Expected a dialog named 'Nope'" in message
    assert "'Manage Attachments'" in message


def test_scope_of_a_role_nothing_has_says_so() -> None:
    with pytest.raises(LookupError, match="got none"):
        scope(_PAGE, "tree")


def test_scope_is_not_fooled_by_a_name_that_only_contains_the_asked_one() -> None:
    with pytest.raises(LookupError):
        scope(_PAGE, "dialog", "Manage")


def test_collapse_replaces_each_option_list_by_a_count_and_the_selection() -> None:
    text = collapse_options(_PAGE)

    assert '- combobox "Expense Type" (3 options, selected "Lodging")' in text
    assert '- combobox "Category" (2 options, selected "Invoice")' in text
    assert "- option" not in text
    assert '- button "Edit"' in text and '- button "OK"' in text


def test_collapse_says_when_nothing_is_selected() -> None:
    text = collapse_options('- combobox "Type":\n  - option "A"\n  - option "B"\n')

    assert text.strip() == '- combobox "Type" (2 options, none selected)'


def test_collapse_leaves_a_control_with_other_children_alone() -> None:
    page = '- listbox "L":\n  - option "A"\n  - group "G":\n    - option "B"\n'

    assert collapse_options(page) == page


def test_window_returns_the_slice_and_where_the_next_one_starts() -> None:
    text = "a" * 25

    first = window(text, 0, 10)
    last = window(text, 20, 10)

    assert first == ("a" * 10, True, 10)
    assert last == ("a" * 5, False, None)


def test_window_past_the_end_is_empty_not_an_error() -> None:
    assert window("abc", 99, 10) == ("", False, None)


@pytest.mark.parametrize(("offset", "limit"), [(-1, 10), (0, 0), (0, -5)])
def test_window_refuses_a_negative_offset_or_a_limit_below_one(offset: int, limit: int) -> None:
    with pytest.raises(ValueError, match="positive|negative|at least"):
        window("abc", offset, limit)


def test_a_node_whose_name_needs_yaml_quoting_is_found_and_collapsed() -> None:
    page = (
        "- 'dialog \"Confirm: delete\":':\n"
        '  - button "OK"\n'
        "- 'combobox \"Type: kind\":':\n"
        '  - option "A"\n'
        '  - option "B" [selected]\n'
        "  - 'option \"C: d\"'\n"
    )

    text, _ = scope(page, "dialog", "Confirm: delete")
    collapsed = collapse_options(page)

    assert '- button "OK"' in text
    assert '(3 options, selected "B")' in collapsed
    assert "option" not in collapsed.replace("options", "")


def test_a_line_separator_inside_a_name_does_not_forge_a_node() -> None:
    page = (
        '- button "x\u2028- dialog \\"Evil\\":\u2028  - button \\"OK\\""\n'
        '- dialog "Real":\n'
        '  - button "Save"\n'
    )

    with pytest.raises(LookupError):
        scope(page, "dialog", "Evil")
    text, _ = scope(page, "dialog", "Real")
    assert text == '- dialog "Real":\n  - button "Save"\n'
    assert "\u2028" in collapse_options(page)


def test_scope_counts_every_match_and_keeps_the_first() -> None:
    page = '- dialog "A":\n  - button "1"\n- dialog "B":\n  - button "2"\n'

    text, matches = scope(page, "dialog")

    assert matches == 2
    assert 'button "1"' in text and 'button "2"' not in text


def test_a_single_option_reads_as_one_option() -> None:
    assert "(1 option, none selected)" in collapse_options('- combobox "T":\n  - option "A"\n')


def test_only_escaped_quotes_and_backslashes_are_unescaped() -> None:
    page = '- dialog "Say \\"hi\\" a\\\\b \\n":\n  - button "OK"\n'

    text, _ = scope(page, "dialog", 'Say "hi" a\\b \\n')

    assert 'button "OK"' in text


def test_a_selected_option_with_quotes_in_its_name_is_written_escaped() -> None:
    page = '- combobox "T":\n  - option "Say \\"hi\\"" [selected]\n  - option "B"\n'

    assert collapse_options(page).strip() == ('- combobox "T" (2 options, selected "Say \\"hi\\"")')
