"""A Seos holder deactivated in the PACS before installing has their pass suspended.

A Seos pass's card stays off until the pass is installed, so phase 2 does
not read an off card on an uninstalled pass as a revocation. A deactivated
identity is one, though, whatever its cards say: its pass stayed live and
installable, and the install then switched its cards on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase2_local_to_ag, phase4_ag_to_local
from agsync.sync.snapshot import Snapshot


class _Ag:
    def __init__(self, card):
        self.card, self.calls = card, []

    @property
    def access_cards(self):
        return self

    def get(self, card_id):
        return self.card

    def suspend(self, card_id):
        self.calls.append(("suspend", card_id))

    def resume(self, card_id):
        self.calls.append(("resume", card_id))


class _Pacs:
    supports_status_writeback = True
    supports_credential_writeback = True

    def __init__(self):
        self.writes = []

    def update_credential_status(self, pid, cid, status):
        self.writes.append(status)
        return True

    def write_back_credentials(self, *a):
        return True


def _snapshot(person_active: bool, pass_state: str, installed: bool, status=CredentialStatus.SUSPENDED):
    tracking.upsert(pacs_person_id="p", pacs_credential_id="t", ag_card_id="pass-p", full_name="Pat",
                    employee_id="p", status="active", last_known_ag_state=pass_state, sync_ref="ref-p")
    card = SimpleNamespace(id="half", state=pass_state, site_code="239", card_number="9001",
                           devices=[SimpleNamespace(device_type="iphone", status="installed")] if installed else [])
    snap = Snapshot()
    snap.people = {"p": Person(id="p", full_name="Pat", email="pat@example.com", active=person_active)}
    snap.credentials_by_person = {"p": [Credential(id="t", person_id="p", card_number="", status=status,
                                                   trigger_active=True, allocate_identity=True)]}
    snap.ag_cards_by_sync_ref["ref-p"] = [card]
    return snap, _Ag(card)


def test_a_holder_deactivated_before_installing_has_the_pass_suspended():
    snap, ag = _snapshot(person_active=False, pass_state="created", installed=False)
    phase2_local_to_ag.run(snap, ag)
    assert ag.calls == [("suspend", "pass-p")]


def test_an_active_holders_uninstalled_pass_is_still_not_suspended_for_its_held_card():
    snap, ag = _snapshot(person_active=True, pass_state="created", installed=False)
    phase2_local_to_ag.run(snap, ag)
    assert ag.calls == []


@pytest.mark.parametrize("pass_state", ["created", "active"])
def test_an_install_does_not_switch_on_the_cards_of_a_deactivated_holder(pass_state):
    snap, ag = _snapshot(person_active=False, pass_state=pass_state, installed=True)
    pacs = _Pacs()
    phase4_ag_to_local.run(snap, pacs, ag)
    assert CredentialStatus.ACTIVE not in pacs.writes


def test_an_install_still_switches_on_the_cards_of_an_active_holder():
    snap, ag = _snapshot(person_active=True, pass_state="active", installed=True,
                         status=CredentialStatus.AWAITING_INSTALL)
    pacs = _Pacs()
    phase4_ag_to_local.run(snap, pacs, ag)
    assert pacs.writes == [CredentialStatus.ACTIVE]
