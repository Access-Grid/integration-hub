"""Reading Millennium's bulk cardholder export, the adapter's only source."""

from __future__ import annotations

import io
import pathlib
import zipfile

import pytest

from agsync.lib.pacs.millennium_ultra import export
from tests._fakes import FakeMillenniumClient

TRIGGER_LABEL = "AccessGrid / HID Wallet Format"
FIXTURE = pathlib.Path(__file__).parent / "millennium_fixtures" / "cardholder_export.csv"


@pytest.fixture
def csv_text():
    return FIXTURE.read_text()


# --- reading the file ----------------------------------------------------


def test_the_archive_is_one_csv(csv_text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Cardholders.csv", csv_text)
    assert export.unpack(buf.getvalue()).splitlines()[0].startswith("First Name")


def test_an_archive_without_a_csv_is_an_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "nothing useful")
    with pytest.raises(ValueError, match="No CSV"):
        export.unpack(buf.getvalue())


def test_slots_are_folded_up_per_cardholder(csv_text):
    people = export.parse(csv_text)
    auston = next(p for p in people if p.last_name == "Bunsen")
    assert [(s.index, s.facility_code, s.card_number, s.active) for s in auston.slots] == [
        (1, "1", "1", False),
        (2, "2", "1255", True),
        (3, "2", "1256", False),
    ]


def test_an_empty_slot_is_not_a_card(csv_text):
    """Every cardholder gets three slots; the ones holding no card are empty."""
    people = export.parse(csv_text)
    ordinary = next(p for p in people if p.last_name == "Sandoval")
    assert [s.empty for s in ordinary.slots] == [False, True, True]


# --- the adapter reading it ----------------------------------------------


def _adapter(make_millennium_adapter, csv_text):
    client = FakeMillenniumClient()
    client.export_cardholders = lambda: csv_text
    return make_millennium_adapter(client, trigger_card_format_label=TRIGGER_LABEL)


def test_people_are_keyed_by_employee_id(make_millennium_adapter, csv_text):
    adapter = _adapter(make_millennium_adapter, csv_text)
    assert [p.id for p in adapter.list_people()] == ["33137", "1", "599", "200", "680"]


def test_only_trigger_cards_become_credentials(make_millennium_adapter, csv_text):
    adapter = _adapter(make_millennium_adapter, csv_text)
    list(adapter.list_people())
    assert [c.card_number for c in adapter.list_credentials("33137")] == ["1", "1255", "1256"]
    assert list(adapter.list_credentials("599")) == []  # a Wiegand card only


def test_a_cardholder_missing_from_the_export_is_unread_not_empty(
    make_millennium_adapter, csv_text
):
    """Answering [] would let phase 3 read them as deleted."""
    from agsync.lib.pacs import PacsRecordUnavailable

    adapter = _adapter(make_millennium_adapter, csv_text)
    list(adapter.list_people())
    with pytest.raises(PacsRecordUnavailable, match="not in this cycle's export"):
        list(adapter.list_credentials("11591"))
