"""Revoking a Seos pass when its card is removed from the PACS.

Deleting a written card in Millennium is a revocation, and it has to reach
AccessGrid. The route is the ordinary one: the credential reports itself
suspended, and phase 2 pushes that to AccessGrid off the tracking table.
These tests pin the decision, including the case AccessGrid cannot express.
"""

from __future__ import annotations

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
OTHER = "1"
ROSTER = [{"ID": 11587, "IsActive": False, "Name": "Grid, Accessg"}]


def _client(pages):
    return FakeMillenniumClient(roster=ROSTER, pages=pages)


def _page(millennium_page, set_slot, *, slot2=None, slot3=None):
    """A cardholder with a trigger card in slot 1, plus whatever we wrote."""
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1234",
        facility_code="66", card_format=TRIGGER, active=True,
    )
    for index, spec in ((2, slot2), (3, slot3)):
        if spec is None:
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        else:
            number, active = spec
            page = set_slot(
                page, index, card_id=f"79{index}0", card_number=number,
                facility_code="66", card_format=TRIGGER, active=active,
            )
    return page


def _status(adapter):
    list(adapter.list_people())
    creds = list(adapter.list_credentials("11587"))
    assert len(creds) == 1
    return creds[0].status


def _ledger(*numbers, slots=(2, 3)):
    SeosLedger.record("11587", "seos-slot1", [
        {"slot": slot, "card_number": n, "facility_code": "66"}
        for slot, n in zip(slots, numbers, strict=False)
    ])


# --- one device ----------------------------------------------------------


def test_present_and_active_means_active(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    _ledger("5001")
    page = _page(millennium_page, set_slot, slot2=("5001", True))
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.ACTIVE


def test_deleting_the_card_suspends_the_pass(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    # The revocation that has to reach AccessGrid.
    _ledger("5001")
    page = _page(millennium_page, set_slot)  # slot 2 emptied
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.SUSPENDED


def test_deactivating_the_card_suspends_the_pass(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    _ledger("5001")
    page = _page(millennium_page, set_slot, slot2=("5001", False))
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.SUSPENDED


def test_a_different_card_in_the_same_slot_does_not_count(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Slot 2 is occupied and active — but by somebody else's card.

    Reading the slot rather than the card would report the pass healthy while
    the credential behind it no longer exists anywhere.
    """
    _ledger("5001")
    page = _page(millennium_page, set_slot, slot2=("9999", True))
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.SUSPENDED


# --- two devices, which AccessGrid cannot revoke separately -------------


def test_both_present_means_active(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    _ledger("5001", "5002")
    page = _page(millennium_page, set_slot, slot2=("5001", True), slot3=("5002", True))
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.ACTIVE


def test_deleting_one_of_two_suspends_the_whole_pass(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """AccessGrid suspends a pass, not one device on it.

    So a holder whose watch credential was deleted cannot be half-revoked.
    Access control fails closed: the operator's deletion is honoured and the
    pass is suspended, rather than the phone quietly continuing to work.
    """
    _ledger("5001", "5002")
    page = _page(millennium_page, set_slot, slot2=("5001", True))  # 5002 deleted
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.SUSPENDED


def test_a_card_moved_to_another_slot_is_still_recognised(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    # Operators move cards between slots; that is not a revocation.
    _ledger("5001", slots=(2,))
    page = _page(millennium_page, set_slot, slot3=("5001", True))
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.ACTIVE


def test_suspending_follows_the_card_not_the_old_slot_number(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    _ledger("5001", slots=(2,))
    page = _page(millennium_page, set_slot, slot3=("5001", True))
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)

    assert adapter.update_credential_status(
        "11587", "seos-slot1", CredentialStatus.SUSPENDED,
    )
    _, body = client.saved[0][1].to_multipart()
    assert b'name="Card_3_Active"' not in body     # the card's current slot
    assert b'name="Card_1_Active"\r\n\r\ntrue\r\n' in body  # trigger untouched


def test_before_anything_is_written_the_pass_is_active(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    # Nothing in the ledger yet: the pass has just been issued and phase 1
    # is about to write it in. It must not read as revoked.
    page = _page(millennium_page, set_slot)
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _status(adapter) is CredentialStatus.ACTIVE
