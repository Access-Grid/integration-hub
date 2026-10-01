"""A cardholder we hold a card on is read every cycle, whatever the export says.

The bulk export decides which detail pages to read, and it cannot be relied
on to name the people who matter most:

  * It names cardholders by the card format they carry, so somebody drops out
    of it the moment their last trigger card is deleted — which is precisely
    the event that authorises deleting their pass.
  * It carries no cardholder id, so rows are joined to the roster by name,
    and a roster entry spelled differently never lands at all.

Either way the cardholder used to be carried only by their cached detail
page, which a restart or any write of ours drops. After that they were unread
every cycle, phase 3 rightly declined to act on a record it had not read, and
nothing said so: the pass stayed live, the slot stayed held, and the holder's
watch credential was never written. Observed live on cardholder 12448, whose
pass survived having every one of her format-8 cards deleted.

So the ledger, not the export, decides who is read: a card we wrote is a card
we are answerable for.
"""

from __future__ import annotations

import logging
import re

import pytest

from agsync.lib.pacs.base import CredentialIdentity, PacsRecordUnavailable
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
TRIGGER_LABEL = "HID 37"
PID = "9390"


class ExportingClient(FakeMillenniumClient):
    """A client whose export names somebody the roster spells otherwise.

    The export says "George Lampon"; the roster calls 9390 "Lampon Jr,
    George". So the name join lands on his namesake 9391 and never on him,
    though he is the one holding the trigger card. `carries_trigger` switches
    the export row off entirely, standing in for the operator deleting the
    last format-8 card from the cardholder.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._formats = [("1", "Wiegand Card"), (TRIGGER, TRIGGER_LABEL)]
        self.carries_trigger = True
        # The adapter falls back to reading every detail page when the export
        # is unavailable. That fallback is the only reason a cardholder the
        # name join cannot reach ever got read before this fix.
        self.export_broken = False

    def export_cardholders(self):
        if self.export_broken:
            raise RuntimeError("export not ready")
        fmt = TRIGGER_LABEL if self.carries_trigger else "Wiegand Card"
        return (
            "First Name,Last Name,Employee ID,E-Mail,Phone,"
            "Card 1 Encoded Card No.,Card 1 Facility Code,"
            "Card 1 Card Format,Card 1 Active\n"
            f"George,Lampon,1,g@x.test,555,1,99,{fmt},True\n"
        )


def _page(millennium_page, set_slot, *, trigger: bool):
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format=TRIGGER if trigger else "1", active=True,
    )
    for index in (2, 3):
        page = set_slot(page, index, card_id="", card_number="", card_format=None)
    return page


@pytest.fixture
def unnamed(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """9390 carries a trigger card; the export's name join cannot reach him."""
    client = ExportingClient(
        roster=[
            {"ID": PID, "IsActive": True, "Name": "Lampon Jr, George"},
            {"ID": "9391", "IsActive": True, "Name": "Lampon, George"},
        ],
        pages={
            PID: _page(millennium_page, set_slot, trigger=True),
            "9391": _page(millennium_page, set_slot, trigger=True),
        },
    )
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER,
    )
    return adapter, client


def _messages(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _cycle(adapter, *people):
    """What build_snapshot does: the roster, then each person's credentials."""
    list(adapter.list_people())
    for pid in people:
        try:
            list(adapter.list_credentials(pid))
        except PacsRecordUnavailable:
            pass


def _own_a_card(adapter, client, pid=PID):
    """Get a card of ours onto the cardholder, which is what makes them ours.

    Provisioning him at all takes the cold sweep, since the name join cannot
    reach him — which is how he came to be tracked on the live install too.
    The trailing roster pass refreshes the adapter's view of the ledger, as
    the start of every real cycle does.
    """
    client.export_broken = True
    _cycle(adapter, pid)
    client.export_broken = False
    assert adapter.write_back_credentials(
        pid, "seos", [CredentialIdentity("99", "47")]
    ) is True
    list(adapter.list_people())


# =====================================================================
# The regression
# =====================================================================


def test_a_cardholder_we_hold_a_card_on_is_swept(unnamed):
    """Even though the export's name join never reaches him."""
    adapter, client = unnamed
    _own_a_card(adapter, client)

    list(adapter.list_people())

    assert PID in adapter._sweep
    assert PID in adapter._ours


def test_they_are_read_with_nothing_cached(unnamed, caplog):
    """The state after a restart, which is what made this permanent."""
    adapter, client = unnamed
    _own_a_card(adapter, client)
    # A restart loses every cached page; the ledger survives it.
    adapter._profiles.clear()

    with caplog.at_level(logging.INFO):
        list(adapter.list_people())
        creds = list(adapter.list_credentials(PID))

    # Their own credential resolves. The fake serves a static page, so the
    # marker still appears where our card overwrote it and is read as a
    # request for a second pass; this test is about being read at all.
    assert "seos" in {c.id for c in creds}
    assert "not reading cardholder" not in _messages(caplog)


def test_they_are_read_after_their_last_trigger_card_is_deleted(
    unnamed, millennium_page, set_slot
):
    """The event that authorises deleting the pass must not hide itself.

    Losing the trigger card drops the cardholder out of the export, so this
    used to be the one thing we could never see.
    """
    adapter, client = unnamed
    _own_a_card(adapter, client)

    # The operator deletes every format-8 card, and the app restarts.
    client._pages[PID] = _page(millennium_page, set_slot, trigger=False)
    client.carries_trigger = False
    adapter._profiles.clear()

    list(adapter.list_people())
    assert PID in adapter._sweep
    # Read, and honestly reporting no credential — which is what lets phase 3
    # delete the pass. The alternative, PacsRecordUnavailable, is what left it
    # live forever.
    assert list(adapter.list_credentials(PID)) == []


def test_a_write_that_drops_the_cache_no_longer_strands_them(unnamed, caplog):
    """Both write paths evict the cached page; the ledger covers the gap."""
    adapter, client = unnamed
    _own_a_card(adapter, client)

    with caplog.at_level(logging.INFO):
        # Writing again drops the cached page, as retirement does too.
        adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "52")])
        list(adapter.list_people())
        creds = list(adapter.list_credentials(PID))

    # Their own credential resolves. The fake serves a static page, so the
    # marker still appears where our card overwrote it and is read as a
    # request for a second pass; this test is about being read at all.
    assert "seos" in {c.id for c in creds}
    assert "this is the last cycle they are visible" not in _messages(caplog)


# =====================================================================
# What the fix must not cost
# =====================================================================


def test_a_stranger_is_still_not_read(unnamed, caplog):
    """The roster is 2,059 people; the ledger holds a handful."""
    adapter, client = unnamed
    _own_a_card(adapter, client)
    list(adapter.list_people())

    assert "4242" not in adapter._sweep
    with caplog.at_level(logging.INFO):
        with pytest.raises(PacsRecordUnavailable):
            list(adapter.list_credentials("4242"))

    assert "4242" not in _messages(caplog)


def test_the_sweep_is_the_export_plus_the_ledger_and_nothing_else(unnamed):
    """No accidental return to reading every page."""
    adapter, client = unnamed
    _own_a_card(adapter, client)
    list(adapter.list_people())

    # 9391 from the export's name join, 9390 from the ledger.
    assert adapter._sweep == {PID, "9391"}


# =====================================================================
# The diagnostics that found this, kept so they cannot rot
# =====================================================================


def test_dropping_a_cached_page_is_still_reported(unnamed, caplog):
    adapter, client = unnamed
    client.export_broken = True
    _cycle(adapter, PID)
    client.export_broken = False

    with caplog.at_level(logging.INFO):
        adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "47")])

    assert "dropped the cached page for cardholder 9390" in _messages(caplog)


def test_nothing_new_to_write_says_what_was_on_offer(unnamed, caplog):
    """The exit a missing watch credential looks exactly like."""
    adapter, client = unnamed
    _own_a_card(adapter, client)

    with caplog.at_level(logging.INFO):
        assert adapter.write_back_credentials(
            PID, "seos", [CredentialIdentity("99", "47")]
        ) is False

    messages = _messages(caplog)
    assert "nothing new to write for cardholder 9390" in messages
    assert "offered 99/47" in messages


def test_an_unread_cardholder_we_hold_cards_on_is_still_named(unnamed, caplog):
    """The warning is now unreachable through the sweep, not deleted.

    It still guards the other ways a page can go unread — a read that failed
    and is backing off, most of all — so it is exercised directly rather than
    left to rot.
    """
    adapter, client = unnamed
    _own_a_card(adapter, client)
    adapter._profiles.clear()
    adapter._sweep = set()

    with caplog.at_level(logging.INFO):
        assert adapter._profile_for(PID) is None

    assert re.search(r"not reading cardholder 9390 this cycle", _messages(caplog))
