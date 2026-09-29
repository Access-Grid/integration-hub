"""The diagnostics that make a silently-dropped cardholder visible.

A cardholder the bulk export's name join does not reach is read only while
their profile is cached as enrolled — and writing to them drops that cache.
From the next cycle they raise PacsRecordUnavailable, and every phase reads
that as "nothing to do": no credential, no writeback, no watch, no log.

These tests do not assert the bug is fixed. They assert it is *audible*,
which is what the fix will be designed from.
"""

from __future__ import annotations

import logging

import pytest

from agsync.lib.pacs.base import CredentialIdentity, PacsRecordUnavailable
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
TRIGGER_LABEL = "HID 37"
PID = "9390"


class ExportingClient(FakeMillenniumClient):
    """A client whose bulk export names somebody the roster spells otherwise.

    The export carries no cardholder id, so rows are joined to the roster by
    name. Here the export says "George Lampon" and the roster says "Lampon
    Jr, George", so the join lands on the namesake and never on 9390 — who
    is nonetheless the one holding the trigger card.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._formats = [("1", "Wiegand Card"), (TRIGGER, TRIGGER_LABEL)]
        # The export is an optimisation, and the adapter falls back to
        # reading every detail page when it is unavailable. That fallback is
        # how a cardholder the name join cannot reach gets read — and cached
        # — in the first place.
        self.export_broken = False

    def export_cardholders(self):
        if self.export_broken:
            raise RuntimeError("export not ready")
        return (
            "First Name,Last Name,Employee ID,E-Mail,Phone,"
            "Card 1 Encoded Card No.,Card 1 Facility Code,"
            "Card 1 Card Format,Card 1 Active\n"
            f"George,Lampon,1,g@x.test,555,1,99,{TRIGGER_LABEL},True\n"
        )


@pytest.fixture
def stranded(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """9390 carries a trigger card; the roster spells his name differently."""
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format=TRIGGER, active=True,
    )
    for index in (2, 3):
        page = set_slot(page, index, card_id="", card_number="", card_format=None)
    client = ExportingClient(
        roster=[
            {"ID": PID, "IsActive": True, "Name": "Lampon Jr, George"},
            {"ID": "9391", "IsActive": True, "Name": "Lampon, George"},
        ],
        pages={PID: page, "9391": page},
    )
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER,
    )
    return adapter, client


def _cold_start(adapter, client):
    """The cycle that reads everybody, before the export narrows the sweep.

    9390 is only ever visible because of this: it caches his page as
    enrolled, and from then on `_profile_for` refreshes him every cycle
    regardless of the sweep — until something drops the cache.
    """
    client.export_broken = True
    _cycle(adapter, PID)
    client.export_broken = False


def _messages(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _cycle(adapter, *people):
    """What build_snapshot does: the roster, then each person's credentials.

    Reading the credentials is what caches the detail page, so a test that
    only lists people leaves the cache cold and proves nothing about it.
    """
    list(adapter.list_people())
    for pid in people:
        try:
            list(adapter.list_credentials(pid))
        except PacsRecordUnavailable:
            pass


def test_the_sweep_names_the_cardholders_it_leaves_behind(stranded, caplog):
    """The export's name join missing somebody we hold cards on is said aloud."""
    adapter, client = stranded
    # The first cycle is cold, so everybody is read and 9390 is provisioned.
    _cold_start(adapter, client)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "47")])

    with caplog.at_level(logging.INFO):
        list(adapter.list_people())

    assert PID not in adapter._sweep
    assert "we hold cards on are not in this cycle's sweep" in _messages(caplog)
    assert PID in _messages(caplog)


def test_dropping_a_cached_page_says_what_it_costs(stranded, caplog):
    """Writing to a cardholder outside the sweep is what strands them."""
    adapter, client = stranded
    _cold_start(adapter, client)         # reads and caches 9390
    _cycle(adapter, PID)                 # export now drives the sweep

    with caplog.at_level(logging.INFO):
        adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "47")])

    messages = _messages(caplog)
    assert "dropped the cached page for cardholder 9390" in messages
    assert "not in this cycle's sweep" in messages


def test_an_unread_cardholder_we_hold_cards_on_is_named(stranded, caplog):
    """The silent return in _profile_for, for somebody who matters."""
    adapter, client = stranded
    _cold_start(adapter, client)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "47")])
    list(adapter.list_people())

    with caplog.at_level(logging.INFO):
        with pytest.raises(PacsRecordUnavailable):
            list(adapter.list_credentials(PID))

    assert "not reading cardholder 9390 this cycle" in _messages(caplog)


def test_a_stranger_the_sweep_skips_says_nothing(stranded, caplog):
    """The 1,200-odd cardholders we hold nothing on stay quiet."""
    adapter, client = stranded
    _cold_start(adapter, client)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "47")])
    list(adapter.list_people())

    with caplog.at_level(logging.INFO):
        with pytest.raises(PacsRecordUnavailable):
            list(adapter.list_credentials("4242"))

    assert "4242" not in _messages(caplog)


def test_nothing_new_to_write_says_what_was_on_offer(stranded, caplog):
    """The exit a missing watch credential looks exactly like."""
    adapter, client = stranded
    _cold_start(adapter, client)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "47")])

    with caplog.at_level(logging.INFO):
        # Re-offering only what is already there: the ordinary every-cycle
        # case, and the one a never-issued watch is indistinguishable from.
        assert adapter.write_back_credentials(
            PID, "seos", [CredentialIdentity("99", "47")]
        ) is False

    messages = _messages(caplog)
    assert "nothing new to write for cardholder 9390" in messages
    assert "offered 99/47" in messages
