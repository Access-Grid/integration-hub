"""Which card on a cardholder counts as the operator's marker.

A marker is the placeholder an operator creates to ask for a pass: a
trigger-format card holding a number that opens nothing. It is overwritten
rather than worked around, so it does not spend one of Millennium's three
slots on a card nobody will use.

The cards *we* write carry the trigger format too, so the format alone
cannot tell the two apart — the ledger is the only thing that can. Asked for
one credential's cards, it answers "no, not mine" about a card belonging to
another credential of the same cardholder, and that card is then overwritten
in preference to an empty slot.

Which matters the moment a cardholder has a second credential: the first use
of a watch pass would destroy the phone credential it was meant to sit
beside. Reproduced below against a slot layout taken from a live install.
"""

from __future__ import annotations

from agsync.lib.pacs.base import CredentialIdentity
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11665"
ROSTER = [{"ID": PID, "IsActive": True, "Name": "Dorvil, Greg"}]


def _page(millennium_page, set_slot, slots: dict[int, tuple[str, str, str] | None]):
    """slots maps index -> (card_id, number, facility) or None for empty."""
    page = millennium_page
    for index in (1, 2, 3):
        spec = slots.get(index)
        if spec is None:
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        else:
            card_id, number, facility = spec
            page = set_slot(
                page, index, card_id=card_id, card_number=number,
                facility_code=facility, card_format=TRIGGER, active=True,
            )
    return page


def _adapter(make_millennium_adapter, page):
    client = FakeMillenniumClient(roster=ROSTER, pages={PID: page})
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER
    )
    list(adapter.list_people())
    return adapter, client


def _written(client) -> dict[int, str]:
    """Slot -> the card number the import put there, "" for a slot it did
    not touch.

    Which slot is chosen is what these tests are about, and that decision is
    unchanged by the move to an import. The card's internal id no longer
    appears at all: the import addresses a slot, not a card, so there is
    nothing to keep in place.
    """
    from tests._fakes import written_slot

    return {
        i: (written_slot(client, i).card_number or "")
        if written_slot(client, i) is not None else ""
        for i in (1, 2, 3)
    }


def test_another_credentials_card_is_not_a_marker(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The regression, at the layout it was found on.

    Pass A is installed in slot 2. The operator adds a marker in slot 3 to
    ask for a second pass. Slot 1 is empty. `_marker_slot` scans in slot
    order, so it reaches pass A's card first.
    """
    page = _page(millennium_page, set_slot, {
        1: None,
        2: ("8100", "74", "99"),   # pass A, installed
        3: ("8200", "9", "99"),    # the operator's new marker
    })
    adapter, client = _adapter(make_millennium_adapter, page)
    SeosLedger.record(PID, "seos", [
        {"slot": 2, "card_number": "74", "facility_code": "99"},
    ])

    assert adapter.write_back_credentials(
        PID, "seos-card8200", [CredentialIdentity("99", "81")]
    ) is True

    written = _written(client)
    assert written[2] == "", "pass A's installed card is not in the file at all"
    assert written[3] != "", "the operator's marker is what gets used"
    assert written[1] == "", "the empty slot is left for a watch"


def test_the_marker_is_still_consumed_rather_than_worked_around(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """A placeholder must not keep a slot. Three is not many."""
    page = _page(millennium_page, set_slot, {
        1: ("7919", "1", "99"),    # the marker
        2: None,
        3: None,
    })
    adapter, client = _adapter(make_millennium_adapter, page)

    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])

    written = _written(client)
    assert written[1] == "1238", "written over the marker"
    assert written[2] == ""


def test_a_card_from_any_credential_of_theirs_is_ours(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Three credentials, and the only free position is the real marker."""
    page = _page(millennium_page, set_slot, {
        1: ("8100", "74", "99"),   # credential one
        2: ("8300", "88", "99"),   # credential two
        3: ("8200", "9", "99"),    # the marker
    })
    adapter, client = _adapter(make_millennium_adapter, page)
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "74", "facility_code": "99"}])
    SeosLedger.record(PID, "seos-card8300", [
        {"slot": 2, "card_number": "88", "facility_code": "99"}])

    assert adapter.write_back_credentials(
        PID, "seos-card8200", [CredentialIdentity("99", "81")]
    ) is True

    written = _written(client)
    assert written[1] == ""
    assert written[2] == ""
    assert written[3] != ""


def test_a_stranger_card_in_the_trigger_format_is_still_a_marker(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The behaviour that is not changing.

    A trigger-format card the ledger has never heard of is what an operator
    creates to request a pass. It is consumed, as it always was.
    """
    page = _page(millennium_page, set_slot, {
        1: ("9001", "5", "45"),    # unrecorded — an operator's marker
        2: ("8100", "74", "99"),   # ours
        3: None,
    })
    adapter, client = _adapter(make_millennium_adapter, page)
    SeosLedger.record(PID, "seos", [
        {"slot": 2, "card_number": "74", "facility_code": "99"}])

    adapter.write_back_credentials(
        PID, "seos-card9001", [CredentialIdentity("99", "81")]
    )

    written = _written(client)
    assert written[1] != "", "the unrecorded card is the marker"
    assert written[2] == "", "ours is untouched — not in the file"


def test_the_card_id_is_recorded_in_the_cycle_log(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    """So its stability over time can be checked before anything relies on it.

    The credential id scheme for a second pass has to key on something that
    survives both the marker being overwritten and the passage of time.
    Millennium's CardID is the only candidate; this is how we find out.
    """
    import logging

    page = _page(millennium_page, set_slot, {1: ("7919", "1", "99"), 2: None, 3: None})
    adapter, _ = _adapter(make_millennium_adapter, page)

    with caplog.at_level(logging.INFO):
        list(adapter.list_credentials(PID))

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "id=7919" in messages
