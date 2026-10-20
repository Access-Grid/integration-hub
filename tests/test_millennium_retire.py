"""Deleting a Seos card from Millennium once AccessGrid deletes or unlinks it.

retire_credentials only marks the card; finish_cycle deletes it. DESFire
cards are the customer's own badges and are never deleted.
"""

from __future__ import annotations

from types import SimpleNamespace

from agsync.lib.pacs.base import CredentialIdentity
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, PacsCards
from agsync.sync.phases.writeback import deleted_identities_from_cards
from tests._fakes import FakeMillenniumClient, export_row

PID = "33137"
CARD = {"card_number": "5001", "facility_code": "66", "card_format": "HID 37"}
GONE = [CredentialIdentity("66", "5001")]


def _adapter(make_millennium_adapter, *slots, mode=MODE_SEOS, delete_result=True):
    client = FakeMillenniumClient(rows=[export_row(PID, slots=slots)])
    client.deleted = []

    def delete_card_by_number(employee_id, facility_code, card_number):
        client.deleted.append((employee_id, facility_code, card_number))
        return delete_result

    client.delete_card_by_number = delete_card_by_number
    adapter = make_millennium_adapter(client, mode=mode)
    PacsCards.record(PID, "card", [{"slot": 1, "card_number": "5001", "facility_code": "66"}])
    list(adapter.list_people())
    return adapter, client


def test_a_retired_card_is_deleted_at_the_end_of_the_cycle(make_millennium_adapter):
    adapter, client = _adapter(make_millennium_adapter, CARD)
    assert adapter.retire_credentials(PID, "card", GONE) == 1
    assert client.deleted == [], "nothing is deleted during the phases"
    adapter.finish_cycle()
    assert client.deleted == [(PID, "66", "5001")]
    assert PacsCards.get(PID, "card") == []


def test_a_card_already_gone_is_forgotten_without_a_request(make_millennium_adapter):
    adapter, client = _adapter(make_millennium_adapter)  # no card on the cardholder
    adapter.retire_credentials(PID, "card", GONE)
    adapter.finish_cycle()
    assert client.deleted == []
    assert PacsCards.get(PID, "card") == []


def test_a_refused_delete_is_tried_again(make_millennium_adapter):
    adapter, _ = _adapter(make_millennium_adapter, CARD, delete_result=False)
    adapter.retire_credentials(PID, "card", GONE)
    adapter.finish_cycle()
    assert PacsCards.get(PID, "card")[0]["pending"] == "delete"


def test_dry_run_deletes_nothing(make_millennium_adapter, monkeypatch):
    from agsync import dryrun

    adapter, client = _adapter(make_millennium_adapter, CARD)
    adapter.retire_credentials(PID, "card", GONE)
    monkeypatch.setattr(dryrun, "active", lambda: True)
    adapter.finish_cycle()
    assert client.deleted == []
    assert PacsCards.get(PID, "card")[0]["pending"] == "delete"


def test_a_dormant_card_is_left_alone(make_millennium_adapter):
    adapter, _ = _adapter(make_millennium_adapter, {**CARD, "card_format": "Wiegand Card"})
    adapter.list_credentials(PID)  # marks it dormant
    assert adapter.retire_credentials(PID, "card", GONE) == 0


def test_desfire_cards_are_never_retired(make_millennium_adapter):
    adapter, _ = _adapter(make_millennium_adapter, CARD, mode="desfire")
    assert adapter.supports_credential_retirement is False


def test_an_unlinked_card_counts_as_gone():
    card = SimpleNamespace(
        state="active", site_code="", card_number="", details=[
            SimpleNamespace(state="unlink", site_code="66", card_number="5001"),
        ],
    )
    assert [(i.site_code, i.card_number) for i in deleted_identities_from_cards([card])] == [
        ("66", "5001"),
    ]
