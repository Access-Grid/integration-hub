"""The import matches a row on Employee ID, and nothing else.

That field is what makes a write addressable at all. It is unique across
cardholders on this install, and verified on the live install to be the key:
a row whose Employee ID matches is updated, and a row with a blank one does
not work at all.

Two things follow, and both are guarded here.

**A cardholder with no Employee ID cannot be imported to.** Not "is imported
to badly" — the import simply has no way to find them. They go through the
cardholder screen instead, which addresses a record by its own number and
cannot land on the wrong person.

**A duplicated Employee ID must never reach an import.** The field is
operator-entered free text, so the uniqueness it is relied on for is
maintained by people typing carefully. Trusting that without checking is how
one row ends up switching off a stranger's card, so each cycle's export is
counted and a duplicate takes both cardholders off the import path.

The key is read off the cardholder's own record rather than matched out of
the export, so there is no join between the two to get wrong.
"""

from __future__ import annotations

import re

import pytest

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11587"
ROSTER = [{"ID": 11587, "IsActive": True, "Name": "Grid, Access"}]


def _with_employee_id(page: str, value: str) -> str:
    return re.sub(
        r'(name="EmployeeID"[^>]*?value=")[^"]*(")', rf"\g<1>{value}\g<2>", page
    )


@pytest.fixture
def cardholder(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """A cardholder with a card in slot 1 and two free slots."""
    def _build(employee_id: str = "123444", export: bool = True, roster=None):
        page = set_slot(
            millennium_page, 1, card_id="8966", card_number="223",
            facility_code="99", card_format=TRIGGER, active=True,
        )
        for index in (2, 3):
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        page = _with_employee_id(page, employee_id)
        people = roster or ROSTER
        client = FakeMillenniumClient(
            roster=people,
            pages={str(p["ID"]): page for p in people},
            export=export,
        )
        adapter = make_millennium_adapter(
            client, mode=MODE_SEOS, trigger_card_format=TRIGGER
        )
        list(adapter.list_people())
        return adapter, client

    return _build


def _ledger():
    SeosLedger.record(
        PID, "seos", [{"slot": 1, "card_number": "223", "facility_code": "99"}]
    )


# =====================================================================
# A cardholder who has one
# =====================================================================


def test_the_row_is_keyed_on_the_employee_id(cardholder):
    adapter, client = cardholder(employee_id="22344")
    _ledger()

    assert adapter.update_credential_status(
        PID, "seos", CredentialStatus.AWAITING_INSTALL
    ) is True

    assert client.imported[-1][0].employee_id == "22344"
    assert "22344" in client.imported_csv[-1]


def test_the_key_is_read_off_the_record_not_matched_from_the_export(cardholder):
    """So there is no join to get wrong.

    The export has no cardholder record number, so pairing its rows to the
    roster has to go through names — and 42% of this roster collides. Taking
    the key from the cardholder's own page skips that entirely.
    """
    adapter, client = cardholder(employee_id="99887")
    _ledger()

    adapter.update_credential_status(PID, "seos", CredentialStatus.AWAITING_INSTALL)

    assert client.imported[-1][0].employee_id == "99887"


# =====================================================================
# A cardholder who does not
# =====================================================================


def test_a_blank_employee_id_goes_through_the_cardholder_screen(cardholder, caplog):
    """Verified on the live install: a blank key does not work at all.

    The write still happens, by record number, through the screen an operator
    would use. Refusing outright would leave those cardholders unserviceable
    to avoid a risk the fallback does not carry.
    """
    adapter, client = cardholder(employee_id="")
    _ledger()

    with caplog.at_level("INFO"):
        wrote = adapter.update_credential_status(
            PID, "seos", CredentialStatus.AWAITING_INSTALL
        )

    assert client.imported == [], "no row carrying a blank key"
    assert wrote is True
    assert [pid for pid, _ in client.saved] == [PID]
    assert "no Employee ID" in caplog.text


def test_a_blank_employee_id_also_keeps_credentials_off_the_import(cardholder):
    """Both write paths route the same way.

    This is the half that puts a *working card* on a record, so it is the
    half where landing on the wrong one actually opens a door.
    """
    adapter, client = cardholder(employee_id="")

    assert adapter.write_back_credentials(
        PID, "seos", [CredentialIdentity("99", "5001")]
    ) is True
    assert client.imported == []
    assert [pid for pid, _ in client.saved] == [PID]


# =====================================================================
# A cardholder who shares one
# =====================================================================


DOUBLED = [
    {"ID": 11587, "IsActive": True, "Name": "Grid, Access"},
    {"ID": 11999, "IsActive": True, "Name": "Twin, Evil"},
]


def test_a_duplicated_employee_id_never_reaches_an_import(cardholder, caplog):
    """Both cardholders are served the same Employee ID here, so the export
    reports it twice and neither may be written to by key."""
    adapter, client = cardholder(employee_id="22344", roster=DOUBLED)
    _ledger()

    with caplog.at_level("INFO"):
        wrote = adapter.update_credential_status(
            PID, "seos", CredentialStatus.AWAITING_INSTALL
        )

    assert client.imported == []
    assert wrote is True
    assert [pid for pid, _ in client.saved] == [PID]
    assert "names more than one cardholder" in caplog.text


def test_a_duplicate_is_reported_loudly(cardholder, caplog):
    """The field is operator-entered, so this is a data problem somebody has
    to fix rather than something to work around quietly."""
    with caplog.at_level("ERROR"):
        cardholder(employee_id="22344", roster=DOUBLED)

    assert "name more than one cardholder" in caplog.text


def test_a_duplicate_does_not_survive_into_a_later_cycle(cardholder):
    """The count is rebuilt from each cycle's export, so resolving the
    duplicate in Millennium puts the cardholder back on the import path."""
    adapter, client = cardholder(employee_id="22344", roster=DOUBLED)
    _ledger()
    assert adapter._duplicate_employee_ids == {"22344"}

    # The operator fixes the collision; the next cycle sees one of each.
    client._pages["11999"] = _with_employee_id(client._pages["11999"], "55555")
    list(adapter.list_people())

    assert adapter._duplicate_employee_ids == set()
    adapter.update_credential_status(PID, "seos", CredentialStatus.AWAITING_INSTALL)
    assert client.imported, "addressable again"


# =====================================================================
# Dry run
# =====================================================================


def test_a_dry_run_sends_no_import(cardholder, monkeypatch, caplog):
    """The switch has to cover this transport as well as the old one.

    It defaults to on, so an install never told otherwise would be writing
    to a customer's access control while reporting that it was not.
    """
    from agsync import dryrun

    adapter, client = cardholder()
    _ledger()
    monkeypatch.setattr(dryrun, "active", lambda: True)

    with caplog.at_level("INFO"):
        assert adapter.update_credential_status(
            PID, "seos", CredentialStatus.AWAITING_INSTALL
        ) is True

    assert client.imported == []
    assert client.saved == []
    assert dryrun.PREFIX in caplog.text


def test_a_dry_run_sends_no_import_for_credentials_either(cardholder, monkeypatch):
    from agsync import dryrun

    adapter, client = cardholder()
    monkeypatch.setattr(dryrun, "active", lambda: True)

    assert adapter.write_back_credentials(
        PID, "seos", [CredentialIdentity("99", "5001")]
    ) is True
    assert client.imported == []
    assert client.saved == []
