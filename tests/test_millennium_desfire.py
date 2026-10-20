"""DESFire through the same issuance table as Seos.

In DESFire the card already exists in Millennium and its identity is
copied out to AccessGrid, so a new pass needs no write and no free slot.
"""

from __future__ import annotations

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import PacsCards
from agsync.sync import tracking
from tests._fakes import FakeMillenniumClient, export_row

PID = "33137"


def _card(number, active=True, card_format="HID 37"):
    return {"card_number": number, "facility_code": "66", "card_format": card_format,
            "active": active}


def _creds(make_millennium_adapter, *slots) -> dict:
    client = FakeMillenniumClient(rows=[export_row(PID, slots=slots)])
    adapter = make_millennium_adapter(client)  # DESFire is the default mode
    list(adapter.list_people())
    return {c.id: c for c in adapter.list_credentials(PID)}


def test_a_new_card_is_a_pass_carrying_its_own_identity(make_millennium_adapter):
    [cred] = _creds(make_millennium_adapter, _card("5001", active=False)).values()
    assert (cred.id, cred.site_code, cred.card_number) == ("card", "66", "5001")
    assert cred.allocate_identity is False
    assert cred.status is CredentialStatus.SUSPENDED  # its own Active box
    assert "pending" not in PacsCards.get(PID, "card")[0], "nothing to write"


def test_a_card_that_moves_slot_keeps_its_pass(make_millennium_adapter):
    """Keyed by slot, this read as a new pass; recorded, it is followed."""
    _creds(make_millennium_adapter, _card("5001"))
    assert set(_creds(make_millennium_adapter, None, None, _card("5001"))) == {"card"}


def test_a_deleted_card_reports_no_pass_and_is_forgotten(make_millennium_adapter):
    """Its pass is revoked by phase 3, as before."""
    _creds(make_millennium_adapter, _card("5001"))
    assert _creds(make_millennium_adapter) == {}
    assert PacsCards.all() == {}


def test_a_different_card_in_a_recorded_slot_holds_the_pass(make_millennium_adapter):
    _creds(make_millennium_adapter, _card("5001"))
    creds = _creds(make_millennium_adapter, _card("9999"))
    assert creds["card"].hold is True


def test_a_pass_tracked_by_slot_is_adopted_under_its_id(make_millennium_adapter):
    """Installs from before pacs_cards keyed DESFire passes "slot1", "slot2"."""
    tracking.upsert(
        pacs_person_id=PID, pacs_credential_id="slot2", ag_card_id="pass-1",
        full_name="x", employee_id=PID, status="active",
    )
    creds = _creds(make_millennium_adapter, None, _card("5001"))
    assert set(creds) == {"slot2"}, "the same pass, not a new one"
    assert PacsCards.get(PID, "slot2")[0]["card_number"] == "5001"
