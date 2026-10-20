"""Phase 3 refuses to revoke on a read too short to trust; phase 6 keeps a
pass's expiry in step with the PACS, as a metadata-only update."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agsync.lib.pacs.base import Credential, CredentialStatus
from agsync.settings_store import get_json, set_json
from agsync.sync import tracking
from agsync.sync.phases import phase3_deletions, phase6_field_changes
from agsync.sync.snapshot import Snapshot


def _ag(calls):
    return SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: calls.append(("delete", card_id)),
        update=lambda **kw: calls.append(("update", kw)),
    ))


def _people(n):
    return {str(i): SimpleNamespace(id=str(i), full_name="x", active=True) for i in range(n)}


def _track(pid):
    tracking.upsert(
        pacs_person_id=pid, pacs_credential_id="card", ag_card_id=f"pass-{pid}",
        full_name="x", employee_id=pid, status="active", last_synced_full_name="x",
    )


# --- phase 3: a read too short to trust revokes nobody --------------------


def test_a_read_under_30_percent_of_the_last_revokes_nobody():
    set_json(phase3_deletions._LAST_COMPLETE_KEY, 100)
    _track("gone")
    calls: list = []
    snap = Snapshot(people=_people(29), credentials_by_person={str(i): [] for i in range(29)})
    phase3_deletions.run(snap, _ag(calls))
    assert calls == []
    assert get_json(phase3_deletions._LAST_COMPLETE_KEY) == 100, "the baseline is kept"


def test_a_complete_read_revokes_and_becomes_the_baseline():
    set_json(phase3_deletions._LAST_COMPLETE_KEY, 100)
    _track("gone")
    calls: list = []
    snap = Snapshot(people=_people(30), credentials_by_person={str(i): [] for i in range(30)})
    phase3_deletions.run(snap, _ag(calls))
    assert calls == [("delete", "pass-gone")]
    assert get_json(phase3_deletions._LAST_COMPLETE_KEY) == 30


# --- phase 6: expiry, as a metadata update --------------------------------


def _expiring(on):
    return Credential(
        id="card", person_id="p", status=CredentialStatus.ACTIVE, trigger_active=True,
        deactivate_date=on,
    )


def _cycle(on):
    calls: list = []
    snap = Snapshot(
        people={"p": SimpleNamespace(id="p", full_name="x", email="", phone="", title="")},
        credentials_by_person={"p": [_expiring(on)]},
    )
    phase6_field_changes.run(snap, _ag(calls))
    return calls


def test_an_expiry_seen_for_the_first_time_is_recorded_not_pushed():
    _track("p")
    assert _cycle(datetime(2027, 9, 11, tzinfo=UTC)) == []
    assert tracking.get("p", "card").last_synced_expiration == "2027-09-11T00:00:00+00:00"


def test_a_changed_expiry_is_pushed_as_a_metadata_update():
    _track("p")
    _cycle(datetime(2027, 9, 11, tzinfo=UTC))
    [(kind, kw)] = _cycle(datetime(2028, 1, 1, tzinfo=UTC))
    assert kind == "update"
    assert kw == {
        "card_id": "pass-p", "update_type": "metadata",
        "expiration_date": "2028-01-01T00:00:00+00:00",
    }
    assert tracking.get("p", "card").last_synced_expiration == "2028-01-01T00:00:00+00:00"
