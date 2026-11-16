"""Millennium adapter: people and credentials, read from the bulk export.

The trigger is a card format rather than a sentinel field, compared by the
display name the export prints. The adapter runs in one of two directions —
DESFire copies a card out of Millennium, Seos asks AccessGrid to mint one.
"""

from __future__ import annotations

import logging

import pytest

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import (
    MODE_SEOS,
    PacsCards,
    synthesize_email,
)
from agsync.lib.pacs.millennium_ultra.client import MillenniumError
from tests._fakes import FakeMillenniumClient, export_row

TRIGGER = "HID 37"
OTHER = "Wiegand Card"


def _card(number="1234", card_format=TRIGGER, active=True):
    return {
        "card_number": number, "facility_code": "66",
        "card_format": card_format, "active": active,
    }


def _creds(adapter, person_id="33137"):
    list(adapter.list_people())
    return list(adapter.list_credentials(person_id))


def _adapter(make_millennium_adapter, *rows, **kwargs):
    return make_millennium_adapter(FakeMillenniumClient(rows=rows), **kwargs)


# --- names and identity --------------------------------------------------


def test_email_is_synthesized_from_name_and_employee_id():
    assert (
        synthesize_email("Accessg", "Grid", "33137", "cards.example.com")
        == "accessg.grid.33137@cards.example.com"
    )
    assert synthesize_email("Jean-Luc", "O'Brien", "42", "x.test") == "jeanluc.obrien.42@x.test"


def test_no_domain_means_no_email():
    assert synthesize_email("A", "B", "1", "") == ""


def test_no_employee_id_is_an_error(caplog):
    with caplog.at_level(logging.ERROR):
        assert synthesize_email("Ada", "Lovelace", "", "x.test") == ""
    assert "no Employee ID" in caplog.text


def test_people_are_keyed_by_employee_id(make_millennium_adapter):
    adapter = _adapter(
        make_millennium_adapter, export_row("33137", "Accessg", "Grid", phone="555-0100"),
    )
    [person] = adapter.list_people()
    assert person.id == "33137"
    assert person.full_name == "Accessg Grid"
    assert person.email == "accessg.grid.33137@cards.example.com"
    assert person.phone == "555-0100"
    # Millennium has no cardholder-level enable flag.
    assert person.active is True


def test_a_row_with_no_employee_id_is_skipped(make_millennium_adapter, caplog):
    adapter = _adapter(
        make_millennium_adapter, export_row("", "No", "Id"), export_row("7", "Has", "Id"),
    )
    with caplog.at_level(logging.ERROR):
        assert [p.id for p in adapter.list_people()] == ["7"]
    # Once where the address could not be made, once for the skip.
    assert "cannot synthesize an email for No Id" in caplog.text
    assert "skipping No Id" in caplog.text


def test_a_shared_employee_id_skips_every_row_holding_it(make_millennium_adapter, caplog):
    adapter = _adapter(
        make_millennium_adapter,
        export_row("5", "Ann", "One"), export_row("5", "Bob", "Two"), export_row("6"),
    )
    with caplog.at_level(logging.ERROR):
        assert [p.id for p in adapter.list_people()] == ["6"]
    assert "Employee ID 5" in caplog.text


# --- refusing to read nonsense -------------------------------------------


def test_no_stored_trigger_name_fails_the_cycle(make_millennium_adapter):
    """Otherwise no card reads as a trigger, and every pass looks withdrawn."""
    adapter = _adapter(make_millennium_adapter, export_row("1"), trigger_card_format_label="")
    with pytest.raises(MillenniumError, match="no stored name"):
        list(adapter.list_people())


def test_an_empty_export_fails_the_cycle(make_millennium_adapter):
    adapter = _adapter(make_millennium_adapter)
    with pytest.raises(MillenniumError, match="empty"):
        list(adapter.list_people())


# --- the trigger ---------------------------------------------------------


def test_slot_with_the_trigger_format_enrolls(make_millennium_adapter):
    adapter = _adapter(make_millennium_adapter, export_row("33137", slots=[_card()]))
    [cred] = _creds(adapter)
    assert cred.id == "card"
    assert cred.card_number == "1234"
    assert cred.site_code == "66"
    assert cred.trigger_active is True
    assert cred.status is CredentialStatus.ACTIVE
    # DESFire copies an existing card out; nothing is allocated.
    assert cred.allocate_identity is False


def test_slot_with_another_format_does_not_enroll(make_millennium_adapter):
    adapter = _adapter(
        make_millennium_adapter, export_row("33137", slots=[_card(card_format=OTHER)]),
    )
    assert _creds(adapter) == []


def test_trigger_matching_is_by_exact_name(make_millennium_adapter):
    adapter = _adapter(
        make_millennium_adapter, export_row("33137", slots=[_card()]),
        trigger_card_format_label="HID 3",
    )
    assert _creds(adapter) == []


def test_empty_slot_never_triggers(make_millennium_adapter):
    adapter = _adapter(
        make_millennium_adapter, export_row("33137", slots=[_card(number="")]),
    )
    assert _creds(adapter) == []


def test_inactive_card_is_suspended(make_millennium_adapter):
    adapter = _adapter(
        make_millennium_adapter, export_row("33137", slots=[_card(active=False)]),
    )
    [cred] = _creds(adapter)
    assert cred.status is CredentialStatus.SUSPENDED


def test_card_dates_come_from_the_export(make_millennium_adapter):
    card = {**_card(), "activation": "9/11/2026 12:00:00 AM", "expiration": "9/11/2027 6:17:00 PM"}
    adapter = _adapter(make_millennium_adapter, export_row("33137", slots=[card]))
    [cred] = _creds(adapter)
    assert cred.activate_date.isoformat() == "2026-09-11T00:00:00+00:00"
    assert cred.deactivate_date.isoformat() == "2027-09-11T18:17:00+00:00"


# --- Seos: AccessGrid mints ----------------------------------------------


def test_seos_credential_asks_accessgrid_to_allocate(make_millennium_adapter):
    adapter = _adapter(
        make_millennium_adapter, export_row("33137", slots=[_card()]), mode=MODE_SEOS,
    )
    [cred] = _creds(adapter)
    assert cred.id == "card"
    assert cred.allocate_identity is True
    # Nothing of ours may reach AccessGrid, or it would not allocate.
    assert cred.card_number == ""
    assert cred.site_code == ""


def test_a_pair_needs_a_slot_besides_the_marker(make_millennium_adapter):
    """Apple's card overwrites the marker; Google's needs an empty slot."""
    full = [_card(), _card("902", OTHER), _card("903", OTHER)]
    row = export_row("33137", slots=full)
    assert _creds(_adapter(make_millennium_adapter, row, mode=MODE_SEOS, pair=True)) == []
    assert len(_creds(_adapter(make_millennium_adapter, row, mode=MODE_SEOS))) == 1


def test_one_free_slot_beside_the_marker_is_enough(make_millennium_adapter):
    adapter = _adapter(
        make_millennium_adapter,
        export_row("33137", slots=[_card(), _card("902", OTHER)]), mode=MODE_SEOS,
    )
    assert len(_creds(adapter)) == 1


# --- setup-time discovery ------------------------------------------------


def test_card_formats_are_read_for_the_setup_screen(make_millennium_adapter):
    assert _adapter(make_millennium_adapter).card_formats() == [
        ("1", "Wiegand Card"), ("7", "HID 37"),
    ]


def test_no_cardholders_means_no_formats(make_millennium_adapter):
    client = FakeMillenniumClient()
    client.first_cardholder_id = lambda: ""
    assert make_millennium_adapter(client).card_formats() == []


# --- the ledger ----------------------------------------------------------


def test_ledger_keeps_one_entry_per_physical_card():
    # A rewritten card must not accumulate a second ledger row.
    PacsCards.record(
        "33137", "seos",
        [
            {"slot": 2, "card_number": "5001", "facility_code": "66"},
            {"slot": 3, "card_number": "5001", "facility_code": "66"},
        ],
    )
    assert PacsCards.get("33137", "seos") == [
        {"slot": 3, "card_number": "5001", "facility_code": "66"},
    ]


def test_the_ledger_stores_card_identity_encrypted():
    from agsync.db.connection import execute

    PacsCards.record("33137", "seos", [{"slot": 2, "card_number": "5001", "facility_code": "66"}])
    [row] = execute("SELECT card_number, facility_code FROM pacs_cards")
    assert "5001" not in row["card_number"] and "66" != row["facility_code"]
    assert PacsCards.get("33137", "seos")[0]["card_number"] == "5001"


def test_every_change_to_the_ledger_is_logged(caplog, monkeypatch):
    """So the Logs tab shows what the sync is tracking, card by card, with
    whose card it is and which slot it is in."""
    import logging

    from agsync.lib.pacs.millennium_ultra import pacs_cards

    monkeypatch.setattr(pacs_cards, "NAMES", {"33137": "Ada Lovelace"})
    card = {"slot": 1, "card_number": "5001", "facility_code": "66", "pending": "write"}
    with caplog.at_level(logging.INFO):
        PacsCards.record("33137", "card", [card])
        PacsCards.record("33137", "card", [{**card, "slot": 3, "pending": None}])
        PacsCards.forget("33137", "card")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Millennium cards:")]
    assert lines == [
        "Millennium cards: 33137 (Ada Lovelace) card — now tracking card 66/5001 "
        "(slot 1, waiting for AccessGrid's card to replace it)",
        "Millennium cards: 33137 (Ada Lovelace) card — card 66/5001 in slot 1: "
        "moved to slot 3; done",
        "Millennium cards: 33137 (Ada Lovelace) card — no longer tracking card 66/5001 "
        "(was slot 3)",
    ]
