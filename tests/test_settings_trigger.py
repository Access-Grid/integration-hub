"""Changing the enrollment trigger after setup.

The wizard asks once, but the trigger is the single switch deciding who gets
provisioned, so it has to be changeable without re-running setup. Formats are
re-read from the PACS each time rather than remembered, since the install can
add or rename one.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.millennium_ultra.adapter import MillenniumUltraAdapter
from tests._fakes import FakeMillenniumClient

ROSTER = [{"ID": 11587, "IsActive": False, "Name": "Grid, Accessg"}]


def _adapter(millennium_page, formats=None) -> MillenniumUltraAdapter:
    client = FakeMillenniumClient(
        roster=ROSTER, pages={"11587": millennium_page}, formats=formats,
    )
    adapter = MillenniumUltraAdapter(
        base_url="https://millennium.test", auth_cookie="x", trigger_card_format="7",
    )
    adapter._client.close()
    adapter._client = client
    return adapter


def test_formats_come_from_a_single_cardholder_page(millennium_page):
    # Reading formats must not pay for a full A-Z roster sweep just to find
    # somebody to look at.
    adapter = _adapter(millennium_page, formats=[("1", "Wiegand Card"), ("7", "HID 37")])
    assert adapter.card_formats() == [("1", "Wiegand Card"), ("7", "HID 37")]


def test_no_cardholders_means_no_formats(millennium_page):
    client = FakeMillenniumClient(roster=[], pages={})
    adapter = MillenniumUltraAdapter(base_url="https://m.test", auth_cookie="x")
    adapter._client.close()
    adapter._client = client
    assert adapter.card_formats() == []


def _only_slot_1(page, set_slot, card_format):
    """One card in slot 1, the other two slots emptied.

    The captured page ships with a Wiegand card in slot 2, which would
    otherwise match a Wiegand trigger and hide what these tests are about.
    """
    page = set_slot(
        page, 1, card_id="7919", card_number="1234",
        facility_code="66", card_format=card_format, active=True,
    )
    for slot in (2, 3):
        page = set_slot(page, slot, card_id="", card_number="", card_format=None)
    return page


def test_changing_the_trigger_changes_who_is_enrolled(millennium_page, set_slot):
    # The point of the setting: the same cardholder enrolls or not depending
    # on which format is the trigger.
    page = _only_slot_1(millennium_page, set_slot, "1")

    def creds_with(trigger):
        adapter = _adapter(page)
        adapter.trigger_card_format = trigger
        list(adapter.list_people())
        return list(adapter.list_credentials("11587"))

    assert len(creds_with("1")) == 1
    assert creds_with("7") == []


def test_an_empty_trigger_enrolls_nobody(millennium_page, set_slot):
    # Guards the window between setup steps: no trigger must never mean
    # "every format matches".
    page = _only_slot_1(millennium_page, set_slot, "1")
    adapter = _adapter(page)
    adapter.trigger_card_format = ""
    list(adapter.list_people())
    assert list(adapter.list_credentials("11587")) == []


@pytest.mark.parametrize("trigger", ["1", "7"])
def test_trigger_matching_is_exact(millennium_page, set_slot, trigger):
    page = _only_slot_1(millennium_page, set_slot, trigger)
    adapter = _adapter(page)
    adapter.trigger_card_format = trigger
    list(adapter.list_people())
    assert len(list(adapter.list_credentials("11587"))) == 1
