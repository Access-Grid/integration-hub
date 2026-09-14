"""Guards against provisioning running away.

A misconfigured trigger on a site with thousands of cardholders turns "sync"
into "issue a pass to everyone". Two things stand in the way: a per-cycle
cap, so a mistake is slow and visible rather than instant and total; and a
stop signal, so changing the trigger stops the cycle that is already running
under the old one instead of letting it finish.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import Credential, CredentialStatus, Person
from agsync.sync.phases import phase1_provision
from agsync.sync.snapshot import Snapshot


class FakeCards:
    def __init__(self):
        self.calls: list[dict] = []

    def provision(self, **params):
        self.calls.append(params)
        return SimpleNamespace(id=f"card-{len(self.calls)}", state="active")


class FakeAG:
    def __init__(self):
        self.access_cards = FakeCards()


@pytest.fixture
def stub_tracking(monkeypatch):
    t = phase1_provision.tracking
    monkeypatch.setattr(t, "get", lambda *a, **k: None)
    monkeypatch.setattr(t, "upsert", lambda *a, **k: None)
    monkeypatch.setattr(t, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(t, "mark_deduped", lambda *a, **k: None)
    monkeypatch.setattr(t, "update_last_known_ag_state", lambda *a, **k: None)


def _snapshot(count: int) -> Snapshot:
    snap = Snapshot()
    for i in range(count):
        pid = str(i)
        snap.people[pid] = Person(
            id=pid, full_name=f"Person {i}", email=f"p{i}@e.com", active=True,
        )
        snap.credentials_by_person[pid] = [
            Credential(
                id="slot1", person_id=pid, card_number=str(1000 + i), site_code="66",
                status=CredentialStatus.ACTIVE, trigger_active=True,
            )
        ]
    return snap


def test_a_cycle_will_not_provision_the_whole_site(stub_tracking):
    ag = FakeAG()
    provisioned = phase1_provision.run(_snapshot(500), ag, "tpl", max_per_cycle=25)
    assert provisioned == 25
    assert len(ag.access_cards.calls) == 25


def test_the_cap_is_reported_with_the_real_number(stub_tracking, caplog):
    with caplog.at_level("WARNING"):
        phase1_provision.run(_snapshot(500), FakeAG(), "tpl", max_per_cycle=25)
    # Silence would read as "25 people were eligible", which is the whole
    # problem — the operator has to see the real number to spot a bad trigger.
    warnings = [r.getMessage() for r in caplog.records]
    assert any("500 credentials are eligible" in m for m in warnings)
    assert any("remaining 475" in m for m in warnings)


def test_normal_volumes_are_untouched(stub_tracking, caplog):
    ag = FakeAG()
    with caplog.at_level("WARNING"):
        provisioned = phase1_provision.run(_snapshot(5), ag, "tpl", max_per_cycle=25)
    assert provisioned == 5
    assert not [r for r in caplog.records if "eligible" in r.getMessage()]


def test_the_remainder_is_provisioned_on_later_cycles(stub_tracking):
    # The cap slows a legitimate large rollout; it must not block it.
    snapshot = _snapshot(30)
    first = phase1_provision.run(snapshot, FakeAG(), "tpl", max_per_cycle=25)
    second = phase1_provision.run(snapshot, FakeAG(), "tpl", max_per_cycle=25)
    assert (first, second) == (25, 25)  # tracking is stubbed, so nothing is "done"


# --- stopping a cycle whose configuration changed -----------------------


def test_a_config_change_stops_the_cycle_in_flight(stub_tracking):
    # This is what let a trigger change keep provisioning against the old
    # trigger for minutes after it was changed.
    ag = FakeAG()
    calls = {"n": 0}

    def changed_after_two() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    provisioned = phase1_provision.run(
        _snapshot(100), ag, "tpl", should_stop=changed_after_two,
    )
    assert provisioned < 100
    assert len(ag.access_cards.calls) == provisioned


def test_no_stop_signal_means_business_as_usual(stub_tracking):
    ag = FakeAG()
    assert phase1_provision.run(_snapshot(3), ag, "tpl", should_stop=lambda: False) == 3


def test_a_failed_provision_is_not_retried_by_phase_1(monkeypatch):
    """Retries belong to phase 5, which counts them and gives up.

    A failed row has no ag_card_id, so phase 1 used to read it as new and
    provision again every cycle — ignoring MAX_RETRIES entirely. Against a
    permanent error like a duplicate Origo address, that is an API call per
    cardholder per cycle, forever.
    """
    from types import SimpleNamespace

    from agsync.sync.phases import phase1_provision

    person = SimpleNamespace(
        id="11591", full_name="Auston Bunsen", email="a@b.c", phone="",
        title="", active=True,
    )
    cred = SimpleNamespace(
        id="seos", trigger_active=True, status=None, site_code="",
        allocate_identity=True, card_number="", file_data=None,
    )
    snap = Snapshot()
    snap.people["11591"] = person
    snap.credentials_by_person["11591"] = [cred]

    monkeypatch.setattr(
        phase1_provision.tracking, "get",
        lambda pid, cid: SimpleNamespace(
            ag_card_id=None, status="pending", sync_error="Origo 409",
            retry_count=30, last_known_ag_state="",
        ),
    )
    monkeypatch.setattr(phase1_provision.tracking, "upsert", lambda **kw: None)

    calls = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        provision=lambda **kw: calls.append(kw),
        list=lambda **kw: [],
    ))
    phase1_provision.run(snap, ag, "tmpl-1")
    assert calls == []
