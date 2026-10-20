"""Suspending a pass in AccessGrid switches its Millennium card off.

End to end, with only the network faked: a tracked pass AccessGrid reports
suspended, phase 4 deciding what Millennium should show, the real
Millennium adapter recording it, and the end-of-cycle import sending it.
Seos reaches the adapter through the install gate; DESFire through the
ordinary status write-back. Both must end in the same import.
"""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest

from agsync.lib.pacs.millennium_ultra.adapter import MODE_DESFIRE, MODE_SEOS, PacsCards
from agsync.lib.pacs.millennium_ultra.import_file import ImportLog
from agsync.sync import tracking
from agsync.sync.phases import phase4_ag_to_local as phase4
from agsync.sync.snapshot import Snapshot
from tests._fakes import FakeMillenniumClient, export_row

PID = "33137"
CARD = {"card_number": "5001", "facility_code": "66", "card_format": "HID 37", "active": True}


class _Ag:
    """AccessGrid holding the one pass, in whatever state the test gives it."""

    def __init__(self, card):
        self._card = card

    @property
    def access_cards(self):
        return self

    def get(self, card_id):
        return self._card


def _pass(state):
    installed = [{"id": "d1", "device_type": "iphone", "status": "installed",
                  "site_code": "66", "card_number": "5001"}]
    return SimpleNamespace(
        id="pass-1", state=state, site_code="66", card_number="5001",
        details=None, expiration_date=None, devices=installed,
    )


@pytest.fixture
def cycle(make_millennium_adapter):
    """One cycle for a cardholder whose pass `card` rests on 66/5001 in slot 1."""
    def _run(mode, ag_state):
        client = FakeMillenniumClient(rows=[export_row(PID, "Ada", "Lovelace", slots=[CARD])])
        client.imports = []

        def import_cardholders(imported):
            client.imports.append(imported)
            return ImportLog({}, [], [])

        client.import_cardholders = import_cardholders
        adapter = make_millennium_adapter(client, mode=mode)
        PacsCards.record(PID, "card", [
            {"slot": 1, "card_number": "5001", "facility_code": "66", "active": True},
        ])
        tracking.upsert(
            pacs_person_id=PID, pacs_credential_id="card", ag_card_id="pass-1",
            full_name="Ada Lovelace", employee_id=PID, status="active",
            last_known_ag_state="active", sync_ref="ref-1",
        )

        snap = Snapshot()
        snap.people = {p.id: p for p in adapter.list_people()}
        snap.credentials_by_person[PID] = list(adapter.list_credentials(PID))
        card = _pass(ag_state)
        snap.ag_cards_by_sync_ref["ref-1"] = [card]

        phase4.run(snap, adapter, _Ag(card))
        adapter.finish_cycle()
        return client.imports

    return _run


def _sent(imports) -> list[dict[str, str]]:
    return [row for imported in imports for row in csv.DictReader(io.StringIO(imported.csv))]


@pytest.mark.parametrize("mode", [MODE_SEOS, MODE_DESFIRE])
def test_a_suspended_pass_switches_its_card_off(cycle, mode):
    [row] = _sent(cycle(mode, "suspended"))
    assert row["EmployeeID"] == PID
    assert (row["Card1EncodedCardNo."], row["Card1FacilityCode"]) == ("5001", "66")
    assert row["Card1Active"] == "False"
    assert PacsCards.get(PID, "card")[0]["active"] is False, "and it is recorded as off"


@pytest.mark.parametrize("mode", [MODE_SEOS, MODE_DESFIRE])
def test_an_active_pass_sends_nothing(cycle, mode):
    """The card is already on, so there is nothing to import."""
    assert cycle(mode, "active") == []
