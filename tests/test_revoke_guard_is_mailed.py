"""When the 30% check holds back revokes, the operator is mailed, not only logged.

A read that brings back under 30% of the cardholders we track revokes
nobody for being missing. Right while it does, people who did leave keep
working passes, and a log line is where nobody looks.
"""

from __future__ import annotations

import pytest

from agsync import notifications
from agsync.lib.pacs import Person
from agsync.sync import tracking
from agsync.sync.phases import phase3_deletions
from agsync.sync.snapshot import Snapshot


class _Ag:
    def __init__(self):
        self.deleted = []

    @property
    def access_cards(self):
        return self

    def delete(self, card_id):
        self.deleted.append(card_id)


class _TrackedOnly:
    """A PACS read by who we track (Unity), not whole."""

    reads_whole_roster = False


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: sent.append((key, problem)))
    return sent


def _track(*pids):
    for pid in pids:
        tracking.upsert(pacs_person_id=pid, pacs_credential_id="t", ag_card_id=f"pass-{pid}", full_name=pid,
                        employee_id=pid, status="active")


def _read(*pids):
    snap = Snapshot()
    snap.people = {pid: Person(id=pid, full_name=pid, email=f"{pid}@x.test") for pid in pids}
    snap.credentials_by_person = {pid: [] for pid in pids}
    return snap


def test_a_short_read_revokes_nobody_and_is_mailed(mailed):
    _track("a", "b", "c", "d")
    ag = _Ag()
    phase3_deletions.run(_read("a"), ag, _TrackedOnly())  # 1 of 4 back
    assert ag.deleted == []  # not even a, read with no card: the read as a whole is in doubt
    [(key, problem)] = mailed
    assert key == "revoke-guard"
    assert "1 of 4" in problem


def test_a_short_whole_roster_read_is_mailed(mailed, monkeypatch):
    from agsync import settings_store

    _track("a")
    monkeypatch.setattr(settings_store, "get_json", lambda key: 100)
    monkeypatch.setattr(settings_store, "set_json", lambda key, value: None)
    ag = _Ag()
    phase3_deletions.run(_read("x"), ag, None)  # 1 against 100 last time
    assert ag.deleted == []
    [(key, problem)] = mailed
    assert key == "revoke-guard" and "1 people against 100" in problem


def test_a_complete_read_mails_nothing(mailed):
    _track("a", "b")
    phase3_deletions.run(_read("a", "b"), _Ag(), _TrackedOnly())
    assert mailed == []
