"""Ceilings on the things that take somebody's access away.

Each phase that removes a pass now has a limit per cycle. None of them can
tell a real mass change from a misreading — that is the point. What they do
is turn a runaway into something slow and visible: the rest wait for later
cycles, the number is named, and a person has time to stop it while most of
the install still works.

Phase 3's empty-snapshot guard catches the catastrophic case, a failed read
reporting nobody. These catch the smaller ones it cannot: a roster that came
back partial, a trigger that was reconfigured, an id scheme that shifted.
Any of those looks from here like a population that genuinely disappeared.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import CredentialStatus, Person
from agsync.sync.phases import phase2_local_to_ag, phase3_deletions
from agsync.sync.snapshot import Snapshot

CAP3 = phase3_deletions.MAX_DELETIONS_PER_CYCLE
CAP2 = phase2_local_to_ag.MAX_TERMINATIONS_PER_CYCLE


def _tracked(n: int, status="active"):
    return [
        SimpleNamespace(
            pacs_person_id=f"p{i}", pacs_credential_id="seos",
            ag_card_id=f"card-{i}", status=status, sync_ref=f"ref-{i}",
            last_known_ag_state="active",
        )
        for i in range(n)
    ]


@pytest.fixture
def ag():
    """An AccessGrid that counts what it was asked to delete."""
    deleted: list[str] = []
    return SimpleNamespace(
        deleted=deleted,
        access_cards=SimpleNamespace(
            delete=lambda card_id: deleted.append(card_id),
            suspend=lambda card_id: None,
            resume=lambda card_id: None,
            get=lambda card_id: None,
        ),
    )


# =====================================================================
# Phase 3: cardholders that have gone from the PACS
# =====================================================================


def test_deletions_stop_at_the_cap(ag, monkeypatch):
    """A whole population reading as deleted at once gets rationed."""
    rows = _tracked(CAP3 + 10)
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: rows)
    monkeypatch.setattr(
        phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None
    )
    snap = Snapshot()
    # Somebody is in the snapshot, so the empty-roster guard does not fire —
    # this is the smaller misreading the cap exists for.
    snap.people["survivor"] = Person(id="survivor", full_name="Still Here")
    snap.credentials_by_person["survivor"] = []

    phase3_deletions.run(snap, ag)

    assert len(ag.deleted) == CAP3


def test_the_cap_names_the_number_and_who_it_stopped_at(ag, monkeypatch, caplog):
    rows = _tracked(CAP3 + 1)
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: rows)
    monkeypatch.setattr(
        phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None
    )
    snap = Snapshot()
    snap.people["survivor"] = Person(id="survivor", full_name="Still Here")
    snap.credentials_by_person["survivor"] = []

    with caplog.at_level("WARNING"):
        phase3_deletions.run(snap, ag)

    assert "deletion cap" in caplog.text
    assert str(CAP3) in caplog.text


def test_under_the_cap_everything_goes(ag, monkeypatch):
    rows = _tracked(3)
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: rows)
    monkeypatch.setattr(
        phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None
    )
    snap = Snapshot()
    snap.people["survivor"] = Person(id="survivor", full_name="Still Here")
    snap.credentials_by_person["survivor"] = []

    phase3_deletions.run(snap, ag)

    assert len(ag.deleted) == 3


def test_an_empty_snapshot_still_deletes_nothing_at_all(ag, monkeypatch):
    """The cap is the second line of defence, not a replacement for the
    first: a read that returned nobody is not evidence of anything."""
    monkeypatch.setattr(
        phase3_deletions.tracking, "all_tracked", lambda: _tracked(5)
    )

    phase3_deletions.run(Snapshot(), ag)

    assert ag.deleted == []


# =====================================================================
# Phase 2: enrolment triggers that have gone
# =====================================================================


def test_terminations_stop_at_the_cap(ag, monkeypatch):
    """The trigger is a setting somebody can mistype, and getting it wrong
    makes every tracked credential read as un-triggered at once."""
    rows = _tracked(CAP2 + 10)
    monkeypatch.setattr(phase2_local_to_ag.tracking, "all_tracked", lambda: rows)
    monkeypatch.setattr(
        phase2_local_to_ag.tracking, "mark_deleted", lambda *a, **k: None
    )
    snap = Snapshot()
    for row in rows:
        snap.credentials_by_person[row.pacs_person_id] = [
            SimpleNamespace(
                id="seos", trigger_active=False, status=CredentialStatus.ACTIVE,
                allocate_identity=True, card_number="1", site_code="1",
            )
        ]

    phase2_local_to_ag.run(snap, ag)

    assert len(ag.deleted) == CAP2


def test_the_termination_cap_points_at_the_trigger(ag, monkeypatch, caplog):
    rows = _tracked(CAP2 + 1)
    monkeypatch.setattr(phase2_local_to_ag.tracking, "all_tracked", lambda: rows)
    monkeypatch.setattr(
        phase2_local_to_ag.tracking, "mark_deleted", lambda *a, **k: None
    )
    snap = Snapshot()
    for row in rows:
        snap.credentials_by_person[row.pacs_person_id] = [
            SimpleNamespace(
                id="seos", trigger_active=False, status=CredentialStatus.ACTIVE,
                allocate_identity=True, card_number="1", site_code="1",
            )
        ]

    with caplog.at_level("WARNING"):
        phase2_local_to_ag.run(snap, ag)

    assert "termination cap" in caplog.text
    assert "enrolment trigger" in caplog.text
