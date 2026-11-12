"""A pass that keeps failing to issue is tried 3 times, then the operator is mailed.

Phase 1 decided "not issued yet" by the tracking row having no pass id, so
a row whose issue failed was issued again every cycle, with no limit —
every 20 s on Unity, up to 200 a cycle — and phase 5's retry cap did
nothing. Now phase 1 issues a card once, phase 5 retries it, and after
MAX_RETRIES failures it is left alone and mailed. Switching the card off
in the PACS, or taking its trigger away, forgets the failures, so turning
it back on starts again.
"""

from __future__ import annotations

import pytest
from accessgrid import AccessGridError

from agsync import notifications
from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase1_provision, phase5_retries
from agsync.sync.phases.phase5_retries import MAX_RETRIES
from agsync.sync.snapshot import Snapshot


class _RefusingAg:
    def __init__(self):
        self.issued = 0

    @property
    def access_cards(self):
        return self

    def provision(self, **kw):
        self.issued += 1
        raise AccessGridError("422 template is not published")


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: sent.append((key, problem)))
    return sent


def _cycle(ag, status=CredentialStatus.ACTIVE, trigger=True, hold=False):
    """Phases 1 and 5, as the engine runs them, over one cardholder."""
    snap = Snapshot()
    snap.people = {"p": Person(id="p", full_name="Pat Doe", email="pat@example.com")}
    snap.credentials_by_person = {"p": [Credential(id="t", person_id="p", card_number="1234", status=status,
                                                   trigger_active=trigger, hold=hold)]}
    phase1_provision.run(snap, ag, "tpl")
    phase5_retries.run(snap, ag, "tpl")


def test_a_failing_issue_is_tried_three_times_then_left_alone(mailed):
    ag = _RefusingAg()
    for _ in range(MAX_RETRIES + 5):
        _cycle(ag)
    assert ag.issued == MAX_RETRIES
    row = tracking.get("p", "t")
    assert row.ag_card_id is None and row.retry_count == MAX_RETRIES


def test_giving_up_is_mailed_with_the_last_error(mailed):
    ag = _RefusingAg()
    for _ in range(MAX_RETRIES + 1):
        _cycle(ag)
    [(key, problem)] = {k: (k, p) for k, p in mailed}.values()
    assert key == "issue-failed:p:t"
    assert "Pat Doe" in problem and "422 template is not published" in problem


@pytest.mark.parametrize("change", [{"status": CredentialStatus.SUSPENDED, "hold": True}, {"trigger": False}])
def test_switching_the_card_off_and_on_again_starts_the_tries_over(mailed, change):
    ag = _RefusingAg()
    for _ in range(MAX_RETRIES + 1):
        _cycle(ag)
    assert ag.issued == MAX_RETRIES

    _cycle(ag, **change)  # an operator switches it off, or takes the trigger away
    assert tracking.get("p", "t") is None
    _cycle(ag)  # and back on
    assert ag.issued == MAX_RETRIES + 1


def test_an_unread_cardholder_does_not_reset_the_tries(mailed):
    ag = _RefusingAg()
    for _ in range(MAX_RETRIES + 1):
        _cycle(ag)
    snap = Snapshot()
    snap.people = {"p": Person(id="p", full_name="Pat Doe", email="pat@example.com")}
    phase5_retries.run(snap, ag, "tpl")  # p read, their cards not: no entry
    assert tracking.get("p", "t").retry_count == MAX_RETRIES


def test_an_issue_that_works_on_a_retry_is_kept(mailed):
    class FailsOnce(_RefusingAg):
        def provision(self, **kw):
            self.issued += 1
            if self.issued == 1:
                raise AccessGridError("502 Bad Gateway")
            from types import SimpleNamespace
            return SimpleNamespace(id="pass-1", state="created")

    ag = FailsOnce()
    for _ in range(4):
        _cycle(ag)
    assert ag.issued == 2
    assert tracking.get("p", "t").ag_card_id == "pass-1"
    assert mailed == []
