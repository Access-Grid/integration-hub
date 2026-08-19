"""Millennium adapter: trigger gating, both directions, and write safety.

The trigger is a card format rather than a sentinel field, and the adapter
runs in one of two opposite directions — DESFire copies a card out of
Millennium, Seos writes an AccessGrid-minted card in. Both are exercised
here against the real captured cardholder page.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agsync.lib.pacs.base import CredentialIdentity, CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import (
    MODE_SEOS,
    SeosLedger,
    parse_roster_name,
    synthesize_email,
)
from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"  # "HID 37" in the captured page's format list
OTHER = "1"    # "Wiegand Card"

ROSTER = [
    {"ID": 11587, "IsActive": False, "Name": "Grid, Accessg"},
    {"ID": 10301, "IsActive": True, "Name": "Abayan, HID. Tanyabella"},
]


def _client(pages: dict[str, str]) -> FakeMillenniumClient:
    return FakeMillenniumClient(roster=ROSTER, pages=pages)


def _creds(adapter, person_id="11587"):
    list(adapter.list_people())  # populates the roster the profiles hang off
    return list(adapter.list_credentials(person_id))


# --- names and identity --------------------------------------------------


def test_roster_name_splits_last_and_first():
    # This install repurposes the middle-name field as a card-type label.
    assert parse_roster_name("Abayan, HID. Tanyabella") == ("Tanyabella", "Abayan")
    assert parse_roster_name("Grid, Accessg") == ("Accessg", "Grid")


def test_email_is_synthesized_from_name_and_record_id():
    # Millennium stores no addresses, and the same human appears once per
    # card technology — so the record id has to be part of the address.
    assert (
        synthesize_email("Accessg", "Grid", "11587", "cards.example.com")
        == "accessg.grid.11587@cards.example.com"
    )
    assert synthesize_email("Jean-Luc", "O'Brien", "42", "x.test") == "jeanluc.obrien.42@x.test"


def test_no_domain_means_no_email():
    assert synthesize_email("A", "B", "1", "") == ""


def test_people_carry_the_synthesized_address(make_millennium_adapter, millennium_page):
    adapter = make_millennium_adapter(_client({"11587": millennium_page}))
    people = {p.id: p for p in adapter.list_people()}
    assert people["11587"].email == "accessg.grid.11587@cards.example.com"
    assert people["10301"].full_name == "Tanyabella Abayan"
    # Millennium has no cardholder-level enable flag; IsActive is UI state.
    assert people["10301"].active is True
    assert people["11587"].active is True


# --- the trigger ---------------------------------------------------------


def test_slot_with_the_trigger_format_enrolls(
    make_millennium_adapter, millennium_page, set_slot
):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    creds = _creds(make_millennium_adapter(_client({"11587": page})))
    assert len(creds) == 1
    assert creds[0].id == "slot1"
    assert creds[0].card_number == "1234"
    assert creds[0].site_code == "66"
    assert creds[0].trigger_active is True
    assert creds[0].status is CredentialStatus.ACTIVE
    # DESFire copies an existing card out; nothing is allocated.
    assert creds[0].allocate_identity is False


def test_slot_with_another_format_does_not_enroll(
    make_millennium_adapter, millennium_page, set_slot
):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=OTHER, active=True,
    )
    assert _creds(make_millennium_adapter(_client({"11587": page}))) == []


def test_empty_slot_never_triggers(make_millennium_adapter, millennium_page, set_slot):
    # An empty slot's <select> submits its first option, which would look
    # like a real format. Millennium does not persist a format for a slot
    # with no card, so an empty slot must never enroll.
    page = set_slot(millennium_page, 3, card_id="", card_number="", card_format=None)
    assert _creds(make_millennium_adapter(_client({"11587": page}))) == []


def test_inactive_card_is_suspended(make_millennium_adapter, millennium_page, set_slot):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=False,
    )
    creds = _creds(make_millennium_adapter(_client({"11587": page})))
    assert creds[0].status is CredentialStatus.SUSPENDED


# --- suspend / resume ----------------------------------------------------


def test_suspend_unchecks_active_and_leaves_the_card(
    make_millennium_adapter, millennium_page, set_slot
):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client)
    assert adapter.update_credential_status("11587", "slot1", CredentialStatus.SUSPENDED)

    _, form = client.saved[0]
    _, body = form.to_multipart()
    assert b'name="Card_1_Active"' not in body      # omission is "inactive"
    assert b'name="Card_1_CardID"\r\n\r\n7919\r\n' in body
    assert b'name="Card_1_EncodedCardNumber"\r\n\r\n1234\r\n' in body


def test_resume_rechecks_active(make_millennium_adapter, millennium_page, set_slot):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=False,
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client)
    assert adapter.update_credential_status("11587", "slot1", CredentialStatus.ACTIVE)
    _, body = client.saved[0][1].to_multipart()
    assert b'name="Card_1_Active"\r\n\r\ntrue\r\n' in body


def test_status_write_is_skipped_when_already_correct(
    make_millennium_adapter, millennium_page, set_slot
):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client)
    assert adapter.update_credential_status("11587", "slot1", CredentialStatus.ACTIVE)
    assert client.saved == []


def test_access_levels_survive_a_status_write(
    make_millennium_adapter, millennium_page, set_slot
):
    levels = '{"0":{"200":{"ALID":2,"AD":null,"ED":null}}}'
    page = millennium_page.replace(
        'name="Card_1_AccessLevels" id="Card_1_AccessLevels" value="{}"',
        'name="Card_1_AccessLevels" id="Card_1_AccessLevels" value="'
        + levels.replace('"', "&quot;") + '"',
    )
    page = set_slot(
        page, 1, card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    client = _client({"11587": page})
    make_millennium_adapter(client).update_credential_status(
        "11587", "slot1", CredentialStatus.SUSPENDED,
    )
    _, body = client.saved[0][1].to_multipart()
    assert levels.encode() in body


# --- Seos: AccessGrid mints, we write in --------------------------------


def _seos_page(millennium_page, set_slot, empty_slots=(2, 3)):
    """A cardholder holding a trigger card, with `empty_slots` free."""
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    for slot in (2, 3):
        if slot in empty_slots:
            page = set_slot(page, slot, card_id="", card_number="", card_format=None)
        else:
            page = set_slot(
                page, slot, card_id=f"79{slot}0", card_number=f"90{slot}",
                facility_code="66", card_format=OTHER, active=True,
            )
    return page


def test_seos_credential_asks_accessgrid_to_allocate(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    creds = _creds(adapter)
    assert len(creds) == 1
    assert creds[0].allocate_identity is True
    # Nothing of ours may reach AccessGrid, or it would not allocate.
    assert creds[0].card_number == ""
    assert creds[0].site_code == ""


def test_seos_needs_two_free_slots(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    # Only one slot free: a holder who installs on a phone and a watch would
    # run out, so we decline rather than provision half a person.
    page = _seos_page(millennium_page, set_slot, empty_slots=(3,))
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _creds(adapter) == []


def test_seos_writes_allocated_cards_into_empty_slots(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    identities = [
        CredentialIdentity("66", "5001", datetime(2026, 8, 18, 4, tzinfo=UTC), None),
        CredentialIdentity("66", "5002", datetime(2026, 8, 18, 4, tzinfo=UTC), None),
    ]
    assert adapter.write_back_credentials("11587", "seos-slot1", identities) is True

    _, form = client.saved[0]
    _, body = form.to_multipart()
    # A new card is created by leaving CardID empty and filling the rest.
    assert b'name="Card_2_CardID"\r\n\r\n\r\n' in body
    assert b'name="Card_2_EncodedCardNumber"\r\n\r\n5001\r\n' in body
    assert b'name="Card_3_EncodedCardNumber"\r\n\r\n5002\r\n' in body
    assert b'name="Card_2_FaciltyCode"\r\n\r\n66\r\n' in body
    assert b'name="Card_2_Active"\r\n\r\ntrue\r\n' in body
    # Written with the trigger format, so the next cycle recognises them.
    written = CardholderForm.parse(page)
    assert form.value("Card_2_CardFormat") == TRIGGER
    assert written.value("Card_2_CardFormat") != TRIGGER  # unchanged in the source
    # The existing card is untouched.
    assert b'name="Card_1_EncodedCardNumber"\r\n\r\n1234\r\n' in body


def test_seos_write_records_the_slots_it_used(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    adapter.write_back_credentials(
        "11587", "seos-slot1", [CredentialIdentity("66", "5001")],
    )
    assert SeosLedger.get("11587", "seos-slot1") == [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ]


def test_seos_write_is_idempotent(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    # Phase 4 re-offers the full list every cycle, so an identity already in
    # a slot must not be written a second time.
    page = _seos_page(millennium_page, set_slot)
    page = set_slot(
        page, 2, card_id="7930", card_number="5001", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.write_back_credentials(
        "11587", "seos-slot1", [CredentialIdentity("66", "5001")],
    ) is True
    assert client.saved == []


def test_seos_refuses_a_card_number_already_in_use(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    client = _client({"11587": page})
    client.number_free = False
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.write_back_credentials(
        "11587", "seos-slot1", [CredentialIdentity("66", "5001")],
    ) is False
    assert client.saved == []


def test_seos_writes_nothing_when_slots_run_short(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot, empty_slots=(3,))
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.write_back_credentials(
        "11587", "seos-slot1",
        [CredentialIdentity("66", "5001"), CredentialIdentity("66", "5002")],
    ) is False
    # All or nothing: a partial write would strand the second device.
    assert client.saved == []


def test_seos_suspend_targets_the_written_slots(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    page = set_slot(
        page, 2, card_id="7930", card_number="5001", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    SeosLedger.record(
        "11587", "seos-slot1",
        [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.update_credential_status(
        "11587", "seos-slot1", CredentialStatus.SUSPENDED,
    )
    _, body = client.saved[0][1].to_multipart()
    assert b'name="Card_2_Active"' not in body
    # The trigger card itself is the operator's, and stays as it was.
    assert b'name="Card_1_Active"\r\n\r\ntrue\r\n' in body


# --- setup-time discovery ------------------------------------------------


def test_card_formats_are_read_from_a_cardholder_page(
    make_millennium_adapter, millennium_page
):
    # Millennium has no endpoint that lists formats — the only source is a
    # card slot's <select>, so this must come off a real cardholder.
    client = FakeMillenniumClient(roster=ROSTER, pages={"11587": millennium_page})
    assert make_millennium_adapter(client).card_formats() == [
        ("1", "Wiegand Card"), ("7", "HID 37"),
    ]


# --- refresh strategy ----------------------------------------------------


def test_first_cycle_reads_every_cardholder(make_millennium_adapter, millennium_page):
    # A partial first read would leave tracked cardholders looking
    # credential-less, which phase 3 treats as a deletion.
    adapter = make_millennium_adapter(_client({"11587": millennium_page}))
    list(adapter.list_people())
    assert adapter._sweep == {"11587", "10301"}


def test_later_cycles_rotate_through_the_roster(make_millennium_adapter, millennium_page):
    adapter = make_millennium_adapter(
        _client({"11587": millennium_page}), sweep_budget=1,
    )
    list(adapter.list_people())          # cold: everyone
    adapter._cold = False

    list(adapter.list_people())
    first = set(adapter._sweep)
    list(adapter.list_people())
    second = set(adapter._sweep)
    # One at a time, and a different one each cycle — the whole roster is
    # covered rather than the same head being re-read forever.
    assert len(first) == len(second) == 1
    assert first != second
    assert first | second == {"11587", "10301"}


def test_enrolled_cardholders_are_refreshed_even_outside_the_slice(
    make_millennium_adapter, millennium_page, set_slot
):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    client = _client({"11587": page, "10301": page})
    adapter = make_millennium_adapter(client, sweep_budget=0)
    _creds(adapter)
    adapter._cold = False

    list(adapter.list_people())
    assert adapter._sweep == set()       # nothing scheduled for a re-read...
    assert list(adapter.list_credentials("11587"))  # ...but the enrolled one still is
    assert adapter._profiles["11587"]["enrolled"] is True


# --- safety rails --------------------------------------------------------


def test_status_write_is_refused_for_a_slot_with_no_card(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    # The card behind the credential was deleted in Millennium. Activating
    # an empty slot is not a thing, so we decline rather than post junk.
    page = _seos_page(millennium_page, set_slot)
    SeosLedger.record(
        "11587", "seos-slot1",
        [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.update_credential_status(
        "11587", "seos-slot1", CredentialStatus.ACTIVE,
    ) is False
    assert client.saved == []


def test_ledger_keeps_one_entry_per_physical_card(seos_ledger):
    # A rewritten card must not accumulate a second ledger row.
    SeosLedger.record(
        "11587", "seos-slot1",
        [
            {"slot": 2, "card_number": "5001", "facility_code": "66"},
            {"slot": 3, "card_number": "5001", "facility_code": "66"},
        ],
    )
    assert SeosLedger.get("11587", "seos-slot1") == [
        {"slot": 3, "card_number": "5001", "facility_code": "66"},
    ]
