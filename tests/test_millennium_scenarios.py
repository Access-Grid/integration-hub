"""The behaviours this integration is accepted on, driven end to end.

Each test here stands for one scenario in the acceptance list, and each one
runs a real phase against the real Millennium adapter and the real captured
cardholder page — no stand-in adapter. That is the point: the suite already
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

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from agsync.sync.phases import phase3_deletions, phase4_ag_to_local
from agsync.sync.snapshot import Snapshot
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
ROSTER = [{"ID": 11587, "IsActive": True, "Name": "Grid, Accessg"}]


def _enrolled_page(millennium_page, set_slot, *, written=None, active=True):
    """A cardholder with a marker in slot 1 and whatever we have written."""
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format=TRIGGER, active=True,
    )
    for index in (2, 3):
        spec = (written or {}).get(index)
        if spec is None:
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        else:
            page = set_slot(
                page, index, card_id=f"79{index}0", card_number=spec,
                facility_code="66", card_format=TRIGGER, active=active,
            )
    return page


def _adapter(make_millennium_adapter, page, **kw):
    client = FakeMillenniumClient(roster=ROSTER, pages={"11587": page})
    return make_millennium_adapter(client, mode=MODE_SEOS, **kw), client


def _posted(client):
    """The form Millennium was last asked to save."""
    assert client.saved, "expected a save"
    return client.saved[-1][1]


def _credential(adapter):
    list(adapter.list_people())
    creds = list(adapter.list_credentials("11587"))
    assert len(creds) == 1
    return creds[0]


# =====================================================================
# 1 — a card in Millennium becomes a pass in AccessGrid
# =====================================================================


def test_s1_an_enrolled_cardholder_is_provisioned_with_no_identity_of_ours(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, monkeypatch
):
    """Phase 1 against the real adapter, not a hand-built credential.

    The identity must be absent from the request: AccessGrid allocates on
    omission, and sending one of ours would mint a card number Millennium
    never agreed to.
    """
    from agsync.sync.phases import phase1_provision

    adapter, _ = _adapter(make_millennium_adapter, _enrolled_page(millennium_page, set_slot))
    cred = _credential(adapter)
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
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """A phone and a watch need a slot each; the marker's is one of them."""
    page = _enrolled_page(millennium_page, set_slot, written={2: "5001", 3: "5002"})
    adapter, _ = _adapter(make_millennium_adapter, page)
    list(adapter.list_people())
    assert list(adapter.list_credentials("11587")) == []


# =====================================================================
# 2 & 3 — the card stays inactive until the pass is installed
# =====================================================================


def test_s2_millennium_honours_awaiting_install(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The half that was never tested.

    Phase 4 asking for AWAITING_INSTALL was covered against a stand-in
    adapter; that Millennium accepts it and unticks the box was not. Drop
    it from the accepted set and the gate silently returns False, no card
    is ever deactivated, and every other test still passes.
    """
    page = _enrolled_page(millennium_page, set_slot, written={2: "5001"}, active=True)
    SeosLedger.record("11587", "seos", [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ])
    adapter, client = _adapter(make_millennium_adapter, page)

    assert adapter.update_credential_status(
        "11587", "seos", CredentialStatus.AWAITING_INSTALL,
    ) is True
    assert _posted(client).is_checked("Card_2_Active") is False


def test_s2_the_gate_deactivates_through_the_real_adapter(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, monkeypatch
):
    """Phase 4 and Millennium joined up, which is where the gap was."""
    page = _enrolled_page(millennium_page, set_slot, written={2: "5001"}, active=True)
    SeosLedger.record("11587", "seos", [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ])
    adapter, client = _adapter(make_millennium_adapter, page)
    cred = _credential(adapter)

    row = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase4_ag_to_local.tracking, "all_tracked", lambda: [row])
    snap = Snapshot()
    snap.credentials_by_person["11587"] = [cred]
    uninstalled = SimpleNamespace(
        id="pass-1", state="created", details=None, site_code="66",
        card_number="5001", expiration_date=None,
        devices=[{"device_type": "unknown", "status": "credentials_created"}],
    )
    snap.ag_cards_by_sync_ref["ref-1"] = [uninstalled]
    ag = SimpleNamespace(access_cards=SimpleNamespace(get=lambda cid: uninstalled))

    assert phase4_ag_to_local._hold_uninstalled_inactive(snap, adapter, ag) == 1
    assert _posted(client).is_checked("Card_2_Active") is False


def test_s3_an_operator_ticking_active_early_is_corrected(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Level-triggered, so it keeps correcting rather than firing once."""
    page = _enrolled_page(millennium_page, set_slot, written={2: "5001"}, active=True)
    SeosLedger.record("11587", "seos", [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ])
    adapter, client = _adapter(make_millennium_adapter, page)

    for _ in range(2):
        assert adapter.update_credential_status(
            "11587", "seos", CredentialStatus.AWAITING_INSTALL,
        ) is True
    assert len(client.saved) == 2
    assert _posted(client).is_checked("Card_2_Active") is False


# =====================================================================
# 4 — a watch's own credential reaches Millennium
# =====================================================================


def test_s4_phone_and_watch_land_in_separate_slots(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Two devices, two card numbers, two slots — through the real form."""
    page = _enrolled_page(millennium_page, set_slot)
    adapter, client = _adapter(make_millennium_adapter, page)

    assert adapter.write_back_credentials("11587", "seos", [
        CredentialIdentity("66", "1238", datetime(2026, 9, 1, tzinfo=UTC), None),
        CredentialIdentity("66", "1243", datetime(2026, 9, 1, tzinfo=UTC), None),
    ]) is True

    _, body = _posted(client).to_multipart()
    assert b'name="Card_1_EncodedCardNumber"\r\n\r\n1238\r\n' in body
    assert b'name="Card_2_EncodedCardNumber"\r\n\r\n1243\r\n' in body
    assert sorted(
        e["card_number"] for e in SeosLedger.get("11587", "seos")
    ) == ["1238", "1243"]


def test_s4_the_watch_is_written_on_a_later_cycle_than_the_phone(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """A watch is installed after the fact, so it arrives alone."""
    page = _enrolled_page(millennium_page, set_slot)
    adapter, client = _adapter(make_millennium_adapter, page)
    adapter.write_back_credentials("11587", "seos", [CredentialIdentity("66", "1238")])

    # Millennium now serves the phone's card back in the marker's slot.
    after = set_slot(
        page, 1, card_id="7919", card_number="1238", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    adapter2, client2 = _adapter(make_millennium_adapter, after)
    assert adapter2.write_back_credentials("11587", "seos", [
        CredentialIdentity("66", "1238"), CredentialIdentity("66", "1243"),
    ]) is True
    _, body = _posted(client2).to_multipart()
    assert b'name="Card_2_EncodedCardNumber"\r\n\r\n1243\r\n' in body
    # The phone's card is left exactly where it is.
    assert b'name="Card_1_EncodedCardNumber"\r\n\r\n1238\r\n' in body


# =====================================================================
# 5 — the marker card is overwritten, not worked around
# =====================================================================


def test_s5_overwriting_the_marker_leaves_the_rest_of_the_record_alone(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The safety property behind the full-form round trip.

    Millennium's save replaces the whole cardholder, so a field we fail to
    echo is a field we erase. Access levels and user fields belong to the
    customer and must come back untouched.
    """
    page = _enrolled_page(millennium_page, set_slot)
    adapter, client = _adapter(make_millennium_adapter, page)

    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm
    before = CardholderForm.parse(page)
    adapter.write_back_credentials("11587", "seos", [CredentialIdentity("66", "1238")])
    after = _posted(client)

    untouched = [
        name for name in ("FirstName", "LastName", "AccessLevelsAsJson", "TenantsAsJson")
        if before.find(name) is not None
    ]
    assert untouched, "fixture should carry fields we must not disturb"
    for name in untouched:
        assert after.value(name) == before.value(name), name


# =====================================================================
# 8 — Millennium unticking a card suspends the pass in AccessGrid
# =====================================================================


def test_s8_an_inactive_card_suspends_the_pass_through_phase_2(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, monkeypatch
):
    """The adapter reports it and phase 2 acts on it — joined up."""
    from agsync.sync.phases import phase2_local_to_ag

    page = _enrolled_page(millennium_page, set_slot, written={2: "5001"}, active=False)
    SeosLedger.record("11587", "seos", [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ])
    adapter, _ = _adapter(make_millennium_adapter, page)
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


def test_s9_the_detail_page_name_beats_the_roster_name(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Which is what makes a rename visible at all.

    The roster is the cheap listing and carries "Last, First" with whatever
    middle initial the record holds; the cardholder page carries the real
    fields. Reading only the roster would mean a rename never reached
    AccessGrid, because the two would agree by construction.
    """
    page = _enrolled_page(millennium_page, set_slot, written={2: "5001"})
    client = FakeMillenniumClient(
        # Deliberately stale and differently spelled from the page, which
        # says FirstName=Access LastName=Grid.
        roster=[{"ID": 11587, "IsActive": True, "Name": "Stalename, Oldfirst"}],
        pages={"11587": page},
    )
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)

    before = {p.id: p for p in adapter.list_people()}["11587"].full_name
    assert before == "Oldfirst Stalename", "with no profile yet, the roster is all we have"

    adapter._profile_for("11587")  # reads the detail page
    after = {p.id: p for p in adapter.list_people()}["11587"].full_name
    assert after == "Access Grid", "the detail page must win once it is read"


def test_s9_a_renamed_cardholder_is_pushed_to_accessgrid(monkeypatch):
    """Phase 6, with the name coming from the PACS snapshot."""
    from agsync.sync.phases import phase6_field_changes

    tracked = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", last_synced_full_name="Accessg Grid",
        last_synced_email="a@b.test", last_synced_phone="", last_synced_title="",
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
        phase3_deletions.tracking, "update_status",
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
    monkeypatch.setattr(phase3_deletions.tracking, "update_status", lambda *a, **k: None)

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
