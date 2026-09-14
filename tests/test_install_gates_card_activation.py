"""A PACS card goes live when the pass reaches a device, not when it is issued.

An operator creates the card in Millennium and ticks Active, but nothing is
on a phone yet — so the PACS is holding a working credential for someone who
cannot present it. Phase 4 holds the card inactive until an install happens,
and a watch counts as much as a phone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import CredentialStatus
from agsync.sync.phases import phase4_ag_to_local as phase4
from agsync.sync.phases.writeback import is_installed
from agsync.sync.snapshot import Snapshot


def _device(kind, status):
    return {"id": f"d-{kind}", "device_type": kind, "status": status,
            "site_code": "2", "card_number": "1238"}


def _card(*devices, state="active"):
    return SimpleNamespace(
        id="pass-1", site_code="2", card_number="1238", state=state,
        details=None, expiration_date=None, devices=list(devices),
    )


# --- the predicate -------------------------------------------------------


def test_an_issued_but_uninstalled_pass_is_not_installed():
    assert is_installed(_card(state="created")) is False


def test_a_phone_counts():
    assert is_installed(_card(_device("iphone", "installed"))) is True


def test_a_watch_alone_counts():
    assert is_installed(_card(_device("apple_watch", "installed"))) is True


def test_a_device_that_is_not_installed_does_not_count():
    assert is_installed(_card(_device("iphone", "pending"))) is False


# --- the phase -----------------------------------------------------------


class _Pacs:
    supports_credential_writeback = True

    def __init__(self):
        self.calls: list = []

    def update_credential_status(self, person_id, credential_id, status):
        self.calls.append((person_id, credential_id, status))
        return True


class _Ag:
    def __init__(self, card):
        self._card = card

    @property
    def access_cards(self):
        return self

    def get(self, card_id):
        return self._card


@pytest.fixture
def wired(monkeypatch):
    def _build(card, pacs_status):
        row = SimpleNamespace(
            pacs_person_id="11618", pacs_credential_id="seos",
            ag_card_id="pass-1", status="active", sync_ref="ref-1",
        )
        monkeypatch.setattr(phase4.tracking, "all_tracked", lambda: [row])
        snap = Snapshot()
        snap.ag_cards_by_sync_ref["ref-1"] = [card]
        snap.credentials_by_person["11618"] = [
            SimpleNamespace(id="seos", status=pacs_status, allocate_identity=True),
        ]
        return snap, _Pacs(), _Ag(card)
    return _build


def test_an_active_card_is_deactivated_while_the_pass_waits(wired):
    """And says why: awaiting an install, not suspended by anyone."""
    snap, pacs, ag = wired(_card(state="created"), CredentialStatus.ACTIVE)
    assert phase4._hold_uninstalled_inactive(snap, pacs, ag) == 1
    assert pacs.calls == [("11618", "seos", CredentialStatus.AWAITING_INSTALL)]


def test_the_card_goes_live_once_a_device_installs(wired):
    snap, pacs, ag = wired(
        _card(_device("apple_watch", "installed")), CredentialStatus.SUSPENDED,
    )
    assert phase4._hold_uninstalled_inactive(snap, pacs, ag) == 1
    assert pacs.calls == [("11618", "seos", CredentialStatus.ACTIVE)]


def test_a_card_already_matching_its_pass_is_left_alone(wired):
    snap, pacs, ag = wired(
        _card(_device("iphone", "installed")), CredentialStatus.ACTIVE,
    )
    assert phase4._hold_uninstalled_inactive(snap, pacs, ag) == 0
    assert pacs.calls == []


def test_it_keeps_correcting_rather_than_waiting_for_a_change(wired):
    """Level-triggered on purpose: the operator can re-tick Active any time."""
    snap, pacs, ag = wired(_card(state="created"), CredentialStatus.ACTIVE)
    phase4._hold_uninstalled_inactive(snap, pacs, ag)
    phase4._hold_uninstalled_inactive(snap, pacs, ag)
    assert len(pacs.calls) == 2


def test_a_read_only_pacs_is_untouched(wired):
    snap, pacs, ag = wired(_card(state="created"), CredentialStatus.ACTIVE)
    pacs.supports_credential_writeback = False
    assert phase4._hold_uninstalled_inactive(snap, pacs, ag) == 0
    assert pacs.calls == []


def test_phase2_does_not_push_the_hold_back_to_accessgrid(monkeypatch):
    """The deadlock this avoids.

    Phase 4 deactivates the card because the pass is not installed. Phase 2
    would then read an inactive card as a revocation and suspend the pass —
    and a suspended pass cannot be installed, so it would never become
    active and the card would never go live.
    """
    from agsync.sync.phases import phase2_local_to_ag as phase2

    row = SimpleNamespace(
        pacs_person_id="11618", pacs_credential_id="seos",
        ag_card_id="pass-1", status="active", sync_ref="ref-1",
        last_known_ag_state="created",
    )
    monkeypatch.setattr(phase2.tracking, "all_tracked", lambda: [row])

    snap = Snapshot()
    snap.ag_cards_by_sync_ref["ref-1"] = [_card(state="created")]
    snap.credentials_by_person["11618"] = [
        SimpleNamespace(
            id="seos", status=CredentialStatus.SUSPENDED,
            allocate_identity=True, trigger_active=True,
        ),
    ]

    suspended = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        suspend=lambda card_id: suspended.append(card_id),
        resume=lambda card_id: None,
        delete=lambda card_id: None,
    ))
    assert phase2.run(snap, ag) == 0
    assert suspended == []


def test_a_read_only_pacs_still_reports_a_revocation_before_install(monkeypatch):
    """The guard is only for credentials we minted.

    On a PACS whose cards are the customer's own, switching one off before
    anyone installed the pass is a real revocation, and it has to reach
    AccessGrid rather than being mistaken for our own install hold.
    """
    from agsync.sync.phases import phase2_local_to_ag as phase2

    row = SimpleNamespace(
        pacs_person_id="p1", pacs_credential_id="tok-1",
        ag_card_id="pass-1", status="active", sync_ref="ref-1",
        last_known_ag_state="created",
    )
    monkeypatch.setattr(phase2.tracking, "all_tracked", lambda: [row])

    snap = Snapshot()
    snap.ag_cards_by_sync_ref["ref-1"] = [_card(state="created")]
    snap.credentials_by_person["p1"] = [
        SimpleNamespace(
            id="tok-1", status=CredentialStatus.SUSPENDED,
            allocate_identity=False, trigger_active=True,
        ),
    ]

    suspended = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        suspend=lambda card_id: suspended.append(card_id),
        resume=lambda card_id: None,
        delete=lambda card_id: None,
    ))
    phase2.run(snap, ag)
    assert suspended == ["pass-1"]
