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


class _Ag:
    def __init__(self, card=None, error=None):
        self.card, self.error = card, error
        self.calls = 0

    @property
    def access_cards(self):
        return self

    def get(self, card_id):
        self.calls += 1
        if self.error:
            raise self.error
        return self.card


@pytest.fixture
def tracked(monkeypatch):
    row = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos-slot1",
        ag_card_id="I_UgcwkCz7nO01s", status="active",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [row])
    return row


def test_a_deleted_half_is_handed_to_the_adapter(tracked):
    pacs = _Pacs()
    assert phase3_deletions._retire_deleted_credentials(_Ag(_pair()), pacs) == 1
    assert pacs.retired == [("11587", "seos-slot1", [ANDROID])]


def test_an_accessgrid_error_retires_nothing(tracked):
    """The safety property. A failed read is not evidence of deletion."""
    from agsync.ag import AccessGridError

    pacs = _Pacs()
    ag = _Ag(error=AccessGridError("API request failed: boom"))
    assert phase3_deletions._retire_deleted_credentials(ag, pacs) == 0
    assert pacs.retired == []


def test_a_pass_with_no_deletions_is_left_alone(tracked):
    pacs = _Pacs()
    ag = _Ag(_pair(android_state="created"))
    assert phase3_deletions._retire_deleted_credentials(ag, pacs) == 0
    assert pacs.retired == []


def test_an_empty_details_list_retires_nothing(tracked):
    """A shape we did not expect must do nothing, not delete everything."""
    pacs = _Pacs()
    ag = _Ag(SimpleNamespace(id="I_UgcwkCz7nO01s", state="active", details=[]))
    assert phase3_deletions._retire_deleted_credentials(ag, pacs) == 0
    assert pacs.retired == []


def test_cardholders_we_never_wrote_to_are_not_even_read(tracked):
    pacs = _Pacs(written={})
    ag = _Ag(_pair())
    assert phase3_deletions._retire_deleted_credentials(ag, pacs) == 0
    assert ag.calls == 0


def test_an_adapter_without_the_capability_is_skipped(tracked):
    class ReadOnly:
        supports_credential_retirement = False

    ag = _Ag(_pair())
    assert phase3_deletions._retire_deleted_credentials(ag, ReadOnly()) == 0
    assert ag.calls == 0
