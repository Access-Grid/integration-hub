"""An operator editing a cardholder while we are mid-write.

The window is not export-to-import. Planning reads the cardholder's detail
page by id, and the export only ever supplies the name — so the exposure is
from **our read to our write**, and the write is the import's three-request
handshake.

Closing it entirely would need a conditional write, and Millennium offers
none. What it has instead is a late re-read: the plan is formed from the
first reading, and immediately before the file is built the record is read
again. A slot whose assumptions moved in between is dropped and re-planned
next cycle. That turns "we overwrite whatever is there" into "we notice and
leave it", for everything except the handshake itself.

These tests stand in for the operator by swapping the served page between
the two reads, which is exactly what the adapter cannot distinguish from a
real edit.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11587"
ROSTER = [{"ID": 11587, "IsActive": True, "Name": "Mowatt, Kai"}]


@pytest.fixture
def cardholder(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """Slot 1 holds the operator's marker; slots 2 and 3 are free."""
    def _build():
        page = set_slot(
            millennium_page, 1, card_id="7919", card_number="1",
            facility_code="99", card_format=TRIGGER, active=True,
        )
        for index in (2, 3):
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        client = FakeMillenniumClient(
            roster=ROSTER, pages={PID: page}, export=True
        )
        adapter = make_millennium_adapter(
            client, mode=MODE_SEOS, trigger_card_format=TRIGGER
        )
        list(adapter.list_people())
        return adapter, client, page

    return _build


def _edit(client, set_slot, page, **slot):
    """Have "the operator" change the record before the next read."""
    def _hook(fake, pid, count):
        # Fires before the page is parsed, so swapping on the second read is
        # what puts the edit between the two: the plan sees the original, the
        # check sees the operator's change.
        if count == 2:
            fake._pages[pid] = set_slot(page, **slot)
    client.on_read = _hook


# =====================================================================
# Writing a card
# =====================================================================


def test_a_slot_filled_between_the_two_reads_is_not_overwritten(
    cardholder, set_slot, caplog
):
    """The failure this exists to prevent.

    A phone and a watch, so the write needs two slots: the marker's, and
    slot 2. Planning sees slot 2 free and picks it; the operator puts their
    own card there. Without the re-read we would write over a working
    credential and report success.
    """
    adapter, client, page = cardholder()
    _edit(client, set_slot, page, slot=2, card_id="8001",
          card_number="555", facility_code="1", card_format="1", active=True)

    with caplog.at_level("WARNING"):
        wrote = adapter.write_back_credentials(
            PID, "seos",
            [CredentialIdentity("66", "1238"), CredentialIdentity("66", "1243")],
        )

    assert wrote is False
    assert client.imported == [], "nothing was sent"
    assert client.saved == [], "and it did not fall through to a form save"
    assert "somebody filled it in between" in caplog.text
    # And nothing was recorded, so the next cycle plans afresh rather than
    # believing the card is already placed.
    assert SeosLedger.get(PID, "seos") == []


def test_the_marker_being_replaced_between_the_two_reads_is_noticed(
    cardholder, set_slot, caplog
):
    """The marker is consumed by number, not by position.

    An operator who swaps the placeholder for a real card has made that slot
    somebody's working credential.
    """
    adapter, client, page = cardholder()
    _edit(client, set_slot, page, slot=1, card_id="7919",
          card_number="999", facility_code="99", card_format=TRIGGER, active=True)

    with caplog.at_level("WARNING"):
        wrote = adapter.write_back_credentials(
            PID, "seos", [CredentialIdentity("66", "1238")]
        )

    assert wrote is False
    assert client.imported == []
    assert "the card moved or was replaced" in caplog.text


def test_an_untouched_record_is_written_normally(cardholder):
    """The ordinary path: two reads, nothing changed, the card lands."""
    adapter, client, _ = cardholder()

    assert adapter.write_back_credentials(
        PID, "seos", [CredentialIdentity("66", "1238")]
    ) is True

    assert client.page_reads == [PID, PID], "planned, then re-read"
    assert len(client.imported) == 1
    assert client.imported[0][0].slots[0].card_number == "1238"


def test_an_edit_elsewhere_on_the_record_does_not_block_the_write(
    cardholder, set_slot
):
    """Only the slots we are writing have preconditions.

    An operator working on a different card has not invalidated our plan,
    and refusing then would make a busy install unwritable.
    """
    adapter, client, page = cardholder()
    _edit(client, set_slot, page, slot=3, card_id="8003",
          card_number="777", facility_code="1", card_format="1", active=True)

    assert adapter.write_back_credentials(
        PID, "seos", [CredentialIdentity("66", "1238")]
    ) is True
    assert len(client.imported) == 1


# =====================================================================
# Switching a card off
# =====================================================================


def test_a_card_swapped_before_a_suspend_is_not_switched_off_in_its_place(
    cardholder, set_slot, caplog
):
    """A suspend targets a card, not a slot number.

    Switching off whatever now occupies the position is a revocation nobody
    asked for, applied to somebody else's credential.
    """
    adapter, client, page = cardholder()
    page = set_slot(
        page, 2, card_id="8100", card_number="5001",
        facility_code="66", card_format=TRIGGER, active=True,
    )
    client._pages[PID] = page
    SeosLedger.record(
        PID, "seos", [{"slot": 2, "card_number": "5001", "facility_code": "66"}]
    )
    _edit(client, set_slot, page, slot=2, card_id="8200",
          card_number="7777", facility_code="66", card_format=TRIGGER, active=True)

    with caplog.at_level("WARNING"):
        wrote = adapter.update_credential_status(
            PID, "seos", CredentialStatus.AWAITING_INSTALL
        )

    assert wrote is False
    assert client.imported == []
    assert "the card moved or was replaced" in caplog.text


def test_a_suspend_on_an_untouched_card_goes_through(cardholder, set_slot):
    adapter, client, page = cardholder()
    page = set_slot(
        page, 2, card_id="8100", card_number="5001",
        facility_code="66", card_format=TRIGGER, active=True,
    )
    client._pages[PID] = page
    SeosLedger.record(
        PID, "seos", [{"slot": 2, "card_number": "5001", "facility_code": "66"}]
    )

    assert adapter.update_credential_status(
        PID, "seos", CredentialStatus.AWAITING_INSTALL
    ) is True

    change = client.imported[0][0].slots[0]
    assert (change.index, change.active) == (2, False)


# =====================================================================
# The re-read itself
# =====================================================================


def test_a_failed_re_read_writes_nothing(cardholder, caplog):
    """A reading we could not take is not evidence that nothing changed."""
    adapter, client, _ = cardholder()
    original = client.get_cardholder_form

    def _fail_second(pid):
        if len(client.page_reads) >= 1:
            client.page_reads.append(str(pid))
            raise OSError("[Errno 60] Operation timed out")
        return original(pid)

    client.get_cardholder_form = _fail_second

    with caplog.at_level("ERROR"):
        wrote = adapter.write_back_credentials(
            PID, "seos", [CredentialIdentity("66", "1238")]
        )

    assert wrote is False
    assert client.imported == []
    assert "could not re-read" in caplog.text


def test_the_write_path_reads_twice_and_only_twice(cardholder):
    """One read to plan, one to check. The guard costs exactly one request."""
    adapter, client, _ = cardholder()

    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])

    assert client.page_reads == [PID, PID]
