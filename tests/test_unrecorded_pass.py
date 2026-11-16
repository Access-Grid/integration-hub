"""An issue whose reply was lost is never issued a second time.

AccessGrid can create a pass and the call still fail on our side: the
connection drops, or a proxy answers 502/504 while the server finishes.
The tracking row then has no pass id, and before this the next attempt —
phase 5 in the same cycle, phase 1 every cycle after — issued a second
pass, sent a second invite, and left the first beyond the sync's reach.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync import notifications
from agsync.ag import AccessGridError
from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase1_provision, phase5_retries
from agsync.sync.snapshot import Snapshot

PID, CID = "p1", "t1"


class _Ag:
    """AccessGrid whose issue call creates nothing, or fails, as told."""

    def __init__(self, fail=False):
        self.fail, self.issued = fail, []

    @property
    def access_cards(self):
        return self

    def provision(self, **params):
        self.issued.append(params)
        if self.fail:
            raise AccessGridError("API request failed: 502 Bad Gateway")
        return SimpleNamespace(id=f"pass-{len(self.issued)}", state="created")


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: sent.append((key, problem)))
    return sent


def _snapshot(*listed) -> Snapshot:
    snap = Snapshot()
    snap.people = {PID: Person(id=PID, full_name="Ada Lovelace", email="ada@example.com")}
    snap.credentials_by_person = {PID: [Credential(
        id=CID, person_id=PID, card_number="5001", status=CredentialStatus.ACTIVE, trigger_active=True,
    )]}
    for card in listed:
        snap.ag_card_by_id[card.id] = card
        snap.ag_cards_by_token.setdefault((PID, CID), []).append(card)
    return snap


def _listed(card_id, state="created"):
    return SimpleNamespace(id=card_id, state=state, employee_id=PID, metadata={"pacs_credential_id": CID})


def _cycle(snap, ag):
    """Phases 1 and 5, as the engine runs them."""
    phase1_provision.run(snap, ag, "tpl")
    phase5_retries.run(snap, ag, "tpl")


def test_a_failed_issue_is_not_retried_in_the_same_cycle(mailed):
    ag = _Ag(fail=True)
    _cycle(_snapshot(), ag)
    assert len(ag.issued) == 1, "phase 5 must wait for a listing that can show the first try"
    row = tracking.get(PID, CID)
    assert row.ag_card_id is None and row.sync_error


@pytest.mark.parametrize("halves", [["created"], ["created", "created"], ["active", "deleted"]])
def test_a_pass_the_lost_reply_created_is_not_issued_again(mailed, halves):
    # Next cycle: the first try's pass (or pair) is in the listing, unrecorded.
    _cycle(_snapshot(), _Ag(fail=True))
    ag = _Ag()
    snap = _snapshot(*[_listed(f"half-{i}", s) for i, s in enumerate(halves)])
    _cycle(snap, ag)
    assert ag.issued == []
    assert tracking.get(PID, CID).ag_card_id is None
    # Phases 1 and 5 each find it; one key, so the throttle mails it once.
    assert mailed and {key for key, _ in mailed} == {f"unrecorded:{PID}:{CID}"}
    problem = mailed[0][1]
    assert "half-0" in problem and "Delete" in problem


def test_once_that_pass_is_deleted_a_new_one_is_issued(mailed):
    ag = _Ag()
    _cycle(_snapshot(_listed("half-0", "deleted"), _listed("half-1", "deleted")), ag)
    assert len(ag.issued) == 1 and mailed == []
    assert tracking.get(PID, CID).ag_card_id == "pass-1"


def test_nothing_is_issued_when_the_pass_list_was_not_read(mailed):
    # Every check that a card is not issued twice reads the listing.
    _cycle(_snapshot(), _Ag(fail=True))
    snap = _snapshot()
    snap.ag_cards_read = False
    ag = _Ag()
    _cycle(snap, ag)
    assert ag.issued == []


def test_a_failed_list_marks_the_snapshot_unread(monkeypatch):
    from agsync.sync import snapshot as snapshot_mod

    def broken(*a):
        raise AccessGridError("Request failed: timed out")

    monkeypatch.setattr(snapshot_mod, "list_cards", broken)
    pacs = SimpleNamespace(list_people=lambda: [], list_credentials=lambda pid: [])
    assert snapshot_mod.build_snapshot(pacs, None, "tpl").ag_cards_read is False
