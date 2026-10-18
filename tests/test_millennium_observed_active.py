"""Reading the Active box, not just writing it.

Millennium's checkbox carries no reason, so these two are the same value
with opposite correct answers:

* off because **we** switched it off, waiting for the holder to install
  their pass — leave it alone; and
* off because **an operator** switched it off — suspend the pass.

Telling them apart needs the box's previous value, and the ledger used to
record only what *we* wrote there. That left one sequence broken, and it is
the sequence that cost a day:

    an operator ticks Active      we recorded nothing
    an operator unticks Active    off -> off, no change, nobody told

The pass stayed live after a deliberate switch-off. These tests pin the
reading half, so a tick is remembered and the untick that follows is a
change.
"""

from __future__ import annotations

import re

import pytest

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11587"
ROSTER = [{"ID": 11587, "IsActive": True, "Name": "Grid, Access"}]


@pytest.fixture
def install(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """A cardholder holding one card of ours, in slot 2."""
    def _build(active: bool):
        page = set_slot(
            millennium_page, 1, card_id="8966", card_number="223",
            facility_code="99", card_format=TRIGGER, active=True,
        )
        page = set_slot(
            page, 2, card_id="9100", card_number="5001",
            facility_code="66", card_format=TRIGGER, active=active,
        )
        page = set_slot(page, 3, card_id="", card_number="", card_format=None)
        page = re.sub(
            r'(name="EmployeeID"[^>]*?value=")[^"]*(")', r"\g<1>1001\g<2>", page
        )
        client = FakeMillenniumClient(
            roster=ROSTER, pages={PID: page}, export=True
        )
        adapter = make_millennium_adapter(
            client, mode=MODE_SEOS, trigger_card_format=TRIGGER
        )
        SeosLedger.record(
            PID, "seos",
            [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
        )
        return adapter, client

    def _cycle(adapter):
        """One pass of the loop: read everybody, then send the writes."""
        list(adapter.list_people())
        for person in adapter.list_people():
            list(adapter.list_credentials(person.id))
        return adapter.flush_writes()

    return _build, _cycle


# =====================================================================
# Reading is recorded
# =====================================================================


def test_a_box_an_operator_ticked_is_remembered(install):
    """The missing half. Nothing of ours wrote this, and it still counts."""
    build, cycle = install
    adapter, _ = build(active=True)
    assert SeosLedger.last_known_active(PID, "seos") == {}, "nothing known yet"

    cycle(adapter)

    assert SeosLedger.last_known_active(PID, "seos") == {2: True}


def test_a_box_an_operator_unticked_is_remembered_too(install):
    build, cycle = install
    adapter, _ = build(active=False)

    cycle(adapter)

    assert SeosLedger.last_known_active(PID, "seos") == {2: False}


def test_the_reading_is_recorded_once_for_the_whole_cycle(install):
    """The ledger is a single encrypted blob, so recording per cardholder
    would rewrite all of it per cardholder, every cycle."""
    build, cycle = install
    adapter, _ = build(active=True)

    cycle(adapter)

    # Nothing left queued, so a second flush has nothing to write.
    assert adapter._observed == {}


# =====================================================================
# And it makes the untick visible
# =====================================================================


def test_switching_a_card_on_is_refused_once_an_operator_has_turned_it_off(install):
    """The whole point.

    The card was on when we last looked, and it is off now, so somebody
    turned it off. Switching it back on would overrule them, silently, every
    cycle — which is exactly what used to happen.
    """
    build, cycle = install

    # Cycle one: the operator has it on, and we notice.
    adapter, client = build(active=True)
    cycle(adapter)
    assert SeosLedger.last_known_active(PID, "seos") == {2: True}

    # Cycle two: they switch it off. The phases read, then decide, and only
    # then does the cycle record what it read — so when phase 4 asks for the
    # card back on, the box's previous value is still on record.
    adapter, client = build(active=False)
    list(adapter.list_people())
    for person in adapter.list_people():
        list(adapter.list_credentials(person.id))

    refused = adapter.update_credential_status(
        PID, "seos", CredentialStatus.ACTIVE
    )
    adapter.flush_writes()

    assert refused is False
    assert client.imported == [], "nothing was written back on"
    # And the new reading is on record for next time.
    assert SeosLedger.last_known_active(PID, "seos") == {2: False}


def test_the_reading_is_recorded_after_the_phases_not_before(install):
    """The ordering the guard depends on, stated on its own.

    Record the reading first and the guard defeats itself: it would compare
    the box's current value against the value it just wrote down, find them
    equal, and conclude nobody had touched anything. The previous value has
    to survive until the phases have had their say.
    """
    build, cycle = install

    adapter, _ = build(active=True)
    cycle(adapter)                       # on, and on record

    adapter, client = build(active=False)
    list(adapter.list_people())
    for person in adapter.list_people():
        list(adapter.list_credentials(person.id))

    # Read, but not yet recorded.
    assert SeosLedger.last_known_active(PID, "seos") == {2: True}
    adapter.flush_writes()
    assert SeosLedger.last_known_active(PID, "seos") == {2: False}


def test_a_card_that_was_never_on_is_still_switched_on(install):
    """The install hand-off has to keep working.

    A card that has only ever been off was not switched off by anybody, so
    there is nothing to overrule.
    """
    build, cycle = install
    adapter, client = build(active=False)
    cycle(adapter)

    assert adapter.update_credential_status(
        PID, "seos", CredentialStatus.ACTIVE
    ) is True
    adapter.flush_writes()

    assert client.imported, "the hand-off happened"


def test_our_own_switch_off_is_not_mistaken_for_an_operators(install):
    """We switched it off to hold it for the install, so we may switch it on.

    This is the pair of the test above it: same observable state, opposite
    answer, and the only difference is who last set it.
    """
    build, cycle = install
    adapter, client = build(active=True)
    cycle(adapter)

    # We hold it off for an uninstalled pass.
    adapter.update_credential_status(PID, "seos", CredentialStatus.AWAITING_INSTALL)
    adapter.flush_writes()
    assert SeosLedger.last_known_active(PID, "seos") == {2: False}

    # The pass installs, so phase 4 asks for it back on. Allowed, because
    # the "off" on record is ours.
    assert adapter.update_credential_status(
        PID, "seos", CredentialStatus.ACTIVE
    ) is True
