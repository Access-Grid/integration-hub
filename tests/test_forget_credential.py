"""Dropping the ledger entry once the cards it records are gone.

The Seos ledger lists the cards this integration wrote into Millennium. A
recorded card missing from the cardholder is how an operator's revocation is
detected — so an entry that outlives its cards suspends whatever pass a new
trigger card earns next, from birth, permanently.

`list_credentials` returning nothing is the moment to act, because our own
cards carry the trigger format: a cardholder with no trigger slot cannot be
holding one of ours. It is the same empty answer phase 3 deletes the pass on.
"""

from __future__ import annotations

import logging

from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, PacsCards
from tests._fakes import FakeMillenniumClient, export_row

TRIGGER = "HID 37"
OTHER = "Wiegand Card"
PID = "33137"


def _card(number, facility="66", card_format=TRIGGER):
    return {
        "card_number": number, "facility_code": facility,
        "card_format": card_format, "active": True,
    }


def _read(make_millennium_adapter, *slots):
    """One cycle: the export holds this cardholder with these slots."""
    client = FakeMillenniumClient(rows=[export_row(PID, slots=slots)])
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    list(adapter.list_people())
    return list(adapter.list_credentials(PID))


def _record(pid=PID, number="1238"):
    PacsCards.record(pid, "seos", [
        {"slot": 1, "card_number": number, "facility_code": "66"},
    ])


def test_the_entry_goes_when_every_trigger_card_is_deleted(
    make_millennium_adapter, caplog
):
    _record()
    # The operator deletes the lot, leaving an unrelated card behind.
    with caplog.at_level(logging.INFO):
        assert _read(make_millennium_adapter, _card("900", "50", OTHER)) == []
    assert PacsCards.get(PID, "seos") == []
    assert f"forgetting the cards recorded for cardholder {PID}" in caplog.text


def test_a_new_pass_is_not_suspended_by_the_one_before_it(
    make_millennium_adapter
):
    """The regression."""
    _record()
    assert _read(make_millennium_adapter, _card("900", "50", OTHER)) == []

    # A fresh trigger card earns a fresh pass, healthy from the start.
    [cred] = _read(make_millennium_adapter, _card("1"), None, None)
    assert cred.status.value == "active", "must not inherit a suspension"


def test_a_recorded_card_still_in_a_slot_is_not_forgotten(
    make_millennium_adapter
):
    """An operator changed our card's format: the card is dormant, not gone.

    Forgetting it would lose the one fact that makes the card ours, and its
    pass would be deleted. It is kept, and its pass held as it is.
    """
    _record()
    [cred] = _read(make_millennium_adapter, _card("1238", card_format=OTHER))
    assert cred.hold is True
    assert PacsCards.get(PID, "seos")[0]["status"] == "dormant"


def test_a_cardholder_we_hold_nothing_on_is_left_alone(
    make_millennium_adapter
):
    _record(pid="9999", number="4242")
    assert _read(make_millennium_adapter, _card("900", "50", OTHER)) == []
    assert PacsCards.get("9999", "seos"), "somebody else's entry must survive"
