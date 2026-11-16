"""Unity: a token past its end date suspends its pass; moving the date on resumes it.

Phase 3 deleted the pass of an expired card (and for Seos the tokens the
hub wrote), so extending the date issued a brand-new pass: a new invite and
a new install. For Unity an expired card now suspends the same pass, as the
spec has it for any card marked inactive. Other PACS still delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.lib.pacs.avigilon.adapter import AvigilonAdapter
from agsync.sync import tracking
from agsync.sync.phases import phase2_local_to_ag, phase3_deletions
from agsync.sync.snapshot import Snapshot

PAST = datetime.now(UTC) - timedelta(days=1)
FUTURE = datetime.now(UTC) + timedelta(days=365)
ACTIVE, AWAITING = CredentialStatus.ACTIVE, CredentialStatus.AWAITING_INSTALL


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


def _cycle(*, ends, ag_state, status=ACTIVE, row="active", allocate=False, suspends_expired=True):
    """Phases 2 and 3 over one tracked pass: (AG calls, row status after)."""
    tracking.upsert(pacs_person_id="p", pacs_credential_id="t", ag_card_id="pass-p", full_name="Pat",
                    employee_id="p", status=row, last_known_ag_state=ag_state, sync_ref="ref-p")
    snap = Snapshot(suspends_expired=suspends_expired)
    snap.people = {"p": Person(id="p", full_name="Pat", email="pat@example.com")}
    snap.credentials_by_person = {"p": [Credential(id="t", person_id="p", card_number="" if allocate else "1",
                                                   status=status, deactivate_date=ends, trigger_active=True,
                                                   allocate_identity=allocate)]}
    snap.ag_cards_by_sync_ref["ref-p"] = [SimpleNamespace(id="half", state=ag_state)]
    ag = _Ag()
    phase2_local_to_ag.run(snap, ag)
    phase3_deletions.run(snap, ag)
    row = tracking.get("p", "t")
    return ag.calls, row.status if row else None


def test_unity_says_its_expired_cards_suspend():
    assert AvigilonAdapter("unity", "u", "p").suspends_expired_cards


@pytest.mark.parametrize("allocate", [False, True])
def test_an_expired_card_suspends_its_pass_and_deletes_nothing(allocate):
    calls, status = _cycle(ends=PAST, ag_state="active", allocate=allocate)
    assert calls == [("suspend", "pass-p")]
    assert status == "expired"


def test_an_expired_seos_card_suspends_a_pass_nobody_installed():
    calls, status = _cycle(ends=PAST, ag_state="created", status=AWAITING, allocate=True)
    assert calls == [("suspend", "pass-p")] and status == "expired"


@pytest.mark.parametrize(("status", "allocate"), [(ACTIVE, False), (ACTIVE, True), (AWAITING, True)])
def test_moving_the_date_on_resumes_the_same_pass(status, allocate):
    """AWAITING: a Seos card the hub switched off along with its pass."""
    calls, row = _cycle(ends=FUTURE, ag_state="suspended", status=status, row="expired", allocate=allocate)
    assert calls == [("resume", "pass-p")] and row == "active"


def test_a_seos_pass_suspended_in_accessgrid_is_still_not_resumed():
    """Not expired: the hub switched its card off because AccessGrid suspended it."""
    calls, _ = _cycle(ends=FUTURE, ag_state="suspended", status=AWAITING, row="suspended", allocate=True)
    assert calls == []


def test_an_expired_and_already_suspended_pass_is_left_alone():
    calls, status = _cycle(ends=PAST, ag_state="suspended", row="expired")
    assert calls == [] and status == "expired"


def test_a_pacs_whose_expired_cards_delete_still_deletes():
    calls, status = _cycle(ends=PAST, ag_state="active", suspends_expired=False)
    assert ("delete", "pass-p") in calls and status == "deleted"
