"""Discarding what we recorded, once the pass it described is gone.

The Seos ledger lists the cards this integration wrote into Millennium. It
has to outlive the individual cycle — it is what tells a card an operator
revoked from a card we never wrote, and what stops a revoked card being
written back — but it must not outlive the *pass*.

Left behind, it lists cards that are no longer on the cardholder, and a
recorded card missing from the cardholder is precisely the signal for "an
operator revoked this". So a cardholder handed a fresh trigger card got a new
pass that was suspended from birth over cards belonging to the pass before
it, permanently, with the old numbers accumulating in the blob.

The refusals matter as much as the forgetting. The cards we write carry the
trigger format themselves, so while one is still in a slot the ledger is the
only thing distinguishing it from an operator's marker — forget too early and
`_marker_slot` offers a live credential up to be overwritten.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from agsync.lib.pacs.base import CredentialIdentity
from agsync.lib.pacs.millennium_ultra.adapter import (
    MODE_DESFIRE,
    MODE_SEOS,
    SeosLedger,
)
from agsync.sync.phases import phase3_deletions
from agsync.sync.snapshot import Snapshot
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11587"
ROSTER = [{"ID": PID, "IsActive": True, "Name": "Grid, Accessg"}]


def _page(millennium_page, set_slot, *, marker=True, written=()):
    """Slot 1 the marker (or an unrelated card), 2 and 3 whatever we wrote."""
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1", facility_code="99",
        card_format=TRIGGER if marker else "1", active=True,
    )
    for index in (2, 3):
        spec = dict(zip((2, 3), written, strict=False)).get(index)
        if spec is None:
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        else:
            page = set_slot(
                page, index, card_id=f"79{index}0", card_number=spec,
                facility_code="66", card_format=TRIGGER, active=True,
            )
    return page


def _adapter(make_millennium_adapter, page, **kw):
    client = FakeMillenniumClient(roster=ROSTER, pages={PID: page})
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER, **kw
    )
    return adapter, client


def _read(adapter):
    """One cycle's reads, which is what populates the roster and the cache."""
    list(adapter.list_people())
    list(adapter.list_credentials(PID))


def _messages(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# =====================================================================
# Forgetting
# =====================================================================


def test_it_forgets_when_nothing_it_wrote_is_left_on_the_cardholder(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    """The case this exists for: every format-8 card has been deleted."""
    adapter, client = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])
    assert SeosLedger.get(PID, "seos")

    # The operator deletes the lot, and phase 3 deletes the pass.
    client._pages[PID] = _page(millennium_page, set_slot, marker=False)
    _read(adapter)

    with caplog.at_level(logging.INFO):
        assert adapter.forget_credential(PID, "seos") is True

    assert SeosLedger.get(PID, "seos") == []
    assert "forgetting the cards recorded for cardholder 11587" in _messages(caplog)


def test_it_forgets_when_the_cardholder_has_left_millennium(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """No cardholder, so no slot of theirs can be holding anything."""
    adapter, client = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])

    client._roster = [{"ID": "999", "IsActive": True, "Name": "Other, Some"}]
    list(adapter.list_people())

    assert adapter.forget_credential(PID, "seos") is True
    assert SeosLedger.get(PID, "seos") == []


def test_a_new_pass_is_not_suspended_by_the_one_before_it(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The regression, end to end through the adapter."""
    adapter, client = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])

    # Every format-8 card deleted; phase 3 deletes the pass and we forget.
    client._pages[PID] = _page(millennium_page, set_slot, marker=False)
    _read(adapter)
    assert adapter.forget_credential(PID, "seos") is True

    # A fresh trigger card, and a fresh pass against it.
    client._pages[PID] = _page(millennium_page, set_slot)
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1250")])
    client._pages[PID] = _page(
        millennium_page, set_slot, marker=False, written=("1250",)
    )
    list(adapter.list_people())

    creds = list(adapter.list_credentials(PID))
    assert len(creds) == 1
    assert creds[0].status.value == "active", "the new pass must not inherit a suspension"
    assert [e["card_number"] for e in SeosLedger.get(PID, "seos")] == ["1250"]


# =====================================================================
# Refusing — the half that keeps this safe
# =====================================================================


def test_it_refuses_while_a_card_it_wrote_is_still_in_a_slot(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    """Forgetting here would let `_marker_slot` overwrite a live credential."""
    adapter, client = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])
    # Millennium serves the card back in the marker's slot, as it does.
    client._pages[PID] = set_slot(
        _page(millennium_page, set_slot), 1, card_id="7919", card_number="1238",
        facility_code="66", card_format=TRIGGER, active=True,
    )
    _read(adapter)

    with caplog.at_level(logging.INFO):
        assert adapter.forget_credential(PID, "seos") is False

    assert [e["card_number"] for e in SeosLedger.get(PID, "seos")] == ["1238"]
    assert "66/1238 is still on the cardholder" in _messages(caplog)


def test_it_refuses_when_the_page_was_not_read_this_cycle(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    """Unknown slots are not an argument for either answer."""
    adapter, _ = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])
    list(adapter.list_people())
    # A write drops the cached page, and a restart clears the lot.
    adapter._profiles.clear()

    with caplog.at_level(logging.INFO):
        assert adapter.forget_credential(PID, "seos") is False

    assert SeosLedger.get(PID, "seos")
    assert "was not read this cycle" in _messages(caplog)


def test_nothing_recorded_is_not_something_to_forget(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    adapter, _ = _adapter(
        make_millennium_adapter, _page(millennium_page, set_slot)
    )
    _read(adapter)
    assert adapter.forget_credential(PID, "seos") is False


def test_desfire_has_no_ledger_of_its_own_to_forget(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The card is the customer's there; we never wrote it."""
    client = FakeMillenniumClient(
        roster=ROSTER, pages={PID: _page(millennium_page, set_slot)}
    )
    adapter = make_millennium_adapter(
        client, mode=MODE_DESFIRE, trigger_card_format=TRIGGER
    )
    SeosLedger.record(PID, "seos", [
        {"slot": 2, "card_number": "1238", "facility_code": "66"},
    ])

    assert adapter.forget_credential(PID, "seos") is False
    assert SeosLedger.get(PID, "seos"), "a DESFire adapter must not touch it"


# =====================================================================
# Phase 3 wiring
# =====================================================================


def _tracked():
    return SimpleNamespace(
        pacs_person_id=PID, pacs_credential_id="seos", ag_card_id="pass-1",
        status="active", sync_ref="ref-1",
    )


def _snapshot_without_the_credential():
    snap = Snapshot()
    # Somebody else remains, so the zero-people guard does not fire.
    snap.people["999"] = SimpleNamespace(id="999", full_name="Other", active=True)
    snap.credentials_by_person["999"] = []
    snap.people[PID] = SimpleNamespace(id=PID, full_name="Accessg Grid", active=True)
    snap.credentials_by_person[PID] = []
    return snap


def _fake_pacs(forgotten, *, retirement=True):
    return SimpleNamespace(
        supports_credential_retirement=retirement,
        forget_credential=lambda pid, cid: forgotten.append((pid, cid)) or True,
        written_credentials=lambda: {},
    )


def test_phase_3_forgets_after_deleting_the_pass(monkeypatch):
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [_tracked()])
    monkeypatch.setattr(phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None)
    forgotten: list[tuple] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(delete=lambda card_id: None))

    assert phase3_deletions.run(
        _snapshot_without_the_credential(), ag, _fake_pacs(forgotten)
    ) == 1
    assert forgotten == [(PID, "seos")]


def test_phase_3_forgets_when_the_pass_was_already_gone(monkeypatch):
    """A 404 leaves the same end state, so the record is just as stale."""
    from agsync.ag import AccessGridError

    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [_tracked()])
    monkeypatch.setattr(phase3_deletions.tracking, "remove", lambda *a, **k: None)
    forgotten: list[tuple] = []

    def gone(card_id):
        raise AccessGridError("404 not found")

    ag = SimpleNamespace(access_cards=SimpleNamespace(delete=gone))
    phase3_deletions.run(_snapshot_without_the_credential(), ag, _fake_pacs(forgotten))
    assert forgotten == [(PID, "seos")]


def test_phase_3_does_not_forget_when_the_deletion_failed(monkeypatch):
    """The pass is still there, so what we wrote still describes it."""
    from agsync.ag import AccessGridError

    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [_tracked()])
    forgotten: list[tuple] = []

    def refused(card_id):
        raise AccessGridError("500 server error")

    ag = SimpleNamespace(access_cards=SimpleNamespace(delete=refused))
    phase3_deletions.run(_snapshot_without_the_credential(), ag, _fake_pacs(forgotten))
    assert forgotten == []


def test_phase_3_does_not_forget_for_an_adapter_that_cannot_retire(monkeypatch):
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [_tracked()])
    monkeypatch.setattr(phase3_deletions.tracking, "mark_deleted", lambda *a, **k: None)
    forgotten: list[tuple] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(delete=lambda card_id: None))

    phase3_deletions.run(
        _snapshot_without_the_credential(), ag,
        _fake_pacs(forgotten, retirement=False),
    )
    assert forgotten == []


def test_a_failure_to_forget_does_not_undo_the_deletion(monkeypatch):
    """The pass is already gone; bookkeeping must not turn that into an error."""
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [_tracked()])
    marked: list[tuple] = []
    monkeypatch.setattr(
        phase3_deletions.tracking, "mark_deleted",
        lambda *a, **k: marked.append(a),
    )

    def explode(pid, cid):
        raise RuntimeError("settings store unreachable")

    pacs = SimpleNamespace(
        supports_credential_retirement=True,
        forget_credential=explode,
        written_credentials=lambda: {},
    )
    ag = SimpleNamespace(access_cards=SimpleNamespace(delete=lambda card_id: None))

    assert phase3_deletions.run(_snapshot_without_the_credential(), ag, pacs) == 1
    assert marked, "the tracking row must still be marked deleted"
