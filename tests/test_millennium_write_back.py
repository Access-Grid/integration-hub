"""Writing a just-issued Seos pass's cards into Millennium.

The trigger card's row stays until the import confirms AccessGrid's card
has replaced it, so a skipped import or a dry run never makes the next
cycle mistake the trigger card for a new request.
"""

from __future__ import annotations

import csv
import io
import re

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, PacsCards
from agsync.lib.pacs.millennium_ultra.import_file import ImportLog
from tests._fakes import FakeMillenniumClient, export_row

PID = "33137"
MARKER = {
    "card_number": "1", "facility_code": "99", "card_format": "HID 37", "active": False,
}


def _adapter(make_millennium_adapter, *slots, pair=False, skipped=()):
    client = FakeMillenniumClient(rows=[export_row(PID, "Ada", "Lovelace", slots=slots)])
    client.imports = []

    def import_cardholders(imported):
        client.imports.append(imported)
        return ImportLog({}, [f"Cardholder 'Employee ID:{p}' skipped" for p in skipped], [])

    client.import_cardholders = import_cardholders
    adapter = make_millennium_adapter(client, mode=MODE_SEOS, pair=pair)
    list(adapter.list_people())
    return adapter, client


def _issued(adapter):
    """The cycle that issues the pass: list_credentials records the marker."""
    [cred] = adapter.list_credentials(PID)
    return cred.id


def _row(imported) -> dict[str, str]:
    return next(csv.DictReader(io.StringIO(imported.csv)))


def test_the_first_card_overwrites_the_trigger_card(make_millennium_adapter):
    marker = {**MARKER, "access_levels": ["MASTER 1"]}
    adapter, client = _adapter(make_millennium_adapter, marker)
    cid = _issued(adapter)
    assert adapter.write_back_credentials(PID, cid, [CredentialIdentity("66", "5001")])

    adapter.finish_cycle()
    row = _row(client.imports[0])
    assert (row["Card1EncodedCardNo."], row["Card1FacilityCode"]) == ("5001", "66")
    assert row["Card1CardFormat"] == "HID 37"
    assert row["Card1Active"] == "False", "the install gate switches it on"
    assert re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M", row["Card1ActivationDate"])
    assert row["Card1AccessLevel1"] == "MASTER 1"
    assert PacsCards.get(PID, cid) == [
        {"slot": 1, "card_number": "5001", "facility_code": "66", "active": False},
    ]


def test_a_pairs_second_card_goes_into_an_empty_slot(make_millennium_adapter):
    adapter, client = _adapter(make_millennium_adapter, MARKER, pair=True)
    cid = _issued(adapter)
    adapter.write_back_credentials(
        PID, cid, [CredentialIdentity("66", "5001"), CredentialIdentity("66", "5002")],
    )
    adapter.finish_cycle()
    row = _row(client.imports[0])
    assert (row["Card1EncodedCardNo."], row["Card2EncodedCardNo."]) == ("5001", "5002")


def test_a_pass_already_written_is_not_written_again(make_millennium_adapter):
    """Phase 4 re-offers identities every cycle; only a new pass takes them."""
    PacsCards.record(PID, "card", [{"slot": 1, "card_number": "5001", "facility_code": "66"}])
    adapter, client = _adapter(make_millennium_adapter, {**MARKER, "card_number": "5001",
                                                          "facility_code": "66"})
    assert not adapter.write_back_credentials(PID, "card", [CredentialIdentity("66", "5009")])
    adapter.finish_cycle()
    assert client.imports == []


def test_a_skipped_import_leaves_one_pass_and_tries_again(make_millennium_adapter):
    adapter, _ = _adapter(make_millennium_adapter, MARKER, skipped=[PID])
    cid = _issued(adapter)
    adapter.write_back_credentials(PID, cid, [CredentialIdentity("66", "5001")])
    adapter.finish_cycle()

    # Next cycle: Millennium still shows the trigger card in slot 1.
    assert [c.id for c in adapter.list_credentials(PID)] == [cid], "no second pass"
    pending = sorted(e.get("pending") for e in PacsCards.get(PID, cid))
    assert pending == ["insert", "write"]


def test_a_card_waiting_to_be_switched_still_counts_towards_the_pass(make_millennium_adapter):
    """Only cards not yet written are left out of the pass's status."""
    PacsCards.record(PID, "card", [
        {"slot": 1, "card_number": "5001", "facility_code": "66", "pending": "deactivate"},
    ])
    adapter, _ = _adapter(make_millennium_adapter, {**MARKER, "card_number": "5001",
                                                     "facility_code": "66", "active": False})
    [cred] = adapter.list_credentials(PID)
    assert cred.status is CredentialStatus.SUSPENDED


def test_a_card_millennium_skipped_stays_pending_and_is_an_error(make_millennium_adapter, caplog):
    """Seen live: the import skipped the pair's second card, and it was
    marked done anyway."""
    import logging

    adapter, client = _adapter(make_millennium_adapter, MARKER, pair=True)
    client.import_cardholders = lambda imported: ImportLog(
        {}, [], [], skips=["Card '5002, 66' has been skipped (Import rule is Only Update)"],
    )
    cid = _issued(adapter)
    adapter.write_back_credentials(
        PID, cid, [CredentialIdentity("66", "5001"), CredentialIdentity("66", "5002")],
    )
    with caplog.at_level(logging.ERROR):
        adapter.finish_cycle()
    assert "Millennium did not take card 66/5002 for 33137" in caplog.text
    by_card = {e["card_number"]: e for e in PacsCards.get(PID, cid)}
    assert by_card["5002"]["pending"] == "insert", "tried again next cycle"
    assert "pending" not in by_card["5001"]


def test_a_retried_second_card_copies_the_levels_from_the_first(make_millennium_adapter):
    """Seen live: the second card was skipped once, and on the retry the
    trigger card was gone, so it went in with no access levels."""
    first = {"card_number": "5001", "facility_code": "66", "card_format": "HID 37",
             "active": False, "access_levels": ["MASTER 1"]}
    adapter, client = _adapter(make_millennium_adapter, first, pair=True)
    PacsCards.record(PID, "card", [
        {"slot": 1, "card_number": "5001", "facility_code": "66", "active": False},
        {"slot": 2, "card_number": "5002", "facility_code": "66", "pending": "insert"},
    ])
    adapter.finish_cycle()
    row = _row(client.imports[0])
    assert row["Card2EncodedCardNo."] == "5002"
    assert row["Card2AccessLevel1"] == "MASTER 1"
