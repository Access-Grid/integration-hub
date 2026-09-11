"""Releasing PACS slots for credentials AccessGrid has deleted.

A card template pair allocates one credential per platform and the holder
installs exactly one; AccessGrid deletes the other. Millennium gives each
cardholder three slots, one of which holds the trigger card, so an
abandoned credential left in place is the difference between a watch that
can be provisioned later and one that cannot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.sync.phases import phase3_deletions
from agsync.sync.phases.writeback import (
    deleted_identities_from_cards,
    identities_from_cards,
)

APPLE = ("2", "1216")
ANDROID = ("2", "1217")


def _pair(android_state="deleted", apple_state="active", pass_state="active"):
    return SimpleNamespace(
        id="I_UgcwkCz7nO01s",
        state=pass_state,
        details=[
            SimpleNamespace(
                id="ewXEpYZyG2Fimj4", site_code=APPLE[0], card_number=APPLE[1],
                expiration_date=None, state=apple_state,
            ),
            SimpleNamespace(
                id="h_6ue16ECOc8N2M", site_code=ANDROID[0], card_number=ANDROID[1],
                expiration_date=None, state=android_state,
            ),
        ],
    )


def _ids(identities):
    return [(str(i.site_code), str(i.card_number)) for i in identities]


# --- reading the states --------------------------------------------------


def test_the_deleted_half_is_the_one_retired():
    assert _ids(deleted_identities_from_cards(_pair())) == [ANDROID]


def test_a_live_pair_retires_nothing():
    assert deleted_identities_from_cards(_pair(android_state="created")) == []


def test_a_wholly_deleted_pass_takes_both_halves():
    got = _ids(deleted_identities_from_cards(
        _pair(android_state="created", apple_state="created", pass_state="deleted")
    ))
    assert sorted(got) == sorted([APPLE, ANDROID])


def test_writeback_no_longer_offers_a_deleted_credential():
    """The loop this closes: phase 3 releases the slot, phase 4 refills it.

    A deleted credential stays in `details` — that is how it is detected —
    so without the filter it would be written straight back every cycle.
    """
    assert _ids(identities_from_cards(_pair())) == [APPLE]


# --- the phase 3 pass ----------------------------------------------------


class _Pacs:
    supports_credential_retirement = True

    def __init__(self, written=None):
        if written is None:
            written = {("11587", "seos-slot1"): ["1216", "1217"]}
        self._written = written
        self.retired: list = []

    def written_credentials(self):
        return self._written

    def retire_credentials(self, person_id, credential_id, identities):
        self.retired.append((person_id, credential_id, _ids(identities)))
        return len(identities)


def _snapshot(*cards, sync_ref="ref-1"):
    """A snapshot resolving the tracked pass to every card of its issue."""
    from agsync.sync.snapshot import Snapshot

    snap = Snapshot()
    if cards:
        snap.ag_cards_by_sync_ref[sync_ref] = list(cards)
    return snap


@pytest.fixture
def tracked(monkeypatch):
    row = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos-slot1",
        ag_card_id="I_UgcwkCz7nO01s", status="active", sync_ref="ref-1",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [row])
    return row


def _card(number, state, site="2", cid=None):
    return SimpleNamespace(
        id=cid or f"card-{number}", site_code=site, card_number=number,
        state=state, details=None, expiration_date=None,
    )


def test_a_deleted_half_is_handed_to_the_adapter(tracked):
    pacs = _Pacs()
    snap = _snapshot(_pair())
    assert phase3_deletions._retire_deleted_credentials(snap, pacs) == 1
    assert pacs.retired == [("11587", "seos-slot1", [ANDROID])]


def test_a_re_issue_leaves_two_cards_and_the_deleted_one_is_found(tracked):
    """The shape that slipped through: two cards, one sync_ref, no details.

    Reading only the tracked id reported the survivor and never mentioned
    the deleted card, so its Millennium slot was never released.
    """
    pacs = _Pacs()
    snap = _snapshot(_card("1238", "active"), _card("1237", "deleted"))
    assert phase3_deletions._retire_deleted_credentials(snap, pacs) == 1
    assert pacs.retired == [("11587", "seos-slot1", [("2", "1237")])]


def test_an_unresolvable_pass_retires_nothing(tracked):
    """The safety property. No information is not evidence of deletion."""
    pacs = _Pacs()
    assert phase3_deletions._retire_deleted_credentials(_snapshot(), pacs) == 0
    assert pacs.retired == []


def test_a_pass_with_no_deletions_is_left_alone(tracked):
    pacs = _Pacs()
    snap = _snapshot(_pair(android_state="created"))
    assert phase3_deletions._retire_deleted_credentials(snap, pacs) == 0
    assert pacs.retired == []


def test_an_empty_details_list_retires_nothing(tracked):
    """A shape we did not expect must do nothing, not delete everything."""
    pacs = _Pacs()
    snap = _snapshot(SimpleNamespace(id="x", state="active", details=[]))
    assert phase3_deletions._retire_deleted_credentials(snap, pacs) == 0
    assert pacs.retired == []


def test_cardholders_we_never_wrote_to_are_skipped(tracked):
    pacs = _Pacs(written={})
    snap = _snapshot(_pair())
    assert phase3_deletions._retire_deleted_credentials(snap, pacs) == 0
    assert pacs.retired == []


def test_an_adapter_without_the_capability_is_skipped(tracked):
    class ReadOnly:
        supports_credential_retirement = False

    snap = _snapshot(_pair())
    assert phase3_deletions._retire_deleted_credentials(snap, ReadOnly()) == 0


# --- per-device credentials ----------------------------------------------
#
# An Apple Watch gets its own card number, carried on the pass's `devices`
# entries rather than in `details`. Those entries call the lifecycle field
# `status`, not `state`.


def _with_devices(*devices, card_number="1238", state="active"):
    return SimpleNamespace(
        id="3xOusBDrErWd004", site_code="2", card_number=card_number,
        state=state, details=None, expiration_date=None,
        devices=list(devices),
    )


def _device(number, status, device_type="apple_watch"):
    return {
        "id": f"dev-{number}", "platform": "apple", "device_type": device_type,
        "status": status, "site_code": "2", "card_number": number,
    }


def test_a_watch_credential_is_written_alongside_the_phone():
    card = _with_devices(
        _device("1238", "installed", "iphone"), _device("1243", "installed"),
    )
    assert _ids(identities_from_cards(card)) == [("2", "1238"), ("2", "1243")]


def test_a_removed_device_is_retired():
    """`status`, not `state` — reading only the latter never saw this."""
    card = _with_devices(
        _device("1238", "installed", "iphone"), _device("1243", "deleted"),
    )
    assert _ids(deleted_identities_from_cards(card)) == [("2", "1243")]
    assert _ids(identities_from_cards(card)) == [("2", "1238")]
