"""Millennium Ultra parsing: card slots, formats, dates, emails, form replay.

All of these run against the saved fixture — no network, no session.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agsync.lib.pacs.millennium_ultra.parse import (
    assign_emails,
    parse_card_formats,
    parse_cardholder,
    parse_datetime,
    parse_form,
    serialize_form,
    split_roster_name,
    synth_email,
)

FIXTURE = Path(__file__).parent / "fixtures" / "millennium_ultra_cardholder.html"


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def holder(html):
    return parse_cardholder(html)


# --- identification ------------------------------------------------------


def test_person_fields(holder):
    assert holder.id == "12345"
    assert holder.first_name == "Rosalind"
    assert holder.last_name == "Marchetti"
    assert holder.full_name == "Rosalind Marchetti"
    assert holder.employee_id == "30373420026"


def test_email_and_phone_are_blank_on_this_tenant(holder):
    # The Personal Information module is off, so both render empty and
    # disabled. This is what forces synthesized addresses.
    assert holder.email == ""
    assert holder.phone == ""


# --- card slots ----------------------------------------------------------


def test_populated_slot_one(holder):
    slot = holder.cards[0]
    assert slot.index == 1
    assert slot.card_id == "6630"
    assert slot.encoded == "30373420026"
    assert slot.facility == "0"
    assert slot.card_format == "7"
    assert slot.active is True
    assert slot.is_empty is False


def test_slot_two_inactive_checkbox(holder):
    slot = holder.cards[1]
    assert slot.encoded == "884422"
    assert slot.facility == "12"
    assert slot.card_format == "1"
    # Renders without `checked`, so the card is disabled.
    assert slot.active is False


def test_empty_slot_detected(holder):
    slot = holder.cards[2]
    # Empty slots render their inputs with no value= attribute at all.
    assert slot.encoded == ""
    assert slot.card_id == ""
    assert slot.card_format == ""
    assert slot.is_empty is True


def test_identity_is_facility_and_number_not_slot(holder):
    # Slot index and CardID are both reusable; the physical card is not.
    assert holder.cards[0].identity == "0:30373420026"
    assert holder.cards[1].identity == "12:884422"


def test_identity_survives_a_slot_move(html):
    """The same physical card in a different slot keeps its identity."""
    moved = html.replace("Card_1_EncodedCardNumber", "TMP").replace(
        "Card_2_EncodedCardNumber", "Card_1_EncodedCardNumber"
    ).replace("TMP", "Card_2_EncodedCardNumber")
    moved = moved.replace("Card_1_FaciltyCode", "TMP2").replace(
        "Card_2_FaciltyCode", "Card_1_FaciltyCode"
    ).replace("TMP2", "Card_2_FaciltyCode")
    holder = parse_cardholder(moved)
    identities = {c.identity for c in holder.cards if not c.is_empty}
    assert "0:30373420026" in identities
    assert "12:884422" in identities


# --- card formats --------------------------------------------------------


def test_card_formats_discovered(html):
    formats = dict(parse_card_formats(html))
    assert formats["7"] == "HID 37"
    assert formats["1"] == "Wiegand Card"
    assert formats["2"] == "Ultra - Wiegand 26 No FC"
    assert len(formats) == 7


# --- dates ---------------------------------------------------------------


def test_parse_standard_timestamp():
    assert parse_datetime("02/18/2025 01:00 AM") == datetime(2025, 2, 18, 1, 0)


def test_parse_malformed_midnight():
    # The app emits "00:00 AM", which %I rejects. Dropping it would look like
    # "no expiry" and silently extend a credential.
    assert parse_datetime("08/17/2028 00:00 AM") == datetime(2028, 8, 17, 0, 0)


def test_parse_date_only_and_blank():
    assert parse_datetime("02/18/2025") == datetime(2025, 2, 18)
    assert parse_datetime("") is None
    assert parse_datetime(None) is None
    assert parse_datetime("not a date") is None


def test_slot_dates_parsed(holder):
    assert holder.cards[0].activation == datetime(2025, 2, 18, 1, 0)
    assert holder.cards[0].expiration == datetime(2026, 2, 18, 1, 0)
    assert holder.cards[1].expiration == datetime(2028, 8, 17, 0, 0)
    assert holder.cards[2].expiration is None


# --- roster names --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Abayan, HID. Tanyabella", ("Abayan", "HID", "Tanyabella")),
        ("Alejandro Barrionuevo, HID. Luis", ("Alejandro Barrionuevo", "HID", "Luis")),
        ("Acebedo, RFID. Jose", ("Acebedo", "RFID", "Jose")),
    ],
)
def test_split_roster_name(raw, expected):
    assert split_roster_name(raw) == expected


# --- synthesized emails --------------------------------------------------


def test_synth_email_basic():
    assert synth_email("Rosalind", "Marchetti", "iconcreds.com") == "rosalindmarchetti@iconcreds.com"


def test_synth_email_strips_punctuation_and_case():
    assert synth_email("Juan Pablo", "O'Neill-Smith", "x.com") == "juanpablooneillsmith@x.com"


def test_synth_email_needs_a_domain():
    assert synth_email("A", "B", "") == ""


def test_collisions_get_the_cardholder_id():
    # The real roster has four separate people named "Acebedo, RFID. Jose".
    people = [
        ("9515", "Jose", "Acebedo"),
        ("10411", "Jose", "Acebedo"),
        ("10412", "Jose", "Acebedo"),
        ("8846", "Karelia", "Aguilera"),
    ]
    emails = assign_emails(people, "iconcreds.com")
    assert len({emails[p[0]] for p in people}) == 4  # all distinct
    assert emails["8846"] == "kareliaaguilera@iconcreds.com"  # unique name stays clean
    assert emails["9515"] == "joseacebedo.9515@iconcreds.com"


# --- generic form replay -------------------------------------------------


def test_serialize_reproduces_every_named_field(html):
    fields = dict(serialize_form(parse_form(html)))
    # Fields the adapter never reads still have to survive a re-post.
    assert fields["UserField1"] == "2810"
    assert fields["UserField2"] == "Tenant"
    assert fields["MiddleName"] == "HID"
    assert fields["TenantsAsJson"] == "[0]"
    assert fields["Card_1_AccessLevels"] == "{}"
    assert fields["__RequestVerificationToken"] == "TOKEN-FROM-THE-PAGE"


def test_serialize_omits_unchecked_boxes_and_buttons(html):
    fields = dict(serialize_form(parse_form(html)))
    # Card 1 is checked, cards 2 and 3 are not — browser semantics.
    assert fields["Card_1_Active"] == "true"
    assert "Card_2_Active" not in fields
    assert "Card_3_Active" not in fields
    assert "saveButton" not in fields


def test_serialize_includes_disabled_fields(html):
    # Several inputs render disabled but still appear in the payload a real
    # browser sends, because the page's JS re-enables them before submit.
    fields = dict(serialize_form(parse_form(html)))
    assert "EMail" in fields
    assert "Address1" in fields


def test_serialize_carries_selected_option(html):
    fields = dict(serialize_form(parse_form(html)))
    assert fields["Card_1_CardFormat"] == "7"
    assert fields["Card_2_CardFormat"] == "1"
    assert fields["Card_3_CardFormat"] == ""  # nothing selected on an empty slot


def test_override_can_check_and_uncheck(html):
    form = parse_form(html)
    # Suspend card 1: removing the field is how a browser "unchecks" it.
    off = dict(serialize_form(form, {"Card_1_Active": None}))
    assert "Card_1_Active" not in off
    assert off["Card_1_EncodedCardNumber"] == "30373420026"  # everything else intact

    # Reactivate card 2: the box renders unchecked, so the field must be added.
    on = dict(serialize_form(form, {"Card_2_Active": "true"}))
    assert on["Card_2_Active"] == "true"
    assert on["Card_1_Active"] == "true"


def test_override_does_not_duplicate_a_field(html):
    pairs = serialize_form(parse_form(html), {"Card_1_EncodedCardNumber": "999"})
    names = [n for n, _ in pairs]
    assert names.count("Card_1_EncodedCardNumber") == 1
    assert dict(pairs)["Card_1_EncodedCardNumber"] == "999"
