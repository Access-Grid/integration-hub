"""The import matches rows by name, so a name has to identify one person.

This is the one property that had to be engineered against before the write
path could move to the import at all. The cardholder save addressed a record
by its id — ``/Cardholders/Cardholders/Index/11666`` — and could not land on
the wrong person however badly it was built. The import has no column for
that id. It matches a row on First Name, Last Name and Card Type, and 42% of
this install's roster collides by name, one of them five ways.

So the import is used only where the name identifies exactly one record on
both sides: one row in the export, which is a dump of the table the import
matches against, and one entry in the roster, which is how the rest of the
adapter knows the cardholder. Anything else goes back through the cardholder
form, which addresses the record by id and is what this adapter did for every
write until now.

That fallback is the point. Declining outright would leave 42% of this
roster unserviceable to protect them from a risk the form does not carry.
What must never happen is the third thing: an ambiguous name going into an
import. The failure that would cause is not an error — it is a working
credential appearing on a stranger's record, silently, on a cycle that
reported success.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"  # "HID 37" in the captured page's format list


@pytest.fixture
def cardholders(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """An adapter over a roster of our choosing, with a card in slot 1.

    Every cardholder is served the same page, which is enough: what is
    being tested is which of them may be addressed, not what is on them.
    """
    def _build(roster, *, export=True):
        page = set_slot(
            millennium_page, 1, card_id="8966", card_number="223",
            facility_code="99", card_format=TRIGGER, active=True,
        )
        for index in (2, 3):
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        client = FakeMillenniumClient(
            roster=roster,
            pages={str(entry["ID"]): page for entry in roster},
            export=export,
        )
        adapter = make_millennium_adapter(
            client, mode=MODE_SEOS, trigger_card_format=TRIGGER
        )
        list(adapter.list_people())
        return adapter, client

    return _build


def _ledger(pid):
    SeosLedger.record(
        pid, "seos", [{"slot": 1, "card_number": "223", "facility_code": "99"}]
    )


UNIQUE = [
    {"ID": 11666, "IsActive": True, "Name": "Mowatt, Kai"},
    {"ID": 11587, "IsActive": True, "Name": "Dorvil, Greg"},
]
COLLIDING = [
    {"ID": 11666, "IsActive": True, "Name": "Mowatt, Kai"},
    {"ID": 11999, "IsActive": True, "Name": "Mowatt, Kai"},
]


# =====================================================================
# A name that identifies one person
# =====================================================================


def test_a_unique_name_is_written_to(cardholders):
    adapter, client = cardholders(UNIQUE)
    _ledger("11666")

    assert adapter.update_credential_status(
        "11666", "seos", CredentialStatus.AWAITING_INSTALL
    ) is True
    assert client.imported, "the write went through the import"


def test_the_row_carries_the_name_the_export_printed(cardholders):
    """Not the one the detail page holds, and not the roster's spelling.

    The export is a dump of the table the import matches against, so its
    own spelling is the only one guaranteed to match. The captured page says
    FirstName=Access LastName=Grid; the row must not say that.
    """
    adapter, client = cardholders(UNIQUE)
    _ledger("11666")

    adapter.update_credential_status(
        "11666", "seos", CredentialStatus.AWAITING_INSTALL
    )

    row = client.imported[-1][0]
    assert (row.identity.first_name, row.identity.last_name) == ("Kai", "Mowatt")


# =====================================================================
# A name that does not
# =====================================================================


def test_a_shared_name_never_reaches_an_import(cardholders, caplog):
    adapter, client = cardholders(COLLIDING)
    _ledger("11666")

    with caplog.at_level("INFO"):
        wrote = adapter.update_credential_status(
            "11666", "seos", CredentialStatus.AWAITING_INSTALL
        )

    assert client.imported == [], "no row carrying an ambiguous name"
    # The card is still switched off — by id, through the form.
    assert wrote is True
    assert [pid for pid, _ in client.saved] == ["11666"]
    assert "shares a name" in caplog.text


def test_a_shared_name_keeps_credentials_on_the_form_path(cardholders, caplog):
    """Both write paths route the same way, not just the status one.

    This is the half that puts a *working card* on a record, so it is the
    half where landing on the wrong person actually opens a door.
    """
    adapter, client = cardholders(COLLIDING)

    with caplog.at_level("INFO"):
        wrote = adapter.write_back_credentials(
            "11666", "seos", [CredentialIdentity("99", "5001")]
        )

    assert client.imported == []
    assert wrote is True
    assert [pid for pid, _ in client.saved] == ["11666"]
    assert "cannot be addressed by name" in caplog.text


def test_neither_of_two_people_sharing_a_name_is_imported(cardholders):
    """Picking the one whose id we happen to hold would be exactly the guess
    this is here to prevent."""
    adapter, client = cardholders(COLLIDING)
    _ledger("11999")

    assert adapter.update_credential_status(
        "11999", "seos", CredentialStatus.AWAITING_INSTALL
    ) is True
    assert client.imported == []
    assert [pid for pid, _ in client.saved] == ["11999"]


# =====================================================================
# No export
# =====================================================================


def test_without_an_export_the_write_goes_through_the_form(cardholders, caplog):
    """Uniqueness is established from the export, so no export means no
    import.

    An install whose export will not run — it has 504'd on this one, with
    images included — keeps working exactly as it did before, rather than
    stopping. The message says which it was, because an operator chasing
    "cannot be addressed by name" should not be sent looking at one name.
    """
    adapter, client = cardholders(UNIQUE, export=False)
    _ledger("11666")

    with caplog.at_level("INFO"):
        wrote = adapter.update_credential_status(
            "11666", "seos", CredentialStatus.AWAITING_INSTALL
        )

    assert wrote is True
    assert client.imported == []
    assert [pid for pid, _ in client.saved] == ["11666"]
    assert "no bulk export this cycle" in caplog.text
    assert "shares a name" not in caplog.text


def test_a_stale_identity_map_does_not_survive_a_failed_export(
    cardholders, millennium_page, set_slot
):
    """The map is rebuilt every cycle, and cleared when the export fails.

    Otherwise a cycle whose export broke would keep writing against names
    read before the roster changed — which is how a renamed or re-created
    cardholder gets somebody else's card.
    """
    adapter, client = cardholders(UNIQUE)
    _ledger("11666")
    assert adapter._import_identity, "a good cycle populates the map"

    client._export = False
    list(adapter.list_people())

    assert adapter._import_identity == {}
    adapter.update_credential_status(
        "11666", "seos", CredentialStatus.AWAITING_INSTALL
    )
    assert client.imported == [], "no row built from a map read last cycle"


# =====================================================================
# The cap
# =====================================================================


def test_the_adapter_never_builds_a_file_larger_than_one_cardholder(cardholders):
    """Each write is one cardholder's row.

    The client's row cap is the backstop; this is the shape that makes it
    unreachable from here. A single import that could span the roster would
    put every cardholder in range of one bad plan.
    """
    adapter, client = cardholders(UNIQUE)
    _ledger("11666")

    adapter.update_credential_status(
        "11666", "seos", CredentialStatus.AWAITING_INSTALL
    )

    assert [len(rows) for rows in client.imported] == [1]


# =====================================================================
# Dry run
# =====================================================================


def test_a_dry_run_sends_no_import(cardholders, monkeypatch, caplog):
    """The switch has to cover the new transport as well as the old one.

    A dry run that still uploaded a file would be the worst possible
    version of this feature: the setting defaults to on, so an install that
    has never been told otherwise would be writing to a customer's access
    control while reporting that it was not.
    """
    from agsync import dryrun

    adapter, client = cardholders(UNIQUE)
    _ledger("11666")
    monkeypatch.setattr(dryrun, "active", lambda: True)

    with caplog.at_level("INFO"):
        assert adapter.update_credential_status(
            "11666", "seos", CredentialStatus.AWAITING_INSTALL
        ) is True

    assert client.imported == []
    assert client.saved == []
    assert dryrun.PREFIX in caplog.text


def test_a_dry_run_sends_no_import_for_credentials_either(
    cardholders, monkeypatch
):
    from agsync import dryrun

    adapter, client = cardholders(UNIQUE)
    monkeypatch.setattr(dryrun, "active", lambda: True)

    assert adapter.write_back_credentials(
        "11666", "seos", [CredentialIdentity("99", "5001")]
    ) is True
    assert client.imported == []
    assert client.saved == []
