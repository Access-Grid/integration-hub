"""Phase 6 pushes field changes to AccessGrid — except the address.

AccessGrid rejects `email` on an issued pass ("Unexpected parameters
provided"), which failed the whole update, so a rename never reached it
either. The address is what the pass was delivered to rather than an
attribute of it.
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
        pacs_person_id="11587", pacs_credential_id="seos-slot1",
        ag_card_id="I_UgcwkCz7nO01s", full_name="Accessg Grid", employee_id="11587",
        status="active", last_synced_email="accessg.grid.11587@old.test",
        last_synced_phone="", last_synced_full_name="Accessg Grid",
        last_synced_title="", last_known_ag_state="active", sync_error=None,
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

def test_email_is_never_sent(monkeypatch, recorded):
    ag = FakeAG()
    _run(monkeypatch, ag, _person(email="new.address@new.test"), _tracked())
    # The address changed, but nothing was pushed — sending it fails the call.
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
