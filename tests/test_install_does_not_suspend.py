"""Installing a pass switches its card on; it never gets the pass suspended.

Seen live: a written card waits switched off for the install. The holder
installed, phase 1 recorded AccessGrid's new "active" state, and phase 2 then
read the waiting card as a suspension and suspended the pass before phase 4
could switch the card on.
"""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, PacsCards
from agsync.lib.pacs.millennium_ultra.import_file import ImportLog
from agsync.sync import tracking
from agsync.sync.phases import phase2_local_to_ag as phase2
from agsync.sync.phases import phase4_ag_to_local as phase4
from agsync.sync.snapshot import Snapshot
from tests._fakes import FakeMillenniumClient, export_row

PID = "33137"


class _Ag:
    def __init__(self, card):
        self._card = card
        self.calls: list[str] = []

    @property
    def access_cards(self):
        return self

    def get(self, card_id):
        return self._card

    def suspend(self, card_id):
        self.calls.append("suspend")

    def resume(self, card_id):
        self.calls.append("resume")


def _cycle(make_millennium_adapter, *, recorded_active, box_on=False):
    card = {"card_number": "1328", "facility_code": "2", "card_format": "HID 37", "active": box_on}
    client = FakeMillenniumClient(rows=[export_row(PID, "Ada", "Lovelace", slots=[None, card])])
    client.imports = []
    client.import_cardholders = lambda f: (client.imports.append(f), ImportLog({}, [], []))[1]
    adapter = make_millennium_adapter(client, mode=MODE_SEOS, pair=True)
    PacsCards.record(PID, "card", [
        {"slot": 2, "card_number": "1328", "facility_code": "2", "active": recorded_active},
    ])
    # As phase 1 leaves it on the cycle after the install.
    tracking.upsert(
        pacs_person_id=PID, pacs_credential_id="card", ag_card_id="pass-1",
        full_name="Ada Lovelace", employee_id=PID, status="active",
        last_known_ag_state="active", sync_ref="ref-1",
    )
    installed = SimpleNamespace(
        id="pass-1", state="active", site_code="2", card_number="1328", details=None,
        expiration_date=None,
        devices=[{"device_type": "iphone", "status": "installed", "site_code": "2", "card_number": "1328"}],
    )
    snap = Snapshot()
    snap.people = {p.id: p for p in adapter.list_people()}
    snap.credentials_by_person[PID] = list(adapter.list_credentials(PID))
    snap.ag_cards_by_sync_ref["ref-1"] = [installed]
    ag = _Ag(installed)
    phase2.run(snap, ag)
    phase4.run(snap, adapter, ag)
    adapter.finish_cycle()
    return snap.credentials_by_person[PID][0], ag, client


def test_installing_switches_the_card_on_and_never_suspends(make_millennium_adapter):
    cred, ag, client = _cycle(make_millennium_adapter, recorded_active=False)
    assert cred.status is CredentialStatus.AWAITING_INSTALL
    assert ag.calls == [], "the pass is not suspended"
    [imported] = client.imports
    row = next(csv.DictReader(io.StringIO(imported.csv)))
    assert row["Card2Active"] == "True"


def test_an_operator_switching_our_card_off_still_suspends(make_millennium_adapter):
    """Off after we had it on is somebody's decision, not our hold."""
    cred, ag, _ = _cycle(make_millennium_adapter, recorded_active=True)
    assert cred.status is CredentialStatus.SUSPENDED
    assert ag.calls == ["suspend"]
