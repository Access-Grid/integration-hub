"""A pass suspended, unlinked or resumed in AccessGrid shows so in the tracking row.

Phase 4 recorded the new AccessGrid state, but the row's `status` — what
the status page shows — kept saying "active" for a pass AccessGrid had
suspended or its holder unlinked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase4_ag_to_local
from agsync.sync.snapshot import Snapshot


class _Pacs:
    supports_status_writeback = True
    supports_credential_writeback = False

    def update_credential_status(self, pid, cid, status):
        return True


def _phase4(row_status, last_known, ag_state, pacs_status, allocate=False):
    tracking.upsert(pacs_person_id="p", pacs_credential_id="t", ag_card_id="pass-p", full_name="Pat",
                    employee_id="p", status=row_status, last_known_ag_state=last_known, sync_ref="ref-p")
    snap = Snapshot()
    snap.people = {"p": Person(id="p", full_name="Pat", email="pat@example.com")}
    snap.credentials_by_person = {"p": [Credential(id="t", person_id="p", card_number="1", status=pacs_status,
                                                   trigger_active=True, allocate_identity=allocate)]}
    snap.ag_cards_by_sync_ref["ref-p"] = [SimpleNamespace(id="half", state=ag_state, devices=[])]
    phase4_ag_to_local.run(snap, _Pacs(), None)
    return tracking.get("p", "t")


@pytest.mark.parametrize("ag_state", ["suspended", "unlink", "unlinked"])
@pytest.mark.parametrize("allocate", [False, True])
def test_a_pass_switched_off_in_accessgrid_reads_suspended(ag_state, allocate):
    row = _phase4("active", "active", ag_state, CredentialStatus.ACTIVE, allocate)
    assert (row.status, row.last_known_ag_state) == ("suspended", ag_state)


@pytest.mark.parametrize("allocate", [False, True])
def test_a_pass_resumed_in_accessgrid_reads_active(allocate):
    row = _phase4("suspended", "suspended", "active", CredentialStatus.SUSPENDED, allocate)
    assert (row.status, row.last_known_ag_state) == ("active", "active")


def test_a_seos_pass_deleted_in_accessgrid_keeps_its_row_live_for_token_retirement():
    """Phase 3 deletes the hub's Unity tokens for it by the row's pass id."""
    row = _phase4("active", "active", "deleted", CredentialStatus.ACTIVE, allocate=True)
    assert (row.status, row.ag_card_id, row.last_known_ag_state) == ("active", "pass-p", "deleted")
