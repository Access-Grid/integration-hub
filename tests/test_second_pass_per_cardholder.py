"""A second Seos pass for a cardholder who already has one.

Wallet will not add a credential to a watch after the pass is installed, so
the only way to reach a holder's watch, once they have installed on their
phone, is a second pass they install on the watch alone.

A cardholder's first pass is keyed `seos`. Passes recorded under any other
id (`seos-card<CardID>`, from before the export) are still read from the
ledger. A *new* second pass has no id to be issued under yet: they were keyed
on the card's CardID, which the export does not carry.
"""

from __future__ import annotations

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient, export_row

TRIGGER = "HID 37"
PID = "33137"


def _card(number, facility="99", active=True, card_format=TRIGGER):
    return {
        "card_number": number, "facility_code": facility,
        "card_format": card_format, "active": active,
    }


def _creds(make_millennium_adapter, *slots) -> dict:
    client = FakeMillenniumClient(rows=[export_row(PID, slots=slots)])
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    list(adapter.list_people())
    return {c.id: c for c in adapter.list_credentials(PID)}


def _record(credential_id, slot, number):
    SeosLedger.record(PID, credential_id, [
        {"slot": slot, "card_number": number, "facility_code": "99"},
    ])


def test_a_cardholder_with_one_pass_has_one_credential(make_millennium_adapter, seos_ledger):
    _record("seos", 2, "74")
    assert set(_creds(make_millennium_adapter, None, _card("74"))) == {"seos"}


def test_a_cardholders_first_pass_keeps_the_bare_id(make_millennium_adapter, seos_ledger):
    assert set(_creds(make_millennium_adapter, _card("1"))) == {"seos"}


def test_a_card_of_ours_is_never_read_as_a_request(make_millennium_adapter, seos_ledger):
    """Our cards carry the trigger format, so only the ledger can tell."""
    _record("seos", 2, "74")
    _record("seos-card8200", 3, "90")
    creds = _creds(make_millennium_adapter, None, _card("74"), _card("90"))
    assert set(creds) == {"seos", "seos-card8200"}


def test_a_second_pass_is_suspended_on_its_own_cards_only(
    make_millennium_adapter, seos_ledger
):
    """One pass going bad must not take the other with it."""
    _record("seos", 2, "74")
    _record("seos-card8200", 3, "90")
    creds = _creds(make_millennium_adapter, None, _card("74"), _card("90", active=False))
    assert creds["seos"].status is CredentialStatus.ACTIVE
    assert creds["seos-card8200"].status is CredentialStatus.SUSPENDED


def test_another_trigger_card_asks_for_a_second_pass(make_millennium_adapter, seos_ledger):
    """A new device gets a new pass, under the next id the ledger hands out."""
    _record("seos", 2, "74")
    creds = _creds(make_millennium_adapter, _card("1"), _card("74"))
    assert set(creds) == {"seos", "seos-2"}
    assert SeosLedger.get(PID, "seos-2")[0]["pending"] == "write"


def test_clearing_the_cards_forgets_every_credential(make_millennium_adapter, seos_ledger):
    """Not just the first.

    An entry left behind outlives the pass it described, and would suspend
    whatever the next trigger card earns.
    """
    _record("seos", 2, "74")
    _record("seos-card8200", 3, "90")
    # The operator deletes every trigger card; an unrelated badge remains.
    badge = _card("5", facility="45", card_format="Wiegand Card")
    assert _creds(make_millennium_adapter, badge) == {}
    assert SeosLedger.get(PID, "seos") == []
    assert SeosLedger.get(PID, "seos-card8200") == []


def test_an_empty_ledger_entry_does_not_claim_an_id(make_millennium_adapter, seos_ledger):
    """Seen on a live install: `11652:seos -> nothing`.

    Counted as a credential it holds "seos" while backing nothing, and the
    cardholder's own trigger card would then be read as asking for a second
    pass — one card, two passes.
    """
    SeosLedger.record(PID, "seos", [])
    assert set(_creds(make_millennium_adapter, _card("1"))) == {"seos"}
