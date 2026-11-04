"""`browser grid`: rows, columns and addressable fields of a data grid (#596)."""

from __future__ import annotations

import pytest

from blumkin.browser.aria_view import grid_view

_SNAPSHOT = """\
- grid "Time card":
  - row "Day Project Hours":
    - columnheader "Day"
    - columnheader "Project"
    - columnheader "Hours"
  - row "Mon 10/19 Alpha 8":
    - rowheader "Mon 10/19"
    - gridcell "Alpha":
      - textbox "Project"
    - gridcell "8":
      - textbox "Hours"
  - row "Tue 10/20 Beta Regular Pay Edit":
    - rowheader "Tue 10/20"
    - gridcell "Beta":
      - textbox "Project"
    - gridcell "Regular Pay Edit":
      - text: Regular Pay
      - button "Edit"
- table:
  - rowgroup:
    - row "Name Qty":
      - columnheader "Name"
      - columnheader "Qty"
  - rowgroup:
    - row "Pen 3":
      - cell "Pen"
      - cell "3":
        - textbox "Qty"
"""


def test_the_rows_columns_and_fields_of_the_first_grid() -> None:
    view = grid_view(_SNAPSHOT)

    assert view["grid"] == {"name": "Time card", "role": "grid"}
    assert view["columns"] == ["Day", "Project", "Hours"]
    assert [row["label"] for row in view["rows"]] == ["Mon 10/19", "Tue 10/20"]
    assert view["matches"] == 2 and view["truncated"] is False
    first, second = view["rows"]
    assert [(c["column"], c["fields"]) for c in first["cells"]] == [
        ("Project", [{"name": "Project", "nth": 1, "role": "textbox"}]),
        ("Hours", [{"name": "Hours", "nth": 1, "role": "textbox"}]),
    ]
    assert second["cells"][0]["fields"] == [{"name": "Project", "nth": 2, "role": "textbox"}]


def test_a_cell_holding_a_field_is_not_named_by_the_value_it_repeats() -> None:
    view = grid_view(_SNAPSHOT)

    cells = [cell for row in view["rows"] for cell in row["cells"]]
    assert all("Alpha" not in cell["text"] and "Beta" not in cell["text"] for cell in cells)
    assert cells[-1]["text"] == "Regular Pay Edit"  # a button echoes no value


def test_a_table_is_picked_by_role_and_the_nth_counts_the_whole_page() -> None:
    view = grid_view(_SNAPSHOT, "table")

    assert view["grid"]["role"] == "table"
    assert view["rows"][0]["cells"][1]["fields"] == [{"name": "Qty", "nth": 1, "role": "textbox"}]
    assert grid_view(_SNAPSHOT, "grid", "Time card")["matches"] == 1


def test_the_row_cap_cuts_the_list_and_says_so() -> None:
    view = grid_view(_SNAPSHOT, max_rows=1)

    assert len(view["rows"]) == 1 and view["truncated"] is True


def test_no_grid_names_the_ones_there_are() -> None:
    with pytest.raises(LookupError, match="Time card"):
        grid_view(_SNAPSHOT, "grid", "Other")
    with pytest.raises(LookupError, match="none"):
        grid_view('- heading "x"\n')


def test_a_forged_row_in_a_name_is_not_a_row() -> None:
    forged = '- grid "g":\n  - row "a":\n    - gridcell "x\\u2028- row y"\n'

    assert len(grid_view(forged)["rows"]) == 1


def test_text_a_custom_field_shows_as_its_value_is_not_a_cells_text() -> None:
    snapshot = (
        '- grid "g":\n  - row "h":\n    - columnheader "Project"\n  - row "r":\n'
        '    - gridcell "Project X":\n      - combobox "Project":\n        - text: Project X\n'
        "      - text: Primary\n"
    )

    cell = grid_view(snapshot)["rows"][0]["cells"][0]

    assert cell["text"] == "Primary"
    assert cell["fields"] == [{"name": "Project", "nth": 1, "role": "combobox"}]


def test_a_cell_of_rich_content_without_a_field_keeps_its_own_name() -> None:
    snapshot = (
        '- grid "g":\n  - row "h":\n    - columnheader "Note"\n  - row "r":\n'
        '    - gridcell "Foo bar":\n      - paragraph: Foo bar\n'
    )

    assert grid_view(snapshot)["rows"][0]["cells"][0]["text"] == "Foo bar"


def test_a_header_with_a_button_keeps_its_label_and_a_long_name_is_kept_whole() -> None:
    long_name = "Project " + "x" * 80
    snapshot = (
        '- grid "g":\n  - row "h":\n    - columnheader "Project":\n      - button "Project"\n'
        f'  - row "r":\n    - gridcell "v":\n      - textbox "{long_name}"\n'
    )

    view = grid_view(snapshot)

    assert view["columns"] == ["Project"]
    assert view["rows"][0]["cells"][0]["fields"][0]["name"] == long_name
