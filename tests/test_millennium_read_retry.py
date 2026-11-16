"""Retrying a cardholder page that failed to load.

A timeout on a detail page used to be logged and forgotten: the cardholder
was left with whatever was cached — nothing, on a cold start — and was not
looked at again until the sweep cursor came round, which on a large roster
is many cycles. Worse, a cardholder yielding no credentials is what phase 3
reads as a deletion.
"""

from __future__ import annotations

ROSTER = [{"ID": 11587, "IsActive": True, "Name": "Grid, Accessg"}]


def test_an_unreadable_cardholder_is_not_a_deletion(monkeypatch):
    """The reason the adapter raises instead of answering [].

    Phase 3 revokes the AccessGrid pass when a tracked credential is gone
    from its person. A dropped connection must not look like that.
    """
    from types import SimpleNamespace

    from agsync.sync.phases import phase3_deletions
    from agsync.sync.snapshot import Snapshot

    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(id="11587", full_name="A G", active=True)
    # No credentials_by_person entry: the page could not be read this cycle.

    row = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos",
        ag_card_id="agcard-1", status="active",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [row])

    deleted = []
    ag = SimpleNamespace(
        access_cards=SimpleNamespace(delete=lambda card_id: deleted.append(card_id))
    )
    assert phase3_deletions.run(snap, ag) == 0
    assert deleted == []
