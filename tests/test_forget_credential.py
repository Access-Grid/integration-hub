"""Dropping the ledger entry once the cards it records are gone.

The Seos ledger lists the cards this integration wrote into Millennium, and
it has to outlive a cycle: a recorded card missing from the cardholder is how
an operator's revocation is detected, and it is what stops a revoked card
being written back. But it must not outlive the cards themselves.

Left behind, it suspends whatever pass a new trigger card earns next — the old
numbers are missing from the cardholder, which reads as a revocation. So an
operator who cleared a cardholder's format-8 cards and gave them a new one got
a pass that was suspended from birth, permanently.

`list_credentials` returning nothing is the moment to act, because our own
cards carry the trigger format: a cardholder with no trigger slot cannot be
holding one of ours. It is the same empty answer phase 3 deletes the pass on.
"""

from __future__ import annotations

import logging

from agsync.lib.pacs.base import CredentialIdentity
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11587"
ROSTER = [{"ID": PID, "IsActive": True, "Name": "Grid, Accessg"}]


def _page(millennium_page, set_slot, *, slot1=(TRIGGER, "1", "99")):
    """Slot 1 carries (format, number, facility); slots 2 and 3 are empty."""
    fmt, number, facility = slot1
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number=number,
        facility_code=facility, card_format=fmt, active=True,
    )
    for index in (2, 3):
        page = set_slot(page, index, card_id="", card_number="", card_format=None)
    return page


def _adapter(make_millennium_adapter, page):
    client = FakeMillenniumClient(roster=ROSTER, pages={PID: page})
    return make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER
    ), client


def _read(adapter):
    """One cycle's reads, which is what refreshes the adapter's view."""
    list(adapter.list_people())
    return list(adapter.list_credentials(PID))


def _messages(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_the_entry_goes_when_every_format_8_card_is_deleted(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    adapter, client = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])
    assert SeosLedger.get(PID, "seos")

    # The operator deletes the lot, leaving an unrelated card behind.
    client._pages[PID] = _page(millennium_page, set_slot, slot1=("1", "900", "50"))

    with caplog.at_level(logging.INFO):
        assert _read(adapter) == []

    assert SeosLedger.get(PID, "seos") == []
    assert "forgetting the cards recorded for cardholder 11587" in _messages(caplog)


def test_a_new_pass_is_not_suspended_by_the_one_before_it(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The regression."""
    adapter, client = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])

    # Cleared, so phase 3 deletes the pass and the entry goes with it.
    client._pages[PID] = _page(millennium_page, set_slot, slot1=("1", "900", "50"))
    assert _read(adapter) == []

    # A fresh trigger card earns a fresh pass.
    client._pages[PID] = _page(millennium_page, set_slot)
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1250")])
    client._pages[PID] = _page(
        millennium_page, set_slot, slot1=(TRIGGER, "1250", "66")
    )

    creds = _read(adapter)
    assert len(creds) == 1
    assert creds[0].status.value == "active", "must not inherit a suspension"
    assert [e["card_number"] for e in SeosLedger.get(PID, "seos")] == ["1250"]


def test_a_recorded_card_still_in_a_slot_is_not_forgotten(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The guard. Only reachable if an operator changes our card's format.

    Forgetting here would lose the one fact that makes the card ours: nothing
    would release its slot, and `_marker_slot` could overwrite it.
    """
    adapter, client = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])

    # Our card is still there, but no longer carries the trigger format.
    client._pages[PID] = _page(millennium_page, set_slot, slot1=("1", "1238", "66"))

    assert _read(adapter) == []
    assert [e["card_number"] for e in SeosLedger.get(PID, "seos")] == ["1238"]


def test_a_cardholder_we_hold_nothing_on_is_left_alone(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """A namesake the export's name join swept in must not touch the ledger."""
    adapter, _ = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot, slot1=("1", "900", "50"))
    )
    SeosLedger.record("9999", "seos", [
        {"slot": 2, "card_number": "4242", "facility_code": "66"},
    ])

    assert _read(adapter) == []
    assert SeosLedger.get("9999", "seos"), "somebody else's entry must survive"
