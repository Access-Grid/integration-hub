"""Building the import file and reading Millennium's log of it.

The logs are real, from imports run by hand against a live install.
"""

from __future__ import annotations

import csv
import io
import pathlib

import pytest

from agsync.lib.pacs.millennium_ultra.import_file import Row, build, parse_log

FIXTURES = pathlib.Path(__file__).parent / "millennium_fixtures"


def _card(slot, number="5001", facility="66", **more):
    cells = {(slot, "Encoded Card No."): number, (slot, "Facility Code"): facility}
    cells.update({(slot, label): value for label, value in more.items()})
    return cells


def _rows(imported):
    return list(csv.reader(io.StringIO(imported.csv)))


# --- the file -----------------------------------------------------------


def test_a_row_carries_its_key_its_names_and_only_what_changes():
    [imported] = build([Row("22344", "King", "Bunsen", _card(2, Active="False"))])
    assert _rows(imported) == [
        ["EmployeeID", "FirstName", "LastName",
         "Card2Active", "Card2EncodedCardNo.", "Card2FacilityCode"],
        ["22344", "King", "Bunsen", "False", "5001", "66"],
    ]


def test_each_column_is_mapped_by_its_position():
    [imported] = build([Row("22344", "King", "Bunsen", _card(2, Active="False"))])
    assert [(f["UserColumnID"], f["Card"], f["ID"]) for f in imported.fields] == [
        (0, 0, 4), (1, 0, 1), (2, 0, 2), (3, 2, 19), (4, 2, 16), (5, 2, 21),
    ]
    # Shaped like the mapping that worked by hand: Millennium's label as the
    # field's name, and our unmatchable header as the column's.
    assert imported.fields[3]["Name"] == "Card 2 Active"
    assert imported.fields[3]["UserColumnName"] == "Card2Active"


def test_rows_changing_the_same_things_share_a_file():
    imports = build([
        Row("1", "A", "One", _card(2, Active="False")),
        Row("2", "B", "Two", _card(2, "5002", Active="True")),
    ])
    assert len(imports) == 1
    assert len(_rows(imports[0])) == 3


def test_rows_changing_different_things_never_share_a_file():
    """A shared column would be an empty cell for the other row, and an
    empty cell can erase the field."""
    imports = build([
        Row("1", "A", "One", _card(2, Active="False")),
        Row("2", "B", "Two", _card(3, Active="False")),
    ])
    assert len(imports) == 2
    assert all(len(_rows(i)) == 2 for i in imports)


def test_a_card_without_its_number_and_facility_code_is_refused():
    """Millennium would skip the slot, and the change would quietly not land."""
    with pytest.raises(ValueError, match="card 2: no Facility Code"):
        build([Row("22344", "King", "Bunsen", {(2, "Encoded Card No."): "5001"})])


# --- the log ------------------------------------------------------------


def _log(name):
    return parse_log((FIXTURES / name).read_text())


def test_skipped_cards_are_warnings_and_counted():
    log = _log("import_log_cards_skipped.txt")
    assert log.counts == {
        "cardholders added": 0, "cardholders updated": 1,
        "cards added": 0, "cards updated": 1, "cards skipped": 2,
    }
    assert len(log.warnings) == 2
    assert log.skipped() == {"22344"}
    assert log.errors == []


def test_a_skipped_row_is_named():
    log = _log("import_log_row_skipped.txt")
    assert log.counts["cardholders skipped"] == 1
    assert "name or EmployeeID can not be null" in log.warnings[0]
    assert log.skipped() == {"22344"}


def test_an_error_is_kept_even_when_a_later_run_succeeds():
    log = _log("import_log_error_then_success.txt")
    assert log.errors == [
        "Error while importing: Necessary column (EmployeeID) doesn't exist "
        "in imported data fields!"
    ]
    assert log.counts["cardholders updated"] == 1


# --- access levels ------------------------------------------------------


def test_a_cards_levels_are_copied_to_the_same_positions():
    from agsync.lib.pacs.millennium_ultra.export import AccessLevel
    from agsync.lib.pacs.millennium_ultra.import_file import access_level_cells

    levels = (AccessLevel(1, "MASTER 1", "", "12/31/2026 00:00:00"), AccessLevel(7, "T2 Front"))
    assert access_level_cells(3, levels) == {
        (3, "Access Level 1"): "MASTER 1",
        (3, "Access Level 1 Expiration Date"): "12/31/2026 00:00:00",
        (3, "Access Level 7"): "T2 Front",
    }


def test_access_levels_map_to_millenniums_field_ids():
    cells = _card(3, **{"Access Level 1": "MASTER 1", "Access Level 10 Expiration Date": "x"})
    [imported] = build([Row("22344", "King", "Bunsen", cells)])
    ids = {f["UserColumnName"]: f["ID"] for f in imported.fields}
    assert ids["Card3AccessLevel1"] == 200
    assert ids["Card3AccessLevel10ExpirationDate"] == 721
