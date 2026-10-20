"""Revoking a Seos pass when its card is removed from the PACS.

Deleting a written card in Millennium is a revocation, and it has to reach
AccessGrid. The route is the ordinary one: the credential reports itself
suspended, and phase 2 pushes that to AccessGrid off the tracking table.
These tests pin the decision, including the case AccessGrid cannot express.
"""

from __future__ import annotations

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient, export_row

TRIGGER = "HID 37"
OTHER = "Wiegand Card"
PID = "33137"


def _card(number, active=True, card_format=TRIGGER):
    return {
        "card_number": number, "facility_code": "66",
        "card_format": card_format, "active": active,
    }


def _status(make_millennium_adapter, *, slot2=None, slot3=None, marker=False, cid="seos"):
    """The `cid` credential's status for a cardholder holding these cards.

    Slot 1 is the cardholder's own badge, in another format unless `marker`.
    A trigger-format card we did not write is somebody asking for a pass.
    """
    slot1 = _card("1234", card_format=TRIGGER if marker else OTHER)
    client = FakeMillenniumClient(rows=[export_row(PID, slots=[slot1, slot2, slot3])])
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    list(adapter.list_people())
    creds = {c.id: c for c in adapter.list_credentials(PID)}
    assert cid in creds, f"no {cid} credential in {sorted(creds)}"
    return creds[cid].status


def _ledger(*numbers, slots=(2, 3)):
    SeosLedger.record(PID, "seos", [
        {"slot": slot, "card_number": n, "facility_code": "66"}
        for slot, n in zip(slots, numbers, strict=False)
    ])


# --- one device ----------------------------------------------------------


def test_present_and_active_means_active(make_millennium_adapter, seos_ledger):
    _ledger("5001")
    assert _status(make_millennium_adapter, slot2=_card("5001")) is CredentialStatus.ACTIVE


def test_deleting_the_card_suspends_the_pass(make_millennium_adapter, seos_ledger):
    # The revocation that has to reach AccessGrid. Two cards were written,
    # so one being deleted leaves the credential present but broken — with
    # none of them left there is no trigger card at all, which phase 3 reads
    # as the credential being gone and deletes the pass outright.
    _ledger("5001", "5002")
    status = _status(make_millennium_adapter, slot3=_card("5002"))  # 5001 deleted
    assert status is CredentialStatus.SUSPENDED


def test_deactivating_the_card_suspends_the_pass(make_millennium_adapter, seos_ledger):
    _ledger("5001")
    status = _status(make_millennium_adapter, slot2=_card("5001", active=False))
    assert status is CredentialStatus.SUSPENDED


def test_a_different_card_in_the_same_slot_does_not_count(
    make_millennium_adapter, seos_ledger
):
    """Slot 2 is occupied and active — but by somebody else's card.

    Reading the slot rather than the card would report the pass healthy while
    the credential behind it no longer exists anywhere.
    """
    _ledger("5001")
    assert _status(make_millennium_adapter, slot2=_card("9999")) is CredentialStatus.SUSPENDED


# --- two devices, which AccessGrid cannot revoke separately -------------


def test_both_present_means_active(make_millennium_adapter, seos_ledger):
    _ledger("5001", "5002")
    status = _status(make_millennium_adapter, slot2=_card("5001"), slot3=_card("5002"))
    assert status is CredentialStatus.ACTIVE


def test_deleting_one_of_two_suspends_the_whole_pass(make_millennium_adapter, seos_ledger):
    """AccessGrid suspends a pass, not one device on it.

    So a holder whose watch credential was deleted cannot be half-revoked.
    Access control fails closed: the operator's deletion is honoured and the
    pass is suspended, rather than the phone quietly continuing to work.
    """
    _ledger("5001", "5002")
    status = _status(make_millennium_adapter, slot2=_card("5001"))  # 5002 deleted
    assert status is CredentialStatus.SUSPENDED


def test_a_card_moved_to_another_slot_is_still_recognised(
    make_millennium_adapter, seos_ledger
):
    # Operators move cards between slots; that is not a revocation.
    _ledger("5001", slots=(2,))
    assert _status(make_millennium_adapter, slot3=_card("5001")) is CredentialStatus.ACTIVE


def test_before_anything_is_written_the_pass_is_active(make_millennium_adapter, seos_ledger):
    # Nothing in the ledger yet: the pass has just been issued and its cards
    # are about to be written in. It must not read as revoked.
    assert _status(make_millennium_adapter, marker=True, cid="card") is CredentialStatus.ACTIVE
