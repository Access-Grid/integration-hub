"""The import file: what goes in it, and what deliberately does not.

Verified against the live install: **a column left out of the file is
untouched; a column included with an empty value is erased.** Both halves
are load-bearing. The first is what makes a narrow write safe. The second is
why including a column we did not mean to change is the whole danger —
including First Name with the wrong value renamed a cardholder, and
including Card Type empty wiped it.

So the file carries the key and the changed cells, and nothing else.

The second thing these tests pin is the column *naming*. Millennium's field
screen places any header that exactly matches one of its own labels, and a
placed field is removed from the catalogue that same page carries — which is
exactly how we would lose the numeric id we need to map it. So every header
is an alias that matches nothing, and the real label travels separately.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.millennium_ultra import import_rows
from agsync.lib.pacs.millennium_ultra.import_rows import RowWrite, SlotWrite

EMPLOYEE = "22344"


def _row(*slots: SlotWrite, employee_id: str = EMPLOYEE) -> RowWrite:
    return RowWrite(employee_id=employee_id, slots=slots)


def _header(csv_text: str) -> list[str]:
    return csv_text.split("\r\n")[0].split(",")


def _cells(csv_text: str, line: int = 1) -> dict[str, str]:
    rows = csv_text.split("\r\n")
    return dict(zip(_header(csv_text), rows[line].split(","), strict=True))


# =====================================================================
# Naming: never Millennium's own labels
# =====================================================================


def test_no_header_is_one_of_millenniums_own_labels():
    """Because a header that matches is placed, and a placed field is then
    missing from the catalogue we need to read its id out of."""
    csv_text = import_rows.to_csv([
        _row(SlotWrite(2, card_number="5001", facility_code="66", active=True))
    ])

    for header in _header(csv_text):
        assert header.startswith("agsync_"), header
        assert " " not in header, header


def test_every_column_carries_the_label_it_feeds():
    """The alias goes in the file; the real label is what gets mapped."""
    columns = import_rows.columns_for([_row(SlotWrite(2, active=False))])

    assert columns == [
        ("agsync_employee_id", "Employee ID"),
        ("agsync_card_2_active", "Card 2 Active"),
    ]


def test_an_alias_is_stable_for_a_given_label():
    assert import_rows.alias_for("Card 2 Facility Code") == (
        "agsync_card_2_facility_code"
    )
    assert import_rows.alias_for("Encoded Card No.") == "agsync_encoded_card_no"


# =====================================================================
# What the file contains
# =====================================================================


def test_the_key_is_always_the_first_column():
    """It is the only thing the import matches a row on."""
    csv_text = import_rows.to_csv([_row(SlotWrite(2, active=False))])

    assert _header(csv_text)[0] == "agsync_employee_id"
    assert _cells(csv_text)["agsync_employee_id"] == EMPLOYEE


def test_only_the_slots_being_written_get_columns():
    """The blast radius of a write, expressed as the width of the file.

    A change to slot 2 cannot reach slots 1 and 3, the cardholder's name,
    their card type, their address, or the access levels on any card. None
    of it is in the file.
    """
    csv_text = import_rows.to_csv([_row(SlotWrite(2, active=False))])

    assert _header(csv_text) == ["agsync_employee_id", "agsync_card_2_active"]


def test_the_name_and_card_type_are_never_written():
    """The two fields whose inclusion did real damage in testing.

    Including First Name renamed a cardholder; including Card Type empty
    wiped it. Neither is expressible from here — there is no way to ask for
    them.
    """
    csv_text = import_rows.to_csv([
        _row(SlotWrite(1, card_number="5001", facility_code="66", active=True))
    ])

    for forbidden in ("first", "last", "name", "card_type", "expiration"):
        assert not any(forbidden in h for h in _header(csv_text)), forbidden


def test_only_the_fields_being_written_get_columns():
    csv_text = import_rows.to_csv([
        _row(SlotWrite(3, card_number="5001", facility_code="66"))
    ])

    assert _header(csv_text) == [
        "agsync_employee_id",
        "agsync_card_3_encoded_card_no",
        "agsync_card_3_facility_code",
    ]


def test_an_unset_field_is_absent_but_an_empty_one_is_written():
    """`None` and `""` have to stay distinguishable.

    An absent column leaves the field alone; an empty one clears it. Your
    testing proved the second, which is also how a slot would be released —
    so collapsing the two would both make that unsayable and make it happen
    by accident.
    """
    absent = import_rows.to_csv([_row(SlotWrite(1, card_number="5001"))])
    assert "agsync_card_1_facility_code" not in _header(absent)

    emptied = import_rows.to_csv([
        _row(SlotWrite(1, card_number="", facility_code=""))
    ])
    assert "agsync_card_1_facility_code" in _header(emptied)
    assert _cells(emptied)["agsync_card_1_facility_code"] == ""


def test_active_is_written_with_the_capital_millennium_requires():
    """Verified on the live install: "true" is not accepted where "True" is."""
    on = import_rows.to_csv([_row(SlotWrite(1, active=True))])
    off = import_rows.to_csv([_row(SlotWrite(1, active=False))])

    assert _cells(on)["agsync_card_1_active"] == "True"
    assert _cells(off)["agsync_card_1_active"] == "False"


def test_columns_are_ordered_by_slot_then_field():
    """So two identical plans produce an identical file."""
    one = import_rows.to_csv([
        _row(SlotWrite(3, active=True), SlotWrite(1, card_number="5001"))
    ])
    other = import_rows.to_csv([
        _row(SlotWrite(1, card_number="5001"), SlotWrite(3, active=True))
    ])

    assert one == other
    assert _header(one) == [
        "agsync_employee_id",
        "agsync_card_1_encoded_card_no",
        "agsync_card_3_active",
    ]


def test_several_rows_share_one_header_and_leave_the_gaps_empty():
    csv_text = import_rows.to_csv([
        _row(SlotWrite(1, active=True), employee_id="22344"),
        _row(SlotWrite(2, card_number="5001"), employee_id="33137"),
    ])

    assert _header(csv_text) == [
        "agsync_employee_id",
        "agsync_card_1_active",
        "agsync_card_2_encoded_card_no",
    ]
    assert _cells(csv_text, 1)["agsync_card_2_encoded_card_no"] == ""
    assert _cells(csv_text, 2)["agsync_card_1_active"] == ""
    assert _cells(csv_text, 2)["agsync_employee_id"] == "33137"


def test_the_file_uses_crlf_and_a_header_row():
    csv_text = import_rows.to_csv([_row(SlotWrite(1, active=True))])

    assert csv_text.endswith("\r\n")
    assert csv_text.count("\r\n") == 2  # the header plus one row


def test_a_value_with_a_comma_is_quoted():
    csv_text = import_rows.to_csv([
        _row(SlotWrite(1, card_format="AccessGrid, HID Wallet"))
    ])

    assert '"AccessGrid, HID Wallet"' in csv_text


# =====================================================================
# Refusals
# =====================================================================


def test_an_empty_file_is_refused():
    with pytest.raises(ValueError, match="at least one row"):
        import_rows.to_csv([])


def test_a_row_that_changes_nothing_is_refused():
    """A file carrying only the key would match a cardholder and apply
    nothing, which is a request we should never have built."""
    with pytest.raises(ValueError, match="only the key"):
        import_rows.to_csv([_row()])
