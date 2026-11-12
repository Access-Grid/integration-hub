"""A change made in AccessGrid survives phases 1, 2 and 4 run in engine order.

Phases 2 and 4 tell which side changed by comparing AccessGrid's state with
the last one recorded. Phase 1 used to record it first, for every tracked
pass, so phase 4 saw no change to send to the PACS and phase 2 pushed the
PACS's old state back over AccessGrid's: a suspend was resumed, a resume
suspended, and an unlink never reached the PACS. Every other test ran
phase 4 alone, which is how it went unnoticed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase1_provision, phase2_local_to_ag, phase4_ag_to_local
from agsync.sync.snapshot import Snapshot

ACTIVE, SUSPENDED = CredentialStatus.ACTIVE, CredentialStatus.SUSPENDED


class _Ag:
    """AccessGrid holding one pass; records what the phases ask of it."""

    def __init__(self, card):
        self.card, self.calls = card, []

    @property
    def access_cards(self):
        return self

    def get(self, card_id):
        return self.card

    def suspend(self, card_id):
        self.calls.append(("suspend", card_id))

    def resume(self, card_id):
        self.calls.append(("resume", card_id))

    def delete(self, card_id):
        self.calls.append(("delete", card_id))


class _Pacs:
    """A PACS whose card the customer supplies (DESFire): status written back."""

    supports_status_writeback = True
    supports_credential_writeback = False

    def __init__(self):
        self.writes = []

    def update_credential_status(self, pid, cid, status):
        self.writes.append(status)
        return True


def _cycle(last_known: str, ag_state: str, pacs_status: CredentialStatus, allocate: bool = False):
    """Phases 1, 2, 4 in engine order over one tracked pass: (AG calls, PACS writes)."""
    tracking.upsert(
        pacs_person_id="p1", pacs_credential_id="t1", ag_card_id="pass-1",
        full_name="Ada Lovelace", employee_id="p1", status="active",
        last_known_ag_state=last_known, sync_ref="ref-1",
    )
    card = SimpleNamespace(id="pass-1", state=ag_state, metadata={"sync_ref": "ref-1"}, devices=[])
    snap = Snapshot()
    snap.people = {"p1": Person(id="p1", full_name="Ada Lovelace", email="ada@example.com")}
    snap.credentials_by_person = {"p1": [Credential(
        id="t1", person_id="p1", card_number="5001", status=pacs_status,
        trigger_active=True, allocate_identity=allocate,
    )]}
    snap.ag_card_by_id["pass-1"] = card
    snap.ag_cards_by_sync_ref["ref-1"] = [card]
    ag, pacs = _Ag(card), _Pacs()
    phase1_provision.run(snap, ag, "tpl")
    phase2_local_to_ag.run(snap, ag)
    phase4_ag_to_local.run(snap, pacs, ag)
    return ag.calls, pacs.writes


@pytest.mark.parametrize(("ag_state", "pacs_was", "pacs_becomes"), [
    ("suspended", ACTIVE, SUSPENDED),
    ("unlinked", ACTIVE, SUSPENDED),
])
def test_an_accessgrid_switch_off_reaches_the_pacs_and_stays(ag_state, pacs_was, pacs_becomes):
    calls, writes = _cycle("active", ag_state, pacs_was)
    assert calls == [], "phase 2 must not undo a change made in AccessGrid"
    assert writes == [pacs_becomes]
    assert tracking.get("p1", "t1").last_known_ag_state == ag_state


def test_an_accessgrid_resume_reaches_the_pacs_and_stays():
    calls, writes = _cycle("suspended", "active", SUSPENDED)
    assert calls == [], "phase 2 must not suspend a pass AccessGrid just resumed"
    assert writes == [ACTIVE]


def test_an_accessgrid_suspend_of_a_seos_pass_is_not_resumed():
    # The install gate, not this loop, writes a Seos card; phase 2 must
    # still leave AccessGrid's suspend alone.
    calls, _ = _cycle("active", "suspended", ACTIVE, allocate=True)
    assert calls == []
    assert tracking.get("p1", "t1").last_known_ag_state == "suspended"

