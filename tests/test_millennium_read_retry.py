"""Retrying a cardholder page that failed to load.

A timeout on a detail page used to be logged and forgotten: the cardholder
was left with whatever was cached — nothing, on a cold start — and was not
looked at again until the sweep cursor came round, which on a large roster
is many cycles. Worse, a cardholder yielding no credentials is what phase 3
reads as a deletion.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.millennium_ultra import adapter as mod
from tests._fakes import FakeMillenniumClient

ROSTER = [{"ID": 11587, "IsActive": True, "Name": "Grid, Accessg"}]


class FlakyClient(FakeMillenniumClient):
    """Fails the first `failures` page reads, then behaves."""

    def __init__(self, page, failures=1):
        super().__init__(roster=ROSTER, pages={"11587": page})
        self.remaining = failures
        self.reads = 0

    def get_cardholder_form(self, cardholder_id):
        self.reads += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise OSError("[Errno 60] Operation timed out")
        return super().get_cardholder_form(cardholder_id)


@pytest.fixture
def clock(monkeypatch):
    """Monotonic time we control, so backoff is tested without sleeping."""
    now = {"t": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: now["t"])
    return now


def _profiles(adapter, client):
    """One cycle: roster, then a profile read for the cardholder."""
    list(adapter.list_people())
    return adapter._profile_for("11587")


def test_a_failed_read_is_queued_with_a_backoff(
    make_millennium_adapter, millennium_page, clock
):
    client = FlakyClient(millennium_page, failures=1)
    adapter = make_millennium_adapter(client)

    assert _profiles(adapter, client) is None
    attempts, next_at = adapter._retry["11587"]
    assert attempts == 1
    assert next_at == mod.RETRY_BASE_SECONDS


def test_the_retry_waits_for_its_turn(make_millennium_adapter, millennium_page, clock):
    client = FlakyClient(millennium_page, failures=1)
    adapter = make_millennium_adapter(client)
    _profiles(adapter, client)
    reads_after_failure = client.reads

    # Too soon: the page is not read again.
    clock["t"] = mod.RETRY_BASE_SECONDS - 1
    _profiles(adapter, client)
    assert client.reads == reads_after_failure


def test_a_due_retry_is_read_outside_the_sweep(
    make_millennium_adapter, millennium_page, clock
):
    client = FlakyClient(millennium_page, failures=1)
    adapter = make_millennium_adapter(client)
    _profiles(adapter, client)

    clock["t"] = mod.RETRY_BASE_SECONDS
    adapter._sweep = set()  # not in this cycle's slice
    profile = _profiles(adapter, client)

    assert profile is not None
    assert "11587" not in adapter._retry  # success clears the queue


def test_the_delay_doubles_and_is_capped(
    make_millennium_adapter, millennium_page, clock
):
    client = FlakyClient(millennium_page, failures=99)
    adapter = make_millennium_adapter(client)

    delays = []
    for _ in range(10):
        _profiles(adapter, client)
        attempts, next_at = adapter._retry["11587"]
        delays.append(next_at - clock["t"])
        clock["t"] = next_at

    assert delays[:4] == [60, 120, 240, 480]
    assert max(delays) == mod.RETRY_MAX_SECONDS


def test_an_enrolled_cardholder_is_never_held_back(
    make_millennium_adapter, millennium_page, set_slot, clock
):
    """Their slots drive phases 2-6; a stale reading is worse than a retry."""
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1234",
        facility_code="66", card_format="7", active=True,
    )
    client = FlakyClient(page, failures=0)
    adapter = make_millennium_adapter(client)
    _profiles(adapter, client)  # caches an enrolled profile

    client.remaining = 1
    _profiles(adapter, client)          # fails, queues a retry
    reads = client.reads
    clock["t"] = 1                      # nowhere near due
    _profiles(adapter, client)
    assert client.reads > reads         # read anyway


def test_an_unreadable_cardholder_is_not_a_deletion(monkeypatch):
    """The reason the adapter raises instead of answering [].

    Phase 3 revokes the AccessGrid pass when a tracked credential is gone
    from its person. A dropped connection must not look like that.
    """
    from types import SimpleNamespace

    from agsync.sync.phases import phase3_deletions
    from agsync.sync.snapshot import Snapshot

    snap = Snapshot()
    snap.people["11587"] = SimpleNamespace(id="11587", full_name="A G", active=True)
    # No credentials_by_person entry: the page could not be read this cycle.

    row = SimpleNamespace(
        pacs_person_id="11587", pacs_credential_id="seos-slot1",
        ag_card_id="agcard-1", status="active",
    )
    monkeypatch.setattr(phase3_deletions.tracking, "all_tracked", lambda: [row])

    deleted = []
    ag = SimpleNamespace(
        access_cards=SimpleNamespace(delete=lambda card_id: deleted.append(card_id))
    )
    assert phase3_deletions.run(snap, ag) == 0
    assert deleted == []
