"""Phase 2 pushes a PACS status to a pass only once it has read the pass's state.

It tells which side changed by comparing AccessGrid's state with the last
one recorded. With no state read — AccessGrid's pass list failed, or the
pass is not in it — that comparison has nothing to compare, and phase 2
used to treat the blank as "active": every tracked card that was off got a
suspend call, including passes already unlinked or suspended in AccessGrid.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase2_local_to_ag
from agsync.sync.snapshot import Snapshot


class _Ag:
    def __init__(self):
        self.calls = []

    @property
    def access_cards(self):
        return self

    def suspend(self, card_id):
        self.calls.append(("suspend", card_id))

    def resume(self, card_id):
        self.calls.append(("resume", card_id))

    def delete(self, card_id):
        self.calls.append(("delete", card_id))


def _snapshot(pacs_status, last_known, listed_state=None, trigger=True, listing_read=True):
    tracking.upsert(pacs_person_id="p", pacs_credential_id="t", ag_card_id="pass-p", full_name="Pat",
                    employee_id="p", status="active", last_known_ag_state=last_known, sync_ref="ref-p")
    snap = Snapshot()
    snap.ag_cards_read = listing_read
    snap.people = {"p": Person(id="p", full_name="Pat", email="pat@example.com")}
    snap.credentials_by_person = {"p": [Credential(id="t", person_id="p", card_number="1",
                                                   status=pacs_status, trigger_active=trigger)]}
    if listed_state is not None:
        snap.ag_cards_by_sync_ref["ref-p"] = [SimpleNamespace(id="half", state=listed_state)]
    return snap


@pytest.mark.parametrize("last_known", ["unlink", "suspended", "active"])
def test_an_unread_pass_list_suspends_nothing(last_known):
    ag = _Ag()
    phase2_local_to_ag.run(_snapshot(CredentialStatus.SUSPENDED, last_known, listing_read=False), ag)
    assert ag.calls == []


def test_a_pass_missing_from_a_read_list_is_not_suspended_blind():
    ag = _Ag()
    phase2_local_to_ag.run(_snapshot(CredentialStatus.SUSPENDED, "active"), ag)
    assert ag.calls == []


def test_a_pass_whose_state_was_read_is_still_suspended():
    ag = _Ag()
    phase2_local_to_ag.run(_snapshot(CredentialStatus.SUSPENDED, "active", listed_state="active"), ag)
    assert ag.calls == [("suspend", "pass-p")]


def test_a_removed_trigger_still_deletes_the_pass_when_the_list_was_not_read():
    """That one comes from the PACS alone and needs no state from AccessGrid."""
    ag = _Ag()
    phase2_local_to_ag.run(_snapshot(CredentialStatus.ACTIVE, "active", trigger=False, listing_read=False), ag)
    assert ag.calls == [("delete", "pass-p")]
