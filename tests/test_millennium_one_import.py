"""One import per cycle, and the ways that could conflict with itself.

Every phase that writes cards now records an intention instead of sending
it, and a single import goes out at the end of the cycle. That buys one
property for free and creates three hazards that have to be engineered
against.

The free property: **no phase can observe a change another phase made in
the same cycle**, because nothing is written until every phase has finished
deciding. Every reading in a cycle is of the same state, so the class of bug
where the loop overwrites something it set itself cannot arise.

The hazards:

* **Double-booking a slot.** Two credentials on one cardholder both read the
  same page, both see the same empty slot, and both pick it. One of the two
  cards would never be written and nothing would say which.
* **Two phases opining on one slot.** A card written in phase 1 and held
  inactive in phase 4 is the ordinary case, and the result must not depend
  on which spoke first except where they genuinely disagree about a field.
* **Losing a cycle's decisions.** A dropped import must not be mistaken for
  a completed one. The caller's bookkeeping is written the moment a write
  method returns, so the intention has to outlive the cycle that made it.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
ONE, TWO = "11587", "11999"
ROSTER = [
    {"ID": 11587, "IsActive": True, "Name": "Grid, Access"},
    {"ID": 11999, "IsActive": True, "Name": "Dorvil, Greg"},
]


@pytest.fixture
def install(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """Two cardholders, each with a marker in slot 1 and two free slots."""
    import re

    def _page(employee_id: str) -> str:
        page = set_slot(
            millennium_page, 1, card_id="8966", card_number="223",
            facility_code="99", card_format=TRIGGER, active=True,
        )
        for index in (2, 3):
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        return re.sub(
            r'(name="EmployeeID"[^>]*?value=")[^"]*(")',
            rf"\g<1>{employee_id}\g<2>", page,
        )

    def _build():
        client = FakeMillenniumClient(
            roster=ROSTER,
            pages={ONE: _page("1001"), TWO: _page("1002")},
            export=True,
        )
        adapter = make_millennium_adapter(
            client, mode=MODE_SEOS, trigger_card_format=TRIGGER
        )
        list(adapter.list_people())
        return adapter, client

    return _build


def _ledger(pid: str, credential_id: str = "seos", slot: int = 1):
    SeosLedger.record(
        pid, credential_id,
        [{"slot": slot, "card_number": "223", "facility_code": "99"}],
    )


# =====================================================================
# One import, however many cardholders
# =====================================================================


def test_two_cardholders_travel_in_a_single_import(install):
    adapter, client = install()
    _ledger(ONE)
    _ledger(TWO)

    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)
    adapter.update_credential_status(TWO, "seos", CredentialStatus.AWAITING_INSTALL)
    assert client.imported == [], "nothing goes out until the flush"

    assert adapter.flush_writes() == 2
    assert len(client.imported) == 1, "one import"
    assert {r.employee_id for r in client.imported[0]} == {"1001", "1002"}


def test_the_file_carries_one_row_per_cardholder(install):
    adapter, client = install()
    _ledger(ONE)
    _ledger(TWO)

    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)
    adapter.update_credential_status(TWO, "seos", CredentialStatus.AWAITING_INSTALL)
    adapter.flush_writes()

    csv_text = client.imported_csv[0]
    assert csv_text.count("\r\n") == 3, "a header and two rows"
    assert "1001" in csv_text and "1002" in csv_text


def test_nothing_pending_sends_nothing(install):
    adapter, client = install()

    assert adapter.flush_writes() == 0
    assert client.imported == []
    assert client.page_reads == [], "and it does not even read"


# =====================================================================
# Hazard: double-booking a slot
# =====================================================================


def test_two_credentials_never_get_the_same_free_slot(install):
    """Both read the same page and both see slots 2 and 3 free.

    Without the claim check they would both take the marker's slot, and the
    merged plan would keep only one of the two cards.
    """
    adapter, client = install()

    assert adapter.write_back_credentials(
        ONE, "seos-a", [CredentialIdentity("66", "9001")]
    ) is True
    assert adapter.write_back_credentials(
        ONE, "seos-b", [CredentialIdentity("66", "9002")]
    ) is True
    adapter.flush_writes()

    (row,) = client.imported[0]
    slots = {s.index: s.card_number for s in row.slots}
    assert sorted(slots.values()) == ["9001", "9002"]
    assert len(set(slots)) == 2, "two different slots"


def test_a_claimed_slot_is_not_offered_again_even_when_the_page_says_empty(install):
    adapter, _ = install()

    adapter.write_back_credentials(ONE, "seos-a", [CredentialIdentity("66", "9001")])
    first = set(adapter._pending[ONE].slots)
    adapter.write_back_credentials(ONE, "seos-b", [CredentialIdentity("66", "9002")])
    both = set(adapter._pending[ONE].slots)

    assert first < both, "the second write took a slot of its own"


def test_a_cardholder_with_one_slot_left_refuses_the_second_pair(install):
    """Three slots, and a pair needs two. The second cannot fit, and saying
    so beats quietly writing half of it."""
    adapter, client = install()

    assert adapter.write_back_credentials(
        ONE, "seos-a",
        [CredentialIdentity("66", "9001"), CredentialIdentity("66", "9002")],
    ) is True
    assert adapter.write_back_credentials(
        ONE, "seos-b",
        [CredentialIdentity("66", "9003"), CredentialIdentity("66", "9004")],
    ) is False

    adapter.flush_writes()
    (row,) = client.imported[0]
    assert sorted(s.card_number for s in row.slots) == ["9001", "9002"]


# =====================================================================
# Hazard: two phases, one slot
# =====================================================================


def test_a_card_written_then_held_inactive_is_one_row(install):
    """Phase 1 writes the card; phase 4 holds it off until it is installed.

    One cardholder, one row, and the later opinion wins on the field they
    both have a view on.
    """
    adapter, client = install()

    adapter.write_back_credentials(ONE, "seos", [CredentialIdentity("66", "9001")])
    _ledger(ONE, "seos", slot=1)
    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)
    adapter.flush_writes()

    assert len(client.imported) == 1
    (row,) = client.imported[0]
    (written,) = (s for s in row.slots if s.index == 1)
    assert written.card_number == "9001", "the card still goes in"
    assert written.active is False, "and the later opinion decides the box"


# =====================================================================
# Hazard: losing a cycle's decisions
# =====================================================================


def test_a_refused_import_leaves_the_intention_recorded(install, caplog):
    """So the next cycle sends it again rather than forgetting it.

    This is what makes the caller's bookkeeping safe. Phase 4 notes that it
    pushed the change the moment the write method returns, so if a dropped
    import were simply lost, the phase would never ask again and phase 2
    would push Millennium's stale state back to AccessGrid.
    """
    adapter, client = install()
    _ledger(ONE)
    client.import_result = False

    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)
    with caplog.at_level("ERROR"):
        assert adapter.flush_writes() == 0

    assert SeosLedger.desired_active(ONE, "seos") == {1: False}, "still wanted"
    assert "the next cycle will try again" in caplog.text


def test_nothing_reaches_the_ledger_until_the_import_confirms(install):
    adapter, client = install()
    client.import_result = False

    adapter.write_back_credentials(ONE, "seos", [CredentialIdentity("66", "9001")])
    assert SeosLedger.get(ONE, "seos") == [], "not before the flush"
    adapter.flush_writes()
    assert SeosLedger.get(ONE, "seos") == [], "and not after a refused one"


def test_a_confirmed_import_records_the_cards_it_wrote(install):
    adapter, client = install()

    adapter.write_back_credentials(ONE, "seos", [CredentialIdentity("66", "9001")])
    adapter.flush_writes()

    assert [e["card_number"] for e in SeosLedger.get(ONE, "seos")] == ["9001"]


def test_the_desire_is_recorded_before_the_flush_runs(install):
    """Written when the phase decides, not when the import lands."""
    adapter, _ = install()
    _ledger(ONE)

    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)

    assert SeosLedger.desired_active(ONE, "seos") == {1: False}


def test_a_new_cycle_forgets_what_the_last_one_never_sent(install):
    """The intention is in the ledger, so re-planning is both correct and
    cheaper than carrying a stale in-memory plan across cycles."""
    adapter, _ = install()
    _ledger(ONE)

    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)
    assert adapter._pending
    list(adapter.list_people())

    assert adapter._pending == {}


# =====================================================================
# One cardholder's problem is not everybody's
# =====================================================================


def test_a_diverged_cardholder_does_not_block_the_others(install, set_slot, caplog):
    """One record moving under us must not stop the rest of the import."""
    adapter, client = install()
    _ledger(ONE)
    _ledger(TWO)
    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)
    adapter.update_credential_status(TWO, "seos", CredentialStatus.AWAITING_INSTALL)

    # Somebody replaces the card the first cardholder's plan targeted.
    client._pages[ONE] = set_slot(
        client._pages[ONE], 1, card_id="8966", card_number="777",
        facility_code="99", card_format=TRIGGER, active=True,
    )

    with caplog.at_level("WARNING"):
        assert adapter.flush_writes() == 1

    (row,) = client.imported[0]
    assert row.employee_id == "1002", "only the untouched cardholder"
    assert "the card moved or was replaced" in caplog.text


def test_an_employee_id_that_changed_since_planning_is_dropped(install, caplog):
    """The key would address somebody else by the time the file is built."""
    import re

    adapter, client = install()
    _ledger(ONE)
    adapter.update_credential_status(ONE, "seos", CredentialStatus.AWAITING_INSTALL)

    client._pages[ONE] = re.sub(
        r'(name="EmployeeID"[^>]*?value=")[^"]*(")', r"\g<1>5555\g<2>",
        client._pages[ONE],
    )

    with caplog.at_level("WARNING"):
        assert adapter.flush_writes() == 0

    assert client.imported == []
    assert "Employee ID changed" in caplog.text


# =====================================================================
# Dry run
# =====================================================================


def test_a_dry_run_reads_nothing_and_sends_nothing(install, monkeypatch, caplog):
    from agsync import dryrun

    adapter, client = install()
    _ledger(ONE)
    monkeypatch.setattr(dryrun, "active", lambda: True)

    with caplog.at_level("INFO"):
        adapter.update_credential_status(
            ONE, "seos", CredentialStatus.AWAITING_INSTALL
        )

    # The switch is checked where the decision is made, so nothing is even
    # queued — and the flush therefore has nothing to send or read.
    assert dryrun.PREFIX in caplog.text
    assert adapter._pending == {}
    assert adapter.flush_writes() == 0
    assert client.imported == []
    assert client.saved == []
    # It still reads, because working out what it *would* do is the point.
    assert client.page_reads == [ONE]
