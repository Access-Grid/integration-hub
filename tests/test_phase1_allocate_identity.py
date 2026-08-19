"""Phase 1 in the allocate direction: AccessGrid mints, the PACS receives.

The Seos path inverts phase 1's usual assumption. Nothing of ours may reach
AccessGrid — it allocates only when the identity is omitted — and whatever
it allocates has to reach the PACS immediately, because the card does not
exist there until we write it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import Credential, CredentialStatus, Person
from agsync.sync.phases import phase1_provision
from agsync.sync.phases.writeback import identities_from_card
from agsync.sync.snapshot import Snapshot


class FakeCards:
    def __init__(self, card=None):
        self.calls: list[dict] = []
        self._card = card or SimpleNamespace(
            id="agcard-1", state="active", site_code="66", card_number="5001",
        )

    def provision(self, **params):
        self.calls.append(params)
        return self._card


class FakeAG:
    def __init__(self, card=None):
        self.access_cards = FakeCards(card)


class FakeReceivingPacs:
    """A PACS that receives credentials rather than supplying them."""

    supports_status_writeback = True
    supports_credential_writeback = True

    def __init__(self):
        self.writes: list[tuple[str, str, list]] = []

    def write_back_credentials(self, person_id, credential_id, identities):
        self.writes.append((person_id, credential_id, identities))
        return True


@pytest.fixture
def stub_tracking(monkeypatch):
    t = phase1_provision.tracking
    monkeypatch.setattr(t, "get", lambda *a, **k: None)
    monkeypatch.setattr(t, "upsert", lambda *a, **k: None)
    monkeypatch.setattr(t, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(t, "mark_deduped", lambda *a, **k: None)
    monkeypatch.setattr(t, "update_last_known_ag_state", lambda *a, **k: None)


def _snapshot(cred: Credential) -> Snapshot:
    snap = Snapshot()
    snap.people["p1"] = Person(id="p1", full_name="Test User", email="t@e.com", active=True)
    snap.credentials_by_person["p1"] = [cred]
    return snap


def _allocating_cred() -> Credential:
    return Credential(
        id="seos-slot1",
        person_id="p1",
        card_number="",
        site_code="",
        status=CredentialStatus.ACTIVE,
        trigger_active=True,
        allocate_identity=True,
    )


# --- what reaches AccessGrid --------------------------------------------


def test_no_identity_is_sent_so_accessgrid_allocates(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(_snapshot(_allocating_cred()), ag, "tpl", site_code="999")

    params = ag.access_cards.calls[0]
    # Sending either of these would stop AccessGrid allocating — including
    # the global site code, which normally fills in for a blank one.
    assert "site_code" not in params
    assert "card_number" not in params
    assert "file_data" not in params


def test_allocated_credentials_carry_no_stale_identity_metadata(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(_snapshot(_allocating_cred()), ag, "tpl", site_code="999")

    metadata = ag.access_cards.calls[0]["metadata"]
    assert metadata["pacs_credential_id"] == "seos-slot1"
    assert "site_code" not in metadata
    assert "card_number" not in metadata


def test_dedupe_is_skipped_for_allocated_credentials(stub_tracking):
    # There is nothing to dedupe against yet, and the global site code plus
    # an empty card number must not be allowed to match some other card.
    ag = FakeAG()
    snapshot = _snapshot(_allocating_cred())
    snapshot.ag_cards_by_site_card[("999", "")] = SimpleNamespace(id="other")
    provisioned = phase1_provision.run(
        snapshot, ag, "tpl", site_code="999", dedupe_by_site_card=True,
    )
    assert provisioned == 1


# --- what reaches the PACS ----------------------------------------------


def test_allocated_identity_is_written_straight_back(stub_tracking):
    pacs = FakeReceivingPacs()
    phase1_provision.run(_snapshot(_allocating_cred()), FakeAG(), "tpl", pacs=pacs)

    assert len(pacs.writes) == 1
    person_id, credential_id, identities = pacs.writes[0]
    assert (person_id, credential_id) == ("p1", "seos-slot1")
    assert [(i.site_code, i.card_number) for i in identities] == [("66", "5001")]


def test_ordinary_credentials_are_never_written_back(stub_tracking):
    pacs = FakeReceivingPacs()
    ordinary = Credential(
        id="slot1", person_id="p1", card_number="1234", site_code="66",
        status=CredentialStatus.ACTIVE, trigger_active=True,
    )
    phase1_provision.run(_snapshot(ordinary), FakeAG(), "tpl", pacs=pacs)
    assert pacs.writes == []


def test_writeback_failure_does_not_fail_the_provision(stub_tracking):
    class Exploding(FakeReceivingPacs):
        def write_back_credentials(self, *a, **k):
            raise RuntimeError("PACS said no")

    provisioned = phase1_provision.run(
        _snapshot(_allocating_cred()), FakeAG(), "tpl", pacs=Exploding(),
    )
    # The AccessGrid card exists and is tracked; phase 4 retries the write.
    assert provisioned == 1


# --- reading identities off an AccessGrid card --------------------------


def test_one_identity_per_installed_device():
    # A pass on a phone and a watch has two credentials, and both belong in
    # Millennium — that is why provisioning demands two free slots.
    card = SimpleNamespace(
        id="agcard-1",
        site_code="66",
        card_number="5001",
        device_credentials=[
            {"site_code": "66", "card_number": "5001"},
            {"site_code": "66", "card_number": "5002"},
        ],
    )
    assert [(i.site_code, i.card_number) for i in identities_from_card(card)] == [
        ("66", "5001"), ("66", "5002"),
    ]


def test_falls_back_to_the_cards_own_identity():
    card = SimpleNamespace(id="agcard-1", site_code="66", card_number="5001")
    assert [(i.site_code, i.card_number) for i in identities_from_card(card)] == [
        ("66", "5001"),
    ]


def test_card_with_no_identity_yields_nothing():
    assert identities_from_card(SimpleNamespace(id="agcard-1")) == []
