"""When a trigger card gets a pass: the issuance table, row by row.

Each test names the rows of the table it stands for. A trigger card is any
non-empty slot in the AccessGrid card format.
"""

from __future__ import annotations

import logging

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from agsync.sync import tracking
from tests._fakes import FakeMillenniumClient, export_row

TRIGGER = "HID 37"
PID = "33137"


def _card(number, facility="66", active=True, card_format=TRIGGER):
    return {
        "card_number": number, "facility_code": facility,
        "card_format": card_format, "active": active,
    }


def _creds(make_millennium_adapter, *slots, pid=PID) -> dict:
    client = FakeMillenniumClient(rows=[export_row(pid, slots=slots)])
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    list(adapter.list_people())
    return {c.id: c for c in adapter.list_credentials(pid)}


def _record(slot, number, pid=PID, credential_id="seos"):
    SeosLedger.record(pid, credential_id, [
        {"slot": slot, "card_number": number, "facility_code": "66"},
    ])


def test_rows_1_2_a_recorded_card_in_its_slot_is_the_pass_it_was(make_millennium_adapter):
    _record(1, "5001")
    assert set(_creds(make_millennium_adapter, _card("5001"))) == {"seos"}
    assert len(SeosLedger.get(PID, "seos")) == 1


def test_rows_3_4_a_different_card_in_a_recorded_slot_is_an_error(
    make_millennium_adapter, caplog
):
    _record(1, "5001")
    with caplog.at_level(logging.ERROR):
        creds = _creds(make_millennium_adapter, _card("9999"))
    assert "slot 1 holds card 66/9999, but we recorded 66/5001" in caplog.text
    assert set(creds) == {"seos"}, "the recorded pass is reported, nothing new"
    # Held, not suspended: left alone until somebody looks at the error.
    assert creds["seos"].hold is True


def test_rows_5_6_a_recorded_card_that_moved_slot_is_followed(make_millennium_adapter):
    _record(1, "5001")
    assert set(_creds(make_millennium_adapter, None, _card("5001"))) == {"seos"}
    assert SeosLedger.get(PID, "seos")[0]["slot"] == 2


def test_rows_7_8_a_new_trigger_card_beside_a_recorded_one_is_a_new_pass(
    make_millennium_adapter,
):
    _record(1, "5001")
    creds = _creds(make_millennium_adapter, _card("5001"), _card("1"))
    assert set(creds) == {"seos", "seos-2"}
    assert creds["seos-2"].allocate_identity is True


def test_rows_9_11_a_card_recorded_on_somebody_else_is_an_error(
    make_millennium_adapter, caplog
):
    _record(1, "5001", pid="777")
    with caplog.at_level(logging.ERROR):
        assert _creds(make_millennium_adapter, _card("5001")) == {}
    assert "does not match what we recorded" in caplog.text


def test_rows_9_11_a_tracked_pass_with_no_card_recorded_is_an_error(
    make_millennium_adapter, caplog
):
    tracking.upsert(
        pacs_person_id=PID, pacs_credential_id="seos", ag_card_id="card-1",
        full_name="x", employee_id=PID, status="active",
    )
    with caplog.at_level(logging.ERROR):
        assert _creds(make_millennium_adapter, _card("1")) == {}
    assert "does not match what we recorded" in caplog.text


def test_row_12_a_new_cardholder_with_a_new_trigger_card_gets_a_pass(make_millennium_adapter):
    """Recorded at once, so the next cycle sees a known card, not a new one."""
    [cred] = _creds(make_millennium_adapter, _card("1", active=False)).values()
    assert cred.id == "seos"
    # The marker's own unticked box is a placeholder, not the pass's state.
    assert cred.status is CredentialStatus.ACTIVE
    assert SeosLedger.get(PID, "seos") == [
        {"slot": 1, "card_number": "1", "facility_code": "66", "pending": "write"},
    ]
    assert set(_creds(make_millennium_adapter, _card("1", active=False))) == {"seos"}


def test_a_card_moved_out_of_the_trigger_format_is_dormant_and_held(make_millennium_adapter):
    _record(1, "5001")
    [cred] = _creds(make_millennium_adapter, _card("5001", card_format="Wiegand Card")).values()
    assert cred.hold is True
    assert SeosLedger.get(PID, "seos")[0]["status"] == "dormant"


def test_a_dormant_card_moved_back_is_tracked_again(make_millennium_adapter):
    _record(1, "5001")
    _creds(make_millennium_adapter, _card("5001", card_format="Wiegand Card"))
    [cred] = _creds(make_millennium_adapter, _card("5001")).values()
    assert cred.hold is False
    assert "status" not in SeosLedger.get(PID, "seos")[0]
    assert len(SeosLedger.all()) == 1, "and no second pass was issued for it"
