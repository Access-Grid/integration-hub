"""Switching a card's Active box: recorded during the cycle, sent at its end.

update_credential_status only records what each card should be;
finish_cycle sends every cardholder's changes in one import and keeps what
the import log confirms.
"""

from __future__ import annotations

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, PacsCards
from agsync.lib.pacs.millennium_ultra.import_file import ImportLog
from tests._fakes import FakeMillenniumClient, export_row

PID = "33137"


def _adapter(make_millennium_adapter, active=True, skipped=()):
    """A cardholder whose pass `card` rests on card 66/5001 in slot 2."""
    card = {"card_number": "5001", "facility_code": "66", "card_format": "HID 37",
            "active": active}
    client = FakeMillenniumClient(rows=[export_row(PID, "Ada", "Lovelace", slots=[None, card])])
    client.imports = []

    def import_cardholders(imported):
        client.imports.append(imported)
        warnings = [f"Cardholder 'Employee ID:{pid}' has been skipped" for pid in skipped]
        return ImportLog(counts={}, warnings=warnings, errors=[])

    client.import_cardholders = import_cardholders
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    PacsCards.record(PID, "card", [{"slot": 2, "card_number": "5001", "facility_code": "66"}])
    list(adapter.list_people())
    return adapter, client


def test_a_switch_is_sent_at_the_end_of_the_cycle(make_millennium_adapter):
    adapter, client = _adapter(make_millennium_adapter)
    assert adapter.update_credential_status(PID, "card", CredentialStatus.SUSPENDED)
    assert client.imports == [], "nothing is sent during the phases"
    assert PacsCards.get(PID, "card")[0]["pending"] == "deactivate"

    adapter.finish_cycle()
    [imported] = client.imports
    assert imported.csv.splitlines()[1] == "33137,Ada,Lovelace,False,5001,66"
    assert PacsCards.get(PID, "card")[0] == {
        "slot": 2, "card_number": "5001", "facility_code": "66", "active": False,
    }


def test_a_cardholder_the_import_skipped_stays_pending(make_millennium_adapter):
    adapter, _ = _adapter(make_millennium_adapter, skipped=[PID])
    adapter.update_credential_status(PID, "card", CredentialStatus.SUSPENDED)
    adapter.finish_cycle()
    assert PacsCards.get(PID, "card")[0]["pending"] == "deactivate", "tried again next cycle"


def test_a_box_already_right_is_not_sent(make_millennium_adapter):
    adapter, client = _adapter(make_millennium_adapter, active=True)
    assert adapter.update_credential_status(PID, "card", CredentialStatus.ACTIVE)
    adapter.finish_cycle()
    assert client.imports == []
    assert PacsCards.get(PID, "card")[0]["active"] is True


def test_an_operator_who_switched_our_card_off_is_not_overruled(make_millennium_adapter):
    adapter, client = _adapter(make_millennium_adapter, active=False)
    PacsCards.record(PID, "card", [
        {"slot": 2, "card_number": "5001", "facility_code": "66", "active": True},
    ])
    assert adapter.update_credential_status(PID, "card", CredentialStatus.ACTIVE) is False
    adapter.finish_cycle()
    assert client.imports == []


def test_dry_run_sends_nothing_and_keeps_it_pending(make_millennium_adapter, monkeypatch):
    from agsync import dryrun

    adapter, client = _adapter(make_millennium_adapter)
    adapter.update_credential_status(PID, "card", CredentialStatus.SUSPENDED)
    monkeypatch.setattr(dryrun, "active", lambda: True)
    adapter.finish_cycle()
    assert client.imports == []
    assert PacsCards.get(PID, "card")[0]["pending"] == "deactivate"
