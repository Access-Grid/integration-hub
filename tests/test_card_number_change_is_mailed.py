"""Non-Seos credential edits update the existing pass and preserve its metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync import notifications
from agsync.lib.pacs import Credential, CredentialStatus, Person
from agsync.sync import tracking
from agsync.sync.phases import phase6_field_changes
from agsync.sync.snapshot import Snapshot


class _Ag:
    def __init__(self):
        self.updates = []

    @property
    def access_cards(self):
        return self

    def update(self, card_id, update_type, **changes):
        self.updates.append(changes)


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "notify_card_problem", lambda key, problem: sent.append((key, problem)))
    return sent


def _phase6(pacs_number, issued_number, allocate=False, listed=True, name="Pat Doe"):
    tracking.upsert(pacs_person_id="p", pacs_credential_id="t", ag_card_id="pass-p", full_name="Pat Doe",
                    employee_id="p", status="active", sync_ref="ref-p", last_synced_full_name="Pat Doe",
                    last_synced_email="pat@example.com", last_synced_department="")
    snap = Snapshot()
    snap.people = {"p": Person(id="p", full_name=name, email="pat@example.com")}
    snap.credentials_by_person = {"p": [Credential(id="t", person_id="p", card_number=pacs_number,
                                                   status=CredentialStatus.ACTIVE, trigger_active=True,
                                                   allocate_identity=allocate)]}
    if listed:
        snap.ag_cards_by_sync_ref["ref-p"] = [
            SimpleNamespace(id=h, state="active", metadata={"card_number": issued_number, "site_code": "239"})
            for h in ("ios", "android")
        ]
    ag = _Ag()
    phase6_field_changes.run(snap, ag)
    return ag


def test_an_edited_card_number_updates_the_existing_pass(mailed):
    ag = _phase6("5002", "5001")
    assert ag.updates[0]["card_number"] == "5002"
    assert ag.updates[0]["metadata"] == {"card_number": "5002", "site_code": "239"}
    assert mailed == []


def test_credential_and_person_changes_share_one_update(mailed):
    ag = _phase6("5002", "5001", name="Pat Doe-Smith")
    assert len(ag.updates) == 1
    assert ag.updates[0]["full_name"] == "Pat Doe-Smith"
    assert ag.updates[0]["card_number"] == "5002"


def test_an_unchanged_card_number_mails_nothing(mailed):
    _phase6("5001", "5001")
    assert mailed == []


def test_a_seos_pass_is_not_checked(mailed):
    _phase6("", "", allocate=True)
    assert mailed == []


def test_a_pass_not_in_the_listing_is_not_checked(mailed):
    _phase6("5002", "5001", listed=False)
    assert mailed == []
