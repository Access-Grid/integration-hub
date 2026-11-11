"""Phase 6 pushes field changes to AccessGrid — the address only where the
PACS supplies the card, and on its own.

AccessGrid rejects `email` on a Seos pass ("Unexpected parameters
provided"), which failed the whole update when it rode along, so a rename
never reached it either.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.ag import AccessGridError
from agsync.lib.pacs.base import Person
from agsync.sync.phases import phase6_field_changes
from agsync.sync.snapshot import Snapshot


class FakeCards:
    def __init__(self, fail=False):
        self.calls: list[dict] = []
        self._fail = fail

    def update(self, **params):
        self.calls.append(params)
        if self._fail:
            raise AccessGridError("Unexpected parameters provided")
        return SimpleNamespace(id=params.get("card_id"))


class FakeAG:
    def __init__(self, fail=False):
        self.access_cards = FakeCards(fail)


@pytest.fixture
def recorded(monkeypatch):
    """Capture what phase 6 writes back to the tracking table."""
    calls: list[dict] = []
    monkeypatch.setattr(
        phase6_field_changes.tracking, "update_field_tracking",
        lambda pid, cid, **kw: calls.append({"person": pid, **kw}),
    )
    return calls


def _tracked(**overrides):
    row = dict(
        pacs_person_id="11587", pacs_credential_id="seos",
        ag_card_id="I_UgcwkCz7nO01s", full_name="Accessg Grid", employee_id="11587",
        status="active", last_synced_email="accessg.grid.11587@old.test",
        last_synced_phone="", last_synced_full_name="Accessg Grid",
        last_synced_title="", last_known_ag_state="active", sync_error=None,
        last_synced_expiration=None,
        retry_count=0, sync_ref="",
    )
    row.update(overrides)
    return SimpleNamespace(**row)


def _snapshot(person: Person) -> Snapshot:
    snap = Snapshot()
    snap.people[person.id] = person
    return snap


def _person(**kw):
    base = dict(id="11587", full_name="Accessg Grid",
                email="accessg.grid.11587@old.test", phone="", title="", active=True)
    base.update(kw)
    return Person(**base)


def _run(monkeypatch, ag, person, tracked):
    monkeypatch.setattr(phase6_field_changes.tracking, "all_tracked", lambda: [tracked])
    return phase6_field_changes.run(_snapshot(person), ag)


# --- what is sent --------------------------------------------------------

def test_email_is_not_sent_for_a_credential_not_read(monkeypatch, recorded):
    ag = FakeAG()
    _run(monkeypatch, ag, _person(email="new.address@new.test"), _tracked())
    # Not knowing whether the pass is Seos, where sending it fails the call.
    assert ag.access_cards.calls == []


def test_a_rename_is_sent_without_the_address(monkeypatch, recorded):
    ag = FakeAG()
    person = _person(full_name="Auston Access", email="auston.access.11587@new.test")
    _run(monkeypatch, ag, person, _tracked())

    assert len(ag.access_cards.calls) == 1
    params = ag.access_cards.calls[0]
    assert params["full_name"] == "Auston Access"
    assert "email" not in params


def test_phone_and_title_are_still_sent(monkeypatch, recorded):
    ag = FakeAG()
    _run(monkeypatch, ag, _person(phone="+15551234567", title="Resident"), _tracked())
    params = ag.access_cards.calls[0]
    assert params["phone_number"] == "+15551234567"
    assert params["title"] == "Resident"


def test_nothing_changed_means_no_call(monkeypatch, recorded):
    ag = FakeAG()
    assert _run(monkeypatch, ag, _person(), _tracked()) == 0
    assert ag.access_cards.calls == []


# --- what is said and remembered ----------------------------------------

def test_a_changed_address_is_reported(monkeypatch, recorded, caplog):
    with caplog.at_level("WARNING"):
        _run(monkeypatch, FakeAG(), _person(email="new@new.test"), _tracked())
    warning = " ".join(r.getMessage() for r in caplog.records)
    # The pass keeps the old address, and that is worth knowing.
    assert "new@new.test" in warning
    assert "accessg.grid.11587@old.test" in warning


def test_an_unpushable_address_is_reported_once_not_forever(monkeypatch, recorded):
    # Recorded even though nothing was sent, or every cycle warns again.
    _run(monkeypatch, FakeAG(), _person(email="new@new.test"), _tracked())
    assert recorded[0]["email"] == "new@new.test"


def test_a_failed_push_is_not_recorded_as_done(monkeypatch, recorded):
    # Otherwise the rename is forgotten and never retried.
    ag = FakeAG(fail=True)
    assert _run(monkeypatch, ag, _person(full_name="Auston Access"), _tracked()) == 0
    assert recorded == []


# --- the address, where the PACS supplies the card ------------------------------


def _with_cred(person, allocate):
    from agsync.lib.pacs.base import Credential, CredentialStatus

    snap = _snapshot(person)
    snap.credentials_by_person[person.id] = [Credential(
        id="seos", person_id=person.id, card_number="" if allocate else "42069",
        status=CredentialStatus.ACTIVE, trigger_active=True, allocate_identity=allocate)]
    return snap


def test_desfire_pass_gets_the_address_in_its_own_update(monkeypatch, recorded):
    ag = FakeAG()
    person = _person(full_name="Auston Access", email="new@new.test")
    monkeypatch.setattr(phase6_field_changes.tracking, "all_tracked", lambda: [_tracked()])
    phase6_field_changes.run(_with_cred(person, allocate=False), ag)
    assert [sorted(c) for c in ag.access_cards.calls] == [
        ["card_id", "full_name", "update_type"], ["card_id", "email", "update_type"]]
    assert recorded[0]["email"] == "new@new.test"


def test_seos_pass_never_gets_the_address(monkeypatch, recorded):
    ag = FakeAG()
    monkeypatch.setattr(phase6_field_changes.tracking, "all_tracked", lambda: [_tracked()])
    phase6_field_changes.run(_with_cred(_person(email="new@new.test"), allocate=True), ag)
    assert ag.access_cards.calls == []
    assert recorded[0]["email"] == "new@new.test"  # reported once, not retried


def test_rejected_address_is_retried_and_the_rename_still_recorded(monkeypatch, recorded):
    class Picky(FakeCards):
        def update(self, **params):
            if "email" in params:
                self.calls.append(params)
                raise AccessGridError("Unexpected parameters provided")
            return super().update(**params)

    ag = SimpleNamespace(access_cards=Picky())
    person = _person(full_name="Auston Access", email="new@new.test")
    monkeypatch.setattr(phase6_field_changes.tracking, "all_tracked", lambda: [_tracked()])
    phase6_field_changes.run(_with_cred(person, allocate=False), ag)
    assert recorded[0]["full_name"] == "Auston Access"
    assert recorded[0]["email"] == "accessg.grid.11587@old.test"
