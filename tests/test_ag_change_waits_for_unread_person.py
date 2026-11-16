"""An AccessGrid suspend for a cardholder the PACS could not read this cycle
waits for a cycle that reads them, rather than being recorded as seen and
never written to the PACS."""

from __future__ import annotations

from types import SimpleNamespace

from agsync.lib.pacs import Credential, CredentialStatus
from agsync.sync import tracking
from agsync.sync.phases import phase4_ag_to_local as phase4
from agsync.sync.snapshot import Snapshot


class _Pacs:
    supports_status_writeback = True

    def __init__(self):
        self.writes = []

    def update_credential_status(self, person_id, credential_id, status):
        self.writes.append((person_id, credential_id, status))
        return True


def _snapshot(read: bool) -> Snapshot:
    snap = Snapshot()
    snap.people = {"p1": SimpleNamespace(id="p1", full_name="Ada Lovelace")}
    if read:
        snap.credentials_by_person["p1"] = [Credential(
            id="t1", person_id="p1", card_number="5001",
            status=CredentialStatus.ACTIVE, trigger_active=True,
        )]
    snap.ag_cards_by_sync_ref["ref-1"] = [SimpleNamespace(id="pass-1", state="suspended")]
    return snap


def test_suspend_for_an_unread_person_waits_then_reaches_the_pacs():
    tracking.upsert(
        pacs_person_id="p1", pacs_credential_id="t1", ag_card_id="pass-1",
        full_name="Ada Lovelace", employee_id="p1", status="active",
        last_known_ag_state="active", sync_ref="ref-1",
    )
    pacs = _Pacs()

    phase4.run(_snapshot(read=False), pacs)
    assert pacs.writes == []
    assert tracking.get("p1", "t1").last_known_ag_state == "active", "still pending"

    phase4.run(_snapshot(read=True), pacs)
    assert pacs.writes == [("p1", "t1", CredentialStatus.SUSPENDED)]
    assert tracking.get("p1", "t1").last_known_ag_state == "suspended"
