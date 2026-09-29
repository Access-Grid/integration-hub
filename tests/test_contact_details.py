"""Where a cardholder's address and phone come from, and when.

The detail page carries both, and used to be the only source. But it is
read inside list_credentials, which runs *after* list_people has already
built the Person — so on the cycle a cardholder is first seen, which is the
same cycle phase 1 provisions them on, both were empty. Every pass went out
to a synthesized address with no phone number, and the real details only
appeared from the second cycle onward, by which time AccessGrid will not
change an issued pass's address.

The bulk export carries them too, and it runs before list_people. So it
covers exactly the cycle the detail page cannot.
"""

from __future__ import annotations

import csv
import io
import pathlib

from agsync.lib.pacs.millennium_ultra import export
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS
from tests._fakes import FakeMillenniumClient

FIXTURE = pathlib.Path(__file__).parent / "millennium_fixtures" / "cardholder_export.csv"
TRIGGER_LABEL = "AccessGrid / HID Wallet Format"


def _csv_with_contact(email="", phone=""):
    """The real export, with contact details filled in for Auston Bunsen."""
    rows = list(csv.DictReader(io.StringIO(FIXTURE.read_text())))
    for row in rows:
        if row["Last Name"] == "Bunsen":
            row["E-Mail"], row["Phone"] = email, phone
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


class ExportingClient(FakeMillenniumClient):
    def __init__(self, csv_text, roster, pages):
        super().__init__(roster=roster, pages=pages, formats=[("8", TRIGGER_LABEL)])
        self._csv = csv_text

    def export_cardholders(self):
        return self._csv


def _roster():
    return [{"ID": 11591, "IsActive": True, "Name": "Bunsen, Auston"}]


def _adapter(make_millennium_adapter, millennium_page, csv_text):
    client = ExportingClient(csv_text, _roster(), {"11591": millennium_page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    adapter.trigger_card_format = "8"
    return adapter


def _first_cycle_person(adapter):
    """What phase 1 is handed, on the cycle a cardholder is first seen."""
    return {p.id: p for p in adapter.list_people()}["11591"]


# --- parsing -------------------------------------------------------------


def test_the_export_carries_contact_details():
    rows = export.parse(_csv_with_contact("a.bunsen@icon.test", "+1 305 555 0142"))
    auston = next(r for r in rows if r.last_name == "Bunsen")
    assert (auston.email, auston.phone) == ("a.bunsen@icon.test", "+1 305 555 0142")


def test_missing_contact_details_parse_as_empty():
    rows = export.parse(_csv_with_contact())
    assert next(r for r in rows if r.last_name == "Bunsen").email == ""


# --- the cycle that matters ----------------------------------------------


def test_the_first_cycle_uses_the_exported_address(
    make_millennium_adapter, millennium_page, seos_ledger
):
    """The regression. Before this, phase 1 saw only the synthesized one."""
    adapter = _adapter(
        make_millennium_adapter, millennium_page,
        _csv_with_contact("a.bunsen@icon.test", "+1 305 555 0142"),
    )
    person = _first_cycle_person(adapter)
    assert person.email == "a.bunsen@icon.test"
    assert person.phone == "+1 305 555 0142"


def test_an_install_holding_nothing_still_gets_a_synthesized_address(
    make_millennium_adapter, millennium_page, seos_ledger
):
    """Which is the live install: 0 of 1,599 cardholders hold either."""
    adapter = _adapter(make_millennium_adapter, millennium_page, _csv_with_contact())
    person = _first_cycle_person(adapter)
    assert person.email.endswith("@cards.example.com")
    assert person.phone == ""


def test_junk_in_the_exported_address_is_treated_as_absent(
    make_millennium_adapter, millennium_page, seos_ledger
):
    """Free text in the PACS, same as on the detail page. Issuing to it
    fails at AccessGrid with an error about the address rather than about
    the field it came from."""
    for junk in ("see reception", "n/a", "bunsen@icon"):
        adapter = _adapter(
            make_millennium_adapter, millennium_page, _csv_with_contact(junk),
        )
        assert _first_cycle_person(adapter).email.endswith("@cards.example.com"), junk


def test_the_detail_page_wins_once_it_has_been_read(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Same precedence the name already uses: the export covers the cycle
    before the page is read, not instead of it."""
    import re

    page = re.sub(
        r'(name="EMail"[^>]*?value=")[^"]*(")',
        r"\g<1>from.the.page@icon.test\g<2>",
        millennium_page,
    )
    adapter = _adapter(
        make_millennium_adapter, page, _csv_with_contact("from.the.export@icon.test"),
    )
    assert _first_cycle_person(adapter).email == "from.the.export@icon.test"

    adapter._profile_for("11591")  # reads the detail page
    assert _first_cycle_person(adapter).email == "from.the.page@icon.test"


def test_a_cardholder_the_export_did_not_name_is_unaffected(
    make_millennium_adapter, millennium_page, seos_ledger
):
    adapter = _adapter(
        make_millennium_adapter, millennium_page, _csv_with_contact("a@icon.test"),
    )
    adapter._client._roster = [
        {"ID": 4242, "IsActive": True, "Name": "Nobody, Someone"},
    ]
    person = {p.id: p for p in adapter.list_people()}["4242"]
    assert person.email.endswith("@cards.example.com")
    assert person.phone == ""
