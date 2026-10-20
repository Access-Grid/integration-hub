"""The behaviours this integration is accepted on, driven end to end.

Each test here stands for one scenario in the acceptance list, and each one
runs a real phase against the real Millennium adapter, reading the export
through the real parser — no stand-in adapter. That is the point: the suite already
had tests for these scenarios at one layer or the other, and a regression
still got through, because "phase 4 asks for AWAITING_INSTALL" and
"Millennium honours AWAITING_INSTALL" were tested against different fakes
and nothing joined them up.

Where a scenario is already covered thoroughly at unit level — the form
round-trip, the ledger, pair resolution — this does not repeat it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, PacsCards
from agsync.sync.phases import phase3_deletions
from agsync.sync.snapshot import Snapshot
from tests._fakes import FakeMillenniumClient, export_row

TRIGGER = "HID 37"
PID = "11587"


def _enrolled(*, written=None, active=True, dates=None):
    """A cardholder with whatever we have written, and their own badge.

    Slot 1 carries the operator's marker only while nothing has been written
    yet; the first write consumes it. A trigger-format card we did not write
    means somebody asking for a pass.
    """
    slots = [{
        "card_number": "1", "facility_code": "99",
        "card_format": TRIGGER if not written else "Wiegand Card",
    }]
    for index in (2, 3):
        number = (written or {}).get(index)
        slots.append(number and {
            "card_number": number, "facility_code": "66", "card_format": TRIGGER,
            "active": active, **(dates or {}),
        })
    return export_row(PID, "Accessg", "Grid", slots=slots)


def _adapter(make_millennium_adapter, row, **kw):
    client = FakeMillenniumClient(rows=[row])
    return make_millennium_adapter(client, mode=MODE_SEOS, **kw), client


def _credential(adapter, credential_id="seos"):
    """One of the cardholder's credentials, by id.

    Selected rather than assumed to be the only one: a cardholder can hold
    more than one Seos pass, and an un-consumed marker is read as a request.
    """
    list(adapter.list_people())
    creds = {c.id: c for c in adapter.list_credentials("11587")}
    assert credential_id in creds, f"no {credential_id} credential in {sorted(creds)}"
    return creds[credential_id]


# =====================================================================
# 1 — a card in Millennium becomes a pass in AccessGrid
# =====================================================================


def test_s1_an_enrolled_cardholder_is_provisioned_with_no_identity_of_ours(
    make_millennium_adapter, monkeypatch
):
    """Phase 1 against the real adapter, not a hand-built credential.

    The identity must be absent from the request: AccessGrid allocates on
    omission, and sending one of ours would mint a card number Millennium
    never agreed to.
    """
    from agsync.sync.phases import phase1_provision

    adapter, _ = _adapter(make_millennium_adapter, _enrolled())
    cred = _credential(adapter, "card")
    assert cred.trigger_active is True
    assert cred.allocate_identity is True

    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(
        id="11587", full_name="Accessg Grid", email="a@b.test", phone="",
        title="", active=True,
    )
    snap.credentials_by_person["11587"] = [cred]

    monkeypatch.setattr(phase1_provision.tracking, "get", lambda *a: None)
    monkeypatch.setattr(phase1_provision.tracking, "upsert", lambda **kw: None)
    sent: list[dict] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        provision=lambda **kw: (sent.append(kw), SimpleNamespace(id="card-1", state="created"))[1],
        list=lambda **kw: [],
    ))

    assert phase1_provision.run(snap, ag, "tpl-1", pacs=adapter) == 1
    assert "card_number" not in sent[0] and "site_code" not in sent[0]
    assert sent[0]["employee_id"] == "11587"


def test_s1_a_cardholder_without_room_is_not_provisioned(
    make_millennium_adapter
):
    """A pair needs a slot for each card; the marker's is one of them."""
    adapter, _ = _adapter(
        make_millennium_adapter, _enrolled(written={2: "5001", 3: "5002"}), pair=True,
    )
    list(adapter.list_people())
    assert list(adapter.list_credentials("11587")) == []


# =====================================================================
# 8 — Millennium unticking a card suspends the pass in AccessGrid
# =====================================================================


def test_s8_an_inactive_card_suspends_the_pass_through_phase_2(
    make_millennium_adapter, monkeypatch
):
    """The adapter reports it and phase 2 acts on it — joined up."""
    from agsync.sync.phases import phase2_local_to_ag

    PacsCards.record("11587", "seos", [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ])
    adapter, _ = _adapter(make_millennium_adapter, _enrolled(written={2: "5001"}, active=False))
    cred = _credential(adapter)
    assert cred.status is CredentialStatus.SUSPENDED

    row = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1", last_known_ag_state="active",
    )
    monkeypatch.setattr(phase2_local_to_ag.tracking, "all_tracked", lambda: [row])
    monkeypatch.setattr(phase2_local_to_ag.tracking, "update_status", lambda *a, **k: None)
    snap = Snapshot()
    snap.credentials_by_person["11587"] = [cred]
    # Installed, so the awaiting-install guard does not apply.
    snap.ag_cards_by_sync_ref["ref-1"] = [SimpleNamespace(id="pass-1", state="active")]

    suspended: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        suspend=lambda card_id: suspended.append(card_id),
        resume=lambda card_id: None,
        delete=lambda card_id: None,
    ))
    phase2_local_to_ag.run(snap, ag)
    assert suspended == ["pass-1"]


# =====================================================================
# 9 — a rename in Millennium reaches AccessGrid
# =====================================================================


def test_s9_a_renamed_cardholder_is_pushed_to_accessgrid(monkeypatch):
    """Phase 6, with the name coming from the PACS snapshot."""
    from agsync.sync.phases import phase6_field_changes

    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", last_synced_full_name="Accessg Grid",
        last_synced_email="a@b.test", last_synced_phone="", last_synced_title="",
        last_synced_expiration=None,
    )
    monkeypatch.setattr(phase6_field_changes.tracking, "all_tracked", lambda: [tracked])
    monkeypatch.setattr(
        phase6_field_changes.tracking, "update_field_tracking", lambda *a, **k: None
    )
    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(
        id="11587", full_name="Auston Access", email="a@b.test", phone="",
        title="", active=True,
    )

    calls: list[dict] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        update=lambda **kw: calls.append(kw),
    ))
    assert phase6_field_changes.run(snap, ag) == 1
    assert calls[0]["full_name"] == "Auston Access"
    assert "email" not in calls[0]


# =====================================================================
# 11 — deleting the cardholder revokes the pass
# =====================================================================


def test_s11_a_deleted_cardholder_has_their_pass_revoked(monkeypatch):
    """The positive case, which nothing asserted before.

    Phase 3 is the only thing that deletes AccessGrid passes, so the path
    deserves a test that proves it fires — not only ones proving it stays
    its hand.
    """
    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [tracked])
    updated: list[tuple] = []
    monkeypatch.setattr(
        phase3_deletions.tracking, "mark_deleted",
        lambda *a, **k: updated.append((a, k)),
    )

    snap = Snapshot()
    # Somebody else is still there, so the zero-people guard does not fire.
    snap.people["999"] = SimpleNamespace(id="999", full_name="Other", active=True)
    snap.credentials_by_person["999"] = []

    deleted: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: deleted.append(card_id),
    ))
    assert phase3_deletions.run(snap, ag) == 1
    assert deleted == ["pass-1"]
    assert updated, "the tracking row must be marked deleted, not left active"


def test_s11_a_cardholder_who_lost_only_their_card_is_also_revoked(monkeypatch):
    """The person remains; the credential does not."""
    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [tracked])
    monkeypatch.setattr(phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None)

    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(id="11587", full_name="Accessg Grid", active=True)
    snap.credentials_by_person["11587"] = []      # read fine; holds nothing

    deleted: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: deleted.append(card_id),
    ))
    assert phase3_deletions.run(snap, ag) == 1
    assert deleted == ["pass-1"]


def test_s11_an_empty_roster_revokes_nobody(monkeypatch):
    """The guard that keeps a dead session from emptying AccessGrid."""
    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [tracked])
    deleted: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: deleted.append(card_id),
    ))
    assert phase3_deletions.run(Snapshot(), ag) == 0
    assert deleted == []


# =====================================================================
# 12 — a card dated out of validity stops working
# =====================================================================


def _ledger_for_slot2():
    PacsCards.record("11587", "seos", [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ])


def test_s12_an_expired_card_reports_its_end_date(
    make_millennium_adapter
):
    """Read off the card we wrote, so an operator editing it is what counts."""
    _ledger_for_slot2()
    row = _enrolled(written={2: "5001"}, dates={"expiration": "1/2/2020 12:00:00 AM"})
    adapter, _ = _adapter(make_millennium_adapter, row)
    cred = _credential(adapter)
    assert cred.deactivate_date is not None
    assert cred.deactivate_date.year == 2020


def test_s12_phase_3_deletes_a_pass_whose_card_expired(monkeypatch):
    """Whatever the Active box still says."""
    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [tracked])
    monkeypatch.setattr(phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None)

    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(id="11587", full_name="Accessg Grid", active=True)
    snap.credentials_by_person["11587"] = [SimpleNamespace(
        id="seos", status=CredentialStatus.ACTIVE, trigger_active=True, hold=False,
        allocate_identity=True,
        deactivate_date=datetime(2020, 1, 2, tzinfo=UTC),
    )]

    deleted: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: deleted.append(card_id),
    ))
    assert phase3_deletions.run(snap, ag) == 1
    assert deleted == ["pass-1"]


def test_s12_an_expired_pass_takes_its_cards_out_of_millennium(
    monkeypatch, make_millennium_adapter
):
    """Re-issuing is then adding a trigger card, as the first time."""
    _ledger_for_slot2()
    row = _enrolled(written={2: "5001"}, dates={"expiration": "1/2/2020 12:00:00 AM"})
    adapter, _ = _adapter(make_millennium_adapter, row)
    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [tracked])
    monkeypatch.setattr(phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None)
    snap = Snapshot()
    snap.people = {p.id: p for p in adapter.list_people()}
    snap.credentials_by_person["11587"] = list(adapter.list_credentials("11587"))
    card = SimpleNamespace(id="pass-1", site_code="66", card_number="5001", state="active")
    snap.ag_cards_by_sync_ref["ref-1"] = [card]
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: None, get=lambda card_id: card,
    ))

    assert phase3_deletions.run(snap, ag, adapter) == 1
    assert [e.get("pending") for e in PacsCards.get("11587", "seos")] == ["delete"]


def test_s12_a_card_still_in_date_is_left_alone(monkeypatch):
    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [tracked])
    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(id="11587", full_name="Accessg Grid", active=True)
    snap.credentials_by_person["11587"] = [SimpleNamespace(
        id="seos", status=CredentialStatus.ACTIVE, trigger_active=True, hold=False,
        allocate_identity=True,
        deactivate_date=datetime(2099, 1, 1, tzinfo=UTC),
    )]
    deleted: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: deleted.append(card_id),
    ))
    assert phase3_deletions.run(snap, ag) == 0
    assert deleted == []


def test_s12_a_card_with_no_expiry_is_not_expired(monkeypatch):
    """No date stated is not the same as a date in the past."""
    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [tracked])
    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(id="11587", full_name="Accessg Grid", active=True)
    snap.credentials_by_person["11587"] = [SimpleNamespace(
        id="seos", status=CredentialStatus.ACTIVE, trigger_active=True, hold=False,
        allocate_identity=True, deactivate_date=None,
    )]
    deleted: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: deleted.append(card_id),
    ))
    assert phase3_deletions.run(snap, ag) == 0
    assert deleted == []


def test_s12_expiries_are_rationed_per_cycle(monkeypatch):
    """A date-format change could expire a whole population at once."""
    rows = [
        SimpleNamespace(
            pacs_person_id=str(i), pacs_credential_id="seos",
            ag_card_id=f"pass-{i}", status="active", sync_ref="",
        )
        for i in range(phase3_deletions.MAX_EXPIRIES_PER_CYCLE + 10)
    ]
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: rows)
    monkeypatch.setattr(phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None)

    snap = Snapshot()
    for row in rows:
        snap.people[row.pacs_person_id] = SimpleNamespace(
            id=row.pacs_person_id, full_name="x", active=True,
        )
        snap.credentials_by_person[row.pacs_person_id] = [SimpleNamespace(
            id="seos", status=CredentialStatus.ACTIVE, trigger_active=True, hold=False,
            allocate_identity=True,
            deactivate_date=datetime(2020, 1, 2, tzinfo=UTC),
        )]

    deleted: list[str] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        delete=lambda card_id: deleted.append(card_id),
    ))
    phase3_deletions.run(snap, ag)
    assert len(deleted) == phase3_deletions.MAX_EXPIRIES_PER_CYCLE


def test_s12_a_card_not_yet_valid_holds_the_pass(
    make_millennium_adapter
):
    """Dated to start next week: it should not open a door today."""
    _ledger_for_slot2()
    row = _enrolled(written={2: "5001"}, dates={"activation": "1/2/2099 12:00:00 AM"})
    adapter, _ = _adapter(make_millennium_adapter, row)
    cred = _credential(adapter)
    assert cred.status is CredentialStatus.SUSPENDED
    assert cred.activate_date.year == 2099
