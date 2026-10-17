"""The CSV an import is made of, and who it is allowed to name.

Everything here is a pure function of its arguments, which is the point of
the module existing: the logic that caused every write bug on this adapter
now reads as "these rows in, this file out", with no HTML, no multipart, no
anti-forgery token and no clock.

Two properties carry most of the weight:

  * **a column that is not in the file is a field the import leaves alone**,
    so the narrowest possible write is also the safest one; and
  * **a name that identifies more than one cardholder is never written to**,
    because the import matches rows by name and the cost of guessing is a
    working credential on the wrong person's record.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.millennium_ultra import import_rows
from agsync.lib.pacs.millennium_ultra.import_rows import (
    Identity,
    RowWrite,
    SlotWrite,
)


def _exported(first, last, card_type=""):
    return SimpleNamespace(first_name=first, last_name=last, card_type=card_type)


def _row(*slots, first="Kai", last="Mowatt", card_type=""):
    return RowWrite(identity=Identity(first, last, card_type), slots=slots)


def _header(csv_text: str) -> list[str]:
    return csv_text.split("\r\n")[0].split(",")


def _cells(csv_text: str, line: int = 1) -> dict[str, str]:
    rows = csv_text.split("\r\n")
    return dict(zip(_header(csv_text), rows[line].split(","), strict=True))


# =====================================================================
# The file
# =====================================================================


def test_the_identity_columns_always_come_first_and_always_appear():
    """They are the match key, so a file without them matches nothing."""
    csv_text = import_rows.to_csv([_row(SlotWrite(2, active=False))])

    assert _header(csv_text)[:3] == ["First Name", "Last Name", "Card Type"]
    assert _cells(csv_text)["First Name"] == "Kai"
    assert _cells(csv_text)["Last Name"] == "Mowatt"


def test_only_the_slots_being_written_get_columns():
    """The blast radius of a write, expressed as the width of the file.

    A status change on slot 2 cannot reach slots 1 and 3, the cardholder's
    address, their photo, or the access levels on any card — none of it is
    in the file.
    """
    csv_text = import_rows.to_csv([_row(SlotWrite(2, active=False))])

    assert _header(csv_text) == [
        "First Name", "Last Name", "Card Type", "Card 2 Active",
    ]


def test_only_the_fields_being_written_get_columns():
    csv_text = import_rows.to_csv([
        _row(SlotWrite(3, card_number="5001", facility_code="66"))
    ])

    assert _header(csv_text) == [
        "First Name", "Last Name", "Card Type",
        "Card 3 Encoded Card No.", "Card 3 Facility Code",
    ]


def test_an_unset_field_is_absent_but_an_empty_one_is_written():
    """`None` and `""` have to stay distinguishable.

    An absent column leaves the field as it is; an empty one is a value the
    import applies. Writing an empty expiration date is how a card's expiry
    would be cleared, so collapsing the two would make that unsayable — and,
    worse, make it happen by accident.
    """
    absent = import_rows.to_csv([_row(SlotWrite(1, card_number="5001"))])
    assert "Card 1 Facility Code" not in _header(absent)

    emptied = import_rows.to_csv([
        _row(SlotWrite(1, card_number="5001", facility_code=""))
    ])
    assert "Card 1 Facility Code" in _header(emptied)
    assert _cells(emptied)["Card 1 Facility Code"] == ""


def test_active_is_written_the_way_millennium_prints_it():
    """True/False, matching the export's own spelling of the same field."""
    on = import_rows.to_csv([_row(SlotWrite(1, active=True))])
    off = import_rows.to_csv([_row(SlotWrite(1, active=False))])

    assert _cells(on)["Card 1 Active"] == "True"
    assert _cells(off)["Card 1 Active"] == "False"


def test_columns_are_ordered_by_slot_then_by_field():
    """So two identical plans produce an identical file."""
    one = import_rows.to_csv([
        _row(SlotWrite(3, active=True), SlotWrite(1, card_number="5001"))
    ])
    other = import_rows.to_csv([
        _row(SlotWrite(1, card_number="5001"), SlotWrite(3, active=True))
    ])

    assert one == other
    assert _header(one) == [
        "First Name", "Last Name", "Card Type",
        "Card 1 Encoded Card No.", "Card 3 Active",
    ]


def test_several_rows_share_one_header_and_leave_the_gaps_empty():
    csv_text = import_rows.to_csv([
        _row(SlotWrite(1, active=True), first="Kai", last="Mowatt"),
        _row(SlotWrite(2, card_number="5001"), first="Greg", last="Dorvil"),
    ])

    assert _header(csv_text) == [
        "First Name", "Last Name", "Card Type",
        "Card 1 Active", "Card 2 Encoded Card No.",
    ]
    assert _cells(csv_text, 1)["Card 2 Encoded Card No."] == ""
    assert _cells(csv_text, 2)["Card 1 Active"] == ""


def test_the_file_uses_crlf_and_a_header_row():
    """What `FieldHeaders=true` promises, and what the export produces."""
    csv_text = import_rows.to_csv([_row(SlotWrite(1, active=True))])

    assert csv_text.endswith("\r\n")
    assert csv_text.count("\r\n") == 2  # header plus the one row


def test_a_value_with_a_comma_is_quoted():
    csv_text = import_rows.to_csv([
        _row(SlotWrite(1, card_format="AccessGrid, HID Wallet"))
    ])

    assert '"AccessGrid, HID Wallet"' in csv_text


def test_an_empty_file_is_refused():
    with pytest.raises(ValueError, match="at least one row"):
        import_rows.to_csv([])


# =====================================================================
# Who may be named
# =====================================================================


def test_a_name_belonging_to_one_cardholder_is_writable():
    unique = import_rows.unique_identities([
        _exported("Kai", "Mowatt"),
        _exported("Greg", "Dorvil"),
    ])

    assert Identity("Kai", "Mowatt").key in unique
    assert unique[Identity("Kai", "Mowatt").key].first_name == "Kai"


def test_a_shared_name_is_withheld_entirely():
    """Not resolved to the first row.

    The import offers no way to say which of two people a row meant, and
    42% of this install's roster collides by name — one of them five ways.
    So the answer to "which Maria Garcia" is nobody.
    """
    unique = import_rows.unique_identities([
        _exported("Maria", "Garcia"),
        _exported("Maria", "Garcia"),
        _exported("Greg", "Dorvil"),
    ])

    assert Identity("Maria", "Garcia").key not in unique
    assert Identity("Greg", "Dorvil").key in unique


def test_a_third_colliding_row_does_not_resurrect_the_name():
    unique = import_rows.unique_identities([
        _exported("Maria", "Garcia"),
        _exported("Maria", "Garcia"),
        _exported("Maria", "Garcia"),
    ])

    assert unique == {}


def test_names_are_matched_without_case_or_padding():
    """The export and the roster disagree about both, routinely."""
    unique = import_rows.unique_identities([
        _exported(" Kai ", "MOWATT"),
        _exported("kai", "mowatt"),
    ])

    assert unique == {}


def test_the_identity_keeps_the_exports_own_spelling():
    """What goes in the file is what the export printed.

    The export is a dump of the table the import matches against, so
    round-tripping its spelling is the only way to be sure a row lands on
    the record it was read from.
    """
    unique = import_rows.unique_identities([_exported(" Kai", "Mowatt ")])

    identity = unique[Identity("Kai", "Mowatt").key]
    assert (identity.first_name, identity.last_name) == (" Kai", "Mowatt ")


def test_card_type_is_part_of_the_key():
    """Blank for every row this install has produced, and carried anyway.

    It is one of the three columns the import matches on whether or not it
    holds anything, so dropping it from the key here would call two records
    the same when the import would not.
    """
    unique = import_rows.unique_identities([
        _exported("Kai", "Mowatt", "Cardholder"),
        _exported("Kai", "Mowatt", "Visitor"),
    ])

    assert len(unique) == 2
