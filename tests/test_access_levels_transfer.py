"""Access levels follow from the trigger card onto the cards we write.

Every per-slot field on the cardholder form rides along untouched in the
round trip, which is enough for the card that replaces the marker: it
inherits the marker's access levels by occupying its slot. A card written
into an *empty* slot inherits that slot's nothing — so a holder's phone
opened every door their marker card named and their watch opened none.

The blob itself is opaque and is copied verbatim. Re-encoding a structure we
do not own is how a working access level becomes a subtly broken one.
"""

from __future__ import annotations

import re
from html import escape

from agsync.lib.pacs.base import CredentialIdentity
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11587"
ROSTER = [{"ID": PID, "IsActive": True, "Name": "Grid, Accessg"}]

# Captured from a live save on hosted8.mgiaccess.com. The outer key is the
# tenant; the next is the id of the "Access Level N" column the level sits in
# (200 to 209, the same ids the bulk export takes for those columns); then
# the level itself and its own dates. Those keys mean the same thing on every
# card and name neither the slot nor the card, which is what makes copying
# the blob between slots sound. Treated as opaque all the same: the per-level
# AD/ED dates are exactly what re-encoding would quietly mangle.
LEVELS = (
    '{"0":{"200":{"ALID":1,"AD":"2026-09-29T00:00:00+00:00","ED":null},'
    '"201":{"ALID":2,"AD":"2026-09-29T00:00:00+00:00","ED":null}}}'
)
OTHER_LEVELS = '{"0":{"200":{"ALID":7,"AD":null,"ED":null}}}'


def _set_levels(html: str, slot: int, value: str) -> str:
    """Populate one slot's hidden AccessLevels field.

    Escaped as Millennium serves it — the blob is full of double quotes, and
    an attribute carrying them raw would end at the first one.
    """
    pattern = (
        r'(<input[^>]*\bname="Card_' + str(slot) + r'_AccessLevels"[^>]*\bvalue=")'
        r'[^"]*(")'
    )
    escaped = escape(value, quote=True)
    # A lambda replacement, so backslashes in the blob stay literal.
    new, count = re.subn(
        pattern, lambda m: m.group(1) + escaped + m.group(2), html, count=1,
    )
    assert count == 1, f"no AccessLevels input for slot {slot}"
    return new


def _marker_page(millennium_page, set_slot, *, levels=LEVELS, slot=1):
    """A cardholder whose marker card names some access levels."""
    page = millennium_page
    for index in (1, 2, 3):
        if index == slot:
            page = set_slot(
                page, index, card_id="7919", card_number="1", facility_code="99",
                card_format=TRIGGER, active=True,
            )
        else:
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
    return _set_levels(page, slot, levels)


def _adapter(make_millennium_adapter, page):
    client = FakeMillenniumClient(roster=ROSTER, pages={PID: page})
    return make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER
    ), client


def _posted(client):
    assert client.saved, "expected a save"
    return client.saved[-1][1]


def _field(client, name: str) -> str:
    return _posted(client).value(name)


def test_the_card_in_an_empty_slot_gets_the_markers_levels(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The regression. Both halves of a pair, one slot each."""
    adapter, client = _adapter(
        make_millennium_adapter, _marker_page(millennium_page, set_slot)
    )
    assert adapter.write_back_credentials(PID, "seos", [
        CredentialIdentity("66", "1238"), CredentialIdentity("66", "1243"),
    ]) is True

    # Slot 1 is the marker's, slot 2 was empty.
    assert _field(client, "Card_1_EncodedCardNumber") == "1238"
    assert _field(client, "Card_2_EncodedCardNumber") == "1243"
    assert _field(client, "Card_1_AccessLevels") == LEVELS
    assert _field(client, "Card_2_AccessLevels") == LEVELS


def test_a_watch_written_cycles_later_inherits_them_too(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """By then the marker is gone, so the source is the card that replaced it."""
    page = _marker_page(millennium_page, set_slot)
    adapter, client = _adapter(make_millennium_adapter, page)
    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("66", "1238")])

    # Millennium serves the phone's card back in the marker's slot, carrying
    # the levels it inherited. No marker remains.
    after = _set_levels(
        set_slot(
            page, 1, card_id="7919", card_number="1238", facility_code="66",
            card_format=TRIGGER, active=True,
        ),
        1, LEVELS,
    )
    adapter2, client2 = _adapter(make_millennium_adapter, after)
    assert adapter2.write_back_credentials(PID, "seos", [
        CredentialIdentity("66", "1238"), CredentialIdentity("66", "1243"),
    ]) is True

    assert _field(client2, "Card_2_EncodedCardNumber") == "1243"
    assert _field(client2, "Card_2_AccessLevels") == LEVELS


def test_the_blob_is_copied_byte_for_byte(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """It is opaque to us, so it must not be normalised on the way through."""
    # Three levels, captured live, and the cardholder screen renders them
    # in the columns the keys name: 200 holds ALID 2 as "MASTER 1", 201
    # holds ALID 1 as "Common", 202 holds ALID 212 as "SPECIAL ACCESS
    # MASTER". Which column a level occupies is meaning, not noise.
    awkward = (
        '{"0":{"200":{"ALID":2,"AD":null,"ED":null},'
        '"201":{"ALID":1,"AD":null,"ED":null},'
        '"202":{"ALID":212,"AD":null,"ED":null}}}'
    )
    adapter, client = _adapter(
        make_millennium_adapter,
        _marker_page(millennium_page, set_slot, levels=awkward),
    )
    adapter.write_back_credentials(PID, "seos", [
        CredentialIdentity("66", "1238"), CredentialIdentity("66", "1243"),
    ])

    assert _field(client, "Card_2_AccessLevels") == awkward


def test_a_marker_with_no_levels_writes_none(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Nothing to inherit stays nothing."""
    adapter, client = _adapter(
        make_millennium_adapter,
        _marker_page(millennium_page, set_slot, levels="{}"),
    )
    adapter.write_back_credentials(PID, "seos", [
        CredentialIdentity("66", "1238"), CredentialIdentity("66", "1243"),
    ])

    assert _field(client, "Card_1_AccessLevels") == "{}"
    assert _field(client, "Card_2_AccessLevels") == "{}"


def test_an_empty_blob_is_skipped_in_favour_of_a_real_one(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """"{}" is how Millennium spells "none", so it must not end the search.

    Slot 1 is the marker and carries none; slot 2 is a card we wrote earlier
    that does. Stopping at the first trigger-format slot would write the
    marker's nothing over real access.
    """
    page = _marker_page(millennium_page, set_slot, levels="{}")
    page = set_slot(
        page, 2, card_id="7920", card_number="1238", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    page = _set_levels(page, 2, LEVELS)
    adapter, client = _adapter(make_millennium_adapter, page)

    # One identity, which lands in the marker's slot.
    assert adapter.write_back_credentials(
        PID, "seos", [CredentialIdentity("66", "1243")]
    ) is True

    assert _field(client, "Card_1_EncodedCardNumber") == "1243"
    assert _field(client, "Card_1_AccessLevels") == LEVELS


def test_an_unrelated_card_keeps_its_own_levels(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """We only stamp slots we write. A card of the customer's is left alone."""
    page = _marker_page(millennium_page, set_slot, slot=2)
    # Slot 1 holds the cardholder's own badge, in another format.
    page = set_slot(
        page, 1, card_id="700", card_number="900", facility_code="50",
        card_format="1", active=True,
    )
    page = _set_levels(page, 1, OTHER_LEVELS)
    adapter, client = _adapter(make_millennium_adapter, page)

    assert adapter.write_back_credentials(PID, "seos", [
        CredentialIdentity("66", "1238"), CredentialIdentity("66", "1243"),
    ]) is True

    # The marker's slot and the empty one carry the marker's levels; the
    # customer's badge keeps its own.
    assert _field(client, "Card_1_AccessLevels") == OTHER_LEVELS
    assert _field(client, "Card_1_EncodedCardNumber") == "900"
    assert _field(client, "Card_2_AccessLevels") == LEVELS
    assert _field(client, "Card_3_AccessLevels") == LEVELS
