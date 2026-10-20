"""The one-time move from Millennium record ids to Employee IDs.

Run against the suite's temporary database rather than a stand-in, because
what matters is that the tracking table and the ledger move together, in one
transaction, or not at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.millennium_ultra import migrate
from agsync.lib.pacs.millennium_ultra.adapter import PacsCards
from agsync.lib.pacs.millennium_ultra.client import MillenniumAuthError, MillenniumError
from agsync.settings_store import get_json
from agsync.sync import tracking
from tests._fakes import FakeMillenniumClient, export_row


GONE = object()


def _adapter(make_millennium_adapter, employee_ids, **kwargs):
    """`employee_ids` maps record id -> the EmployeeID its page holds, or an
    exception to raise when it is read."""
    client = FakeMillenniumClient(rows=[export_row("33137")])
    client.pages_read = []

    def get_cardholder_form(record_id):
        client.pages_read.append(record_id)
        value = employee_ids[record_id]
        if isinstance(value, Exception):
            raise value
        # A deleted record's page is an empty form: no ID of its own.
        page_id = "" if value is GONE else record_id
        return SimpleNamespace(
            value=lambda name: {"EmployeeID": "" if value is GONE else value, "ID": page_id}.get(name, "")
        )

    client.get_cardholder_form = get_cardholder_form
    return make_millennium_adapter(client, migrated=False, **kwargs), client


def _track(record_id, credential_id="seos"):
    tracking.upsert(
        pacs_person_id=record_id, pacs_credential_id=credential_id,
        ag_card_id=f"card-{record_id}", full_name="x", employee_id=record_id,
        status="active",
    )


def test_tracking_and_ledger_move_to_employee_ids(make_millennium_adapter):
    _track("11591")
    PacsCards.record("11591", "seos", [{"slot": 2, "card_number": "5001", "facility_code": "66"}])
    adapter, _ = _adapter(make_millennium_adapter, {"11591": "33137"})

    list(adapter.list_people())

    assert tracking.get("11591", "seos") is None
    assert tracking.get("33137", "seos").employee_id == "33137"
    assert set(PacsCards.all()) == {"33137:seos"}
    assert get_json(migrate.DONE_KEY) is True


def test_each_move_is_logged(make_millennium_adapter, caplog):
    import logging

    _track("11591")
    adapter, _ = _adapter(make_millennium_adapter, {"11591": "33137"})
    with caplog.at_level(logging.INFO):
        list(adapter.list_people())
    assert "cardholder record 11591 is now Employee ID 33137" in caplog.text


def test_it_runs_once(make_millennium_adapter):
    _track("11591")
    adapter, client = _adapter(make_millennium_adapter, {"11591": "33137"})
    list(adapter.list_people())
    list(adapter.list_people())
    assert client.pages_read == ["11591"]


def test_a_record_id_equal_to_another_employee_id_is_not_overwritten(make_millennium_adapter):
    """Record 1 becomes 5 while record 5 becomes 9: moved naively, record 1's
    row would land on 5 and then move again to 9."""
    _track("1")
    _track("5")
    adapter, _ = _adapter(make_millennium_adapter, {"1": "5", "5": "9"})
    list(adapter.list_people())
    assert tracking.get("5", "seos").ag_card_id == "card-1"
    assert tracking.get("9", "seos").ag_card_id == "card-5"


@pytest.mark.parametrize("pages, why", [
    ({"11591": "", "11592": "2"}, "cardholder 11591 has no Employee ID"),
    ({"11591": "7", "11592": "7"}, "share Employee ID 7"),
    ({"11591": RuntimeError("404"), "11592": "2"}, "cannot read cardholder 11591"),
])
def test_a_record_that_cannot_move_stops_everything(make_millennium_adapter, caplog, pages, why):
    _track("11591")
    _track("11592")
    adapter, _ = _adapter(make_millennium_adapter, pages)

    with pytest.raises(MillenniumError, match="cannot move to Employee IDs"):
        list(adapter.list_people())

    assert why in caplog.text
    # Nothing moved, and it will try again next cycle.
    assert tracking.get("11591", "seos") and tracking.get("11592", "seos")
    assert not get_json(migrate.DONE_KEY)


def test_a_deleted_record_does_not_stop_the_others(make_millennium_adapter, caplog):
    """Seen on-site: a tracked record deleted from Millennium, whose page is
    an empty form. It is set aside, under a key no export row matches, so
    phase 3 revokes its pass; everyone else moves."""
    _track("11591")
    _track("11652")
    adapter, _ = _adapter(make_millennium_adapter, {"11591": "33137", "11652": GONE})

    list(adapter.list_people())

    assert tracking.get("33137", "seos").ag_card_id == "card-11591"
    assert tracking.get("gone:11652", "seos").ag_card_id == "card-11652"
    assert "cardholder record 11652 no longer exists" in caplog.text
    assert get_json(migrate.DONE_KEY) is True


def test_an_expired_session_is_not_a_migration_failure(make_millennium_adapter):
    _track("11591")
    adapter, _ = _adapter(make_millennium_adapter, {"11591": MillenniumAuthError("expired")})
    with pytest.raises(MillenniumAuthError):
        list(adapter.list_people())


def test_a_fresh_install_has_nothing_to_move(make_millennium_adapter):
    adapter, client = _adapter(make_millennium_adapter, {})
    assert [p.id for p in adapter.list_people()] == ["33137"]
    assert client.pages_read == []
    assert get_json(migrate.DONE_KEY) is True


def test_a_missing_trigger_name_is_filled_in(make_millennium_adapter, monkeypatch):
    stored = {}
    monkeypatch.setattr(
        "agsync.settings_store.PacsConfig.update_params", lambda **kw: stored.update(kw),
    )
    adapter, _ = _adapter(make_millennium_adapter, {}, trigger_card_format_label="")
    list(adapter.list_people())
    assert stored == {"trigger_card_format_label": "HID 37"}
