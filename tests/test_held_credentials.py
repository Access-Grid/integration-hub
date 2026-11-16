"""A held credential's pass is left exactly as it is, by every phase.

The PACS shows something we will not act on — a card moved out of the
trigger format, or a slot holding a different card than we recorded — so
nothing is suspended, deleted, issued or written back until it clears.
"""

from __future__ import annotations

from types import SimpleNamespace

from agsync.lib.pacs.base import Credential, CredentialStatus
from agsync.sync.phases import phase2_local_to_ag, phase3_deletions
from agsync.sync.snapshot import Snapshot


def _setup(monkeypatch, phase, status):
    row = SimpleNamespace(
        pacs_person_id="p1", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="", last_known_ag_state="active",
    )
    monkeypatch.setattr(phase.tracking, "all_tracked", lambda: [row])
    snap = Snapshot()
    snap.people["p1"] = SimpleNamespace(id="p1", full_name="x", active=True)
    snap.credentials_by_person["p1"] = [Credential(
        id="seos", person_id="p1", status=status, trigger_active=True, hold=True,
        deactivate_date=None,
    )]
    calls: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        suspend=lambda **kw: calls.append("suspend"),
        resume=lambda **kw: calls.append("resume"),
        delete=lambda **kw: calls.append("delete"),
    ))
    return snap, ag, calls


def test_phase_2_does_not_suspend_a_held_pass(monkeypatch):
    snap, ag, calls = _setup(monkeypatch, phase2_local_to_ag, CredentialStatus.SUSPENDED)
    phase2_local_to_ag.run(snap, ag)
    assert calls == []


def test_phase_3_does_not_delete_a_held_pass(monkeypatch):
    from datetime import UTC, datetime

    snap, ag, calls = _setup(monkeypatch, phase3_deletions, CredentialStatus.ACTIVE)
    held = snap.credentials_by_person["p1"][0]
    # Dated out, which would otherwise delete it.
    snap.credentials_by_person["p1"] = [
        Credential(**{**held.__dict__, "deactivate_date": datetime(2020, 1, 1, tzinfo=UTC)})
    ]
    phase3_deletions.run(snap, ag)
    assert calls == []
