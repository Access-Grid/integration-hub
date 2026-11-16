"""Where a cardholder's address and phone come from: the export's own columns.

A real address is the holder's and wins. Without one, an address is
synthesized from the name and Employee ID against the operator's domain.
"""

from __future__ import annotations

from agsync.lib.pacs.millennium_ultra import export
from tests._fakes import FakeMillenniumClient, export_row


def _person(make_millennium_adapter, **contact):
    client = FakeMillenniumClient(rows=[export_row("33137", "Auston", "Bunsen", **contact)])
    [person] = make_millennium_adapter(client).list_people()
    return person


def test_the_export_carries_contact_details():
    client = FakeMillenniumClient(
        rows=[export_row("33137", email="a.bunsen@icon.test", phone="+1 305 555 0142")]
    )
    [row] = export.parse(client.export_cardholders())
    assert (row.email, row.phone) == ("a.bunsen@icon.test", "+1 305 555 0142")


def test_a_real_address_and_phone_are_used(make_millennium_adapter):
    person = _person(make_millennium_adapter, email="a.bunsen@icon.test", phone="+1 305 555 0142")
    assert person.email == "a.bunsen@icon.test"
    assert person.phone == "+1 305 555 0142"


def test_no_address_means_a_synthesized_one(make_millennium_adapter):
    person = _person(make_millennium_adapter)
    assert person.email == "auston.bunsen.33137@cards.example.com"
    assert person.phone == ""


def test_junk_in_the_address_is_treated_as_absent(make_millennium_adapter):
    """Free text in the PACS. Issuing to it fails at AccessGrid with an error
    about the address rather than about the field it came from."""
    for junk in ("see reception", "n/a", "bunsen@icon"):
        person = _person(make_millennium_adapter, email=junk)
        assert person.email == "auston.bunsen.33137@cards.example.com", junk
