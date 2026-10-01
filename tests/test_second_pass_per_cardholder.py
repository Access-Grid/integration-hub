"""A second Seos pass for a cardholder who already has one.

Wallet will not add a credential to a watch after the pass is installed, and
the install link cannot be replayed — HID's issue codes are single use. So
the only way to reach a holder's watch, once they have installed on their
phone, is a second pass they install on the watch alone, while the phone's
keeps working untouched.

A cardholder used to have exactly one Seos credential, keyed `seos`. They can
now have one per pass. The first keeps that id, so nothing about the ordinary
case changes; a second is keyed on the Millennium CardID of the card that
asked for it. The slot cannot serve — cards move, and a moved id reads as a
new credential, which is the bug migration 005 was written to stop — and nor
can the card number, because provisioning overwrites it. The CardID survives
both: it is deliberately preserved when a marker is overwritten.
"""

from __future__ import annotations

import logging

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import (
    MAX_SEOS_CREDENTIALS,
    MODE_SEOS,
    SeosLedger,
)
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11665"
ROSTER = [{"ID": PID, "IsActive": True, "Name": "Dorvil, Greg"}]


def _page(millennium_page, set_slot, slots):
    """slots maps index -> (card_id, number, facility, active, format) or None."""
    page = millennium_page
    for index in (1, 2, 3):
        spec = slots.get(index)
        if spec is None:
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        else:
            card_id, number, facility, active = spec[:4]
            fmt = spec[4] if len(spec) > 4 else TRIGGER
            page = set_slot(
                page, index, card_id=card_id, card_number=number,
                facility_code=facility, card_format=fmt, active=active,
            )
    return page


def _adapter(make_millennium_adapter, millennium_page, set_slot, slots):
    client = FakeMillenniumClient(
        roster=ROSTER, pages={PID: _page(millennium_page, set_slot, slots)}
    )
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER
    )
    list(adapter.list_people())
    return adapter, client


def _creds(adapter) -> dict:
    return {c.id: c for c in adapter.list_credentials(PID)}


def _pass_a():
    SeosLedger.record(PID, "seos", [
        {"slot": 2, "card_number": "74", "facility_code": "99"},
    ])


# Pass A installed in slot 2; slots 1 and 3 free. Taken from a live cardholder.
PASS_A_ONLY = {1: None, 2: ("8100", "74", "99", True), 3: None}


# =====================================================================
# What a cardholder has, and what they are asking for
# =====================================================================


def test_a_cardholder_with_one_pass_has_one_credential(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    _pass_a()
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, PASS_A_ONLY)

    assert set(_creds(adapter)) == {"seos"}


def test_a_new_trigger_card_asks_for_a_second_pass(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The regression this exists for."""
    _pass_a()
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        **PASS_A_ONLY, 3: ("8200", "9", "99", True),
    })

    creds = _creds(adapter)
    assert set(creds) == {"seos", "seos-card8200"}
    assert creds["seos"].status is CredentialStatus.ACTIVE


def test_a_cardholders_first_pass_keeps_the_bare_id(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """So the ordinary case never depends on a CardID staying put.

    Only a second pass is keyed on one. If CardIDs turned out to move, that
    would cost a duplicate pass for the handful of cardholders with two,
    rather than for every cardholder on the install.
    """
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: ("7919", "1", "99", True), 2: None, 3: None,
    })

    assert set(_creds(adapter)) == {"seos"}


def test_a_card_of_ours_is_never_read_as_a_request(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Our cards carry the trigger format, so only the ledger can tell."""
    _pass_a()
    SeosLedger.record(PID, "seos-card8200", [
        {"slot": 3, "card_number": "90", "facility_code": "99"},
    ])
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: None, 2: ("8100", "74", "99", True), 3: ("8200", "90", "99", True),
    })

    assert set(_creds(adapter)) == {"seos", "seos-card8200"}


# =====================================================================
# Issuing it without disturbing the first
# =====================================================================


def test_the_first_passes_card_survives_the_second_being_issued(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The requirement: the working phone pass is untouched."""
    _pass_a()
    adapter, client = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        **PASS_A_ONLY, 3: ("8200", "9", "99", True),
    })

    assert adapter.write_back_credentials(PID, "seos-card8200", [
        CredentialIdentity("99", "90"), CredentialIdentity("99", "91"),
    ]) is True

    form = client.saved[-1][1]
    assert form.value("Card_2_EncodedCardNumber") == "74", "pass A untouched"
    assert form.value("Card_2_CardID") == "8100"
    # The marker is consumed, keeping its CardID so the id stays put, and the
    # empty slot takes the other half of the pair.
    assert form.value("Card_3_EncodedCardNumber") == "90"
    assert form.value("Card_3_CardID") == "8200"
    assert form.value("Card_1_EncodedCardNumber") == "91"


def test_each_pass_keeps_its_own_record(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    _pass_a()
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        **PASS_A_ONLY, 3: ("8200", "9", "99", True),
    })
    adapter.write_back_credentials(
        PID, "seos-card8200", [CredentialIdentity("99", "90")]
    )

    assert [e["card_number"] for e in SeosLedger.get(PID, "seos")] == ["74"]
    assert [e["card_number"] for e in SeosLedger.get(PID, "seos-card8200")] == ["90"]


def test_a_second_pass_is_suspended_on_its_own_cards_only(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """One pass going bad must not take the other with it."""
    _pass_a()
    SeosLedger.record(PID, "seos-card8200", [
        {"slot": 3, "card_number": "90", "facility_code": "99"},
    ])
    # B's card is present but switched off; A's is fine.
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: None, 2: ("8100", "74", "99", True), 3: ("8200", "90", "99", False),
    })

    creds = _creds(adapter)
    assert creds["seos"].status is CredentialStatus.ACTIVE
    assert creds["seos-card8200"].status is CredentialStatus.SUSPENDED


def test_the_status_write_finds_the_right_slots(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Deactivating one pass must not switch off the other's card."""
    _pass_a()
    SeosLedger.record(PID, "seos-card8200", [
        {"slot": 3, "card_number": "90", "facility_code": "99"},
    ])
    adapter, client = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: None, 2: ("8100", "74", "99", True), 3: ("8200", "90", "99", True),
    })

    assert adapter.update_credential_status(
        PID, "seos-card8200", CredentialStatus.AWAITING_INSTALL
    ) is True

    form = client.saved[-1][1]
    assert form.is_checked("Card_3_Active") is False, "B's card goes off"
    assert form.is_checked("Card_2_Active") is True, "A's card stays on"


# =====================================================================
# Limits
# =====================================================================


def test_a_third_pass_is_refused(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    """Three slots, and a pass can hold two cards. The cap is what stops an
    id that moves issuing a pass per cycle."""
    _pass_a()
    SeosLedger.record(PID, "seos-card8200", [
        {"slot": 3, "card_number": "90", "facility_code": "99"},
    ])
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: ("8300", "7", "99", True),   # a third trigger card
        2: ("8100", "74", "99", True),
        3: ("8200", "90", "99", True),
    })

    with caplog.at_level(logging.INFO):
        creds = _creds(adapter)

    assert len(creds) == MAX_SEOS_CREDENTIALS
    assert "seos-card8300" not in creds
    assert "already has 2 Seos pass(es)" in "\n".join(
        r.getMessage() for r in caplog.records
    )


def test_a_card_with_no_card_id_cannot_key_a_second_pass(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    """Millennium assigns a CardID on save, so this is a card mid-creation."""
    _pass_a()
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        **PASS_A_ONLY, 3: ("", "9", "99", True),
    })

    with caplog.at_level(logging.INFO):
        creds = _creds(adapter)

    assert set(creds) == {"seos"}
    assert "no CardID" in "\n".join(r.getMessage() for r in caplog.records)


def test_a_second_pass_needs_a_slot_of_its_own(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Its marker is one position; the pair needs another."""
    _pass_a()
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: ("8300", "5", "45", True, "1"),   # the cardholder's own badge
        2: ("8100", "74", "99", True),
        3: ("8200", "9", "99", True),        # the marker, no empty slot left
    })

    assert set(_creds(adapter)) == {"seos"}


def test_clearing_the_cards_forgets_every_credential(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Not just the first.

    An entry left behind outlives the pass it described, and would suspend
    whatever the next trigger card earns — the thing the forget exists to
    prevent, reintroduced for the second pass by looking at only one id.
    """
    _pass_a()
    SeosLedger.record(PID, "seos-card8200", [
        {"slot": 3, "card_number": "90", "facility_code": "99"},
    ])
    # The operator deletes every format-8 card; an unrelated badge remains.
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: ("8300", "5", "45", True, "1"), 2: None, 3: None,
    })

    assert _creds(adapter) == {}
    assert SeosLedger.get(PID, "seos") == []
    assert SeosLedger.get(PID, "seos-card8200") == []


def test_an_empty_ledger_entry_does_not_claim_an_id(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Seen on a live install: `11652:seos -> nothing`.

    `retire_credentials` re-records the list after removing a card, so a
    credential whose last card AccessGrid deleted leaves an empty entry.
    Counted as a credential it holds "seos" while backing nothing, and the
    cardholder's own trigger card is then read as asking for a second pass —
    one card, two passes, two licences.
    """
    SeosLedger.record(PID, "seos", [])
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot, {
        1: ("7919", "1", "99", True), 2: None, 3: None,
    })

    assert set(_creds(adapter)) == {"seos"}
