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
from tests._fakes import FakeMillenniumClient, written_slot

TRIGGER = "7"  # "HID 37" in the captured page's format list
OTHER = "1"    # "Wiegand Card"

ROSTER = [
    {"ID": 11587, "IsActive": False, "Name": "Grid, Accessg"},
    {"ID": 10301, "IsActive": True, "Name": "Abayan, HID. Tanyabella"},
]


def _client(pages: dict[str, str]) -> FakeMillenniumClient:
    return FakeMillenniumClient(roster=ROSTER, pages=pages)


def _writable_client(pages: dict[str, str]) -> FakeMillenniumClient:
    """A client that can be written to, which means one with an export.

    The import matches a row on the cardholder's name, and the export is
    what establishes which names identify exactly one record — so without it
    the adapter declines to write at all. The suites about the A-Z sweep use
    `_client` instead, because the sweep is what happens when there is no
    export.
    """
    return FakeMillenniumClient(roster=ROSTER, pages=pages, export=True)


def _imported(client) -> str:
    """The CSV the one import of this test would have uploaded."""
    assert len(client.imported_csv) == 1, client.imported_csv
    return client.imported_csv[0]


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
    client = _writable_client({"11587": page})
    adapter = make_millennium_adapter(client)
    list(adapter.list_people())
    assert adapter.update_credential_status("11587", "slot1", CredentialStatus.SUSPENDED)

    # The import carries the one cell it means to change. The card's number
    # and id are not in the file at all, which is a stronger guarantee than
    # the form save could give: there, every field had to be echoed back
    # correctly to survive, and here there is nothing to echo.
    assert client.saved == []
    written = written_slot(client, 1)
    assert written.active is False, "the box is switched off"
    # The card's number and internal id are not in the file at all, which is
    # a stronger guarantee than the form save could give: there, every field
    # had to be echoed back correctly to survive.
    assert written.card_number is None
    assert written.facility_code is None
    assert "7919" not in _imported(client)


def test_resume_rechecks_active(make_millennium_adapter, millennium_page, set_slot):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=False,
    )
    client = _writable_client({"11587": page})
    adapter = make_millennium_adapter(client)
    list(adapter.list_people())
    assert adapter.update_credential_status("11587", "slot1", CredentialStatus.ACTIVE)
    assert "agsync_card_1_active" in _imported(client)
    assert "True" in _imported(client)


def test_status_write_is_skipped_when_already_correct(
    make_millennium_adapter, millennium_page, set_slot
):
    page = set_slot(
        millennium_page, 1,
        card_id="7919", card_number="1234", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    client = _writable_client({"11587": page})
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
    client = _writable_client({"11587": page})
    adapter = make_millennium_adapter(client)
    list(adapter.list_people())
    adapter.update_credential_status("11587", "slot1", CredentialStatus.SUSPENDED)

    # They survive by not being mentioned. A column the import does not
    # carry is a field it does not write, so there is no round-trip for a
    # status change to get wrong — which is what this used to be about.
    csv_text = _imported(client)
    assert "AccessLevels" not in csv_text
    assert "ALID" not in csv_text


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


def test_seos_needs_a_slot_besides_the_marker(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """A phone and a watch need one slot each.

    The marker's own is one of them — we overwrite it — so one more has to
    be free. With none, a holder who installs on both would run out, and we
    decline rather than provision half a person.
    """
    page = _seos_page(millennium_page, set_slot, empty_slots=())
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert _creds(adapter) == []


def test_one_free_slot_beside_the_marker_is_enough(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot, empty_slots=(3,))
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    assert len(_creds(adapter)) == 1


def test_seos_overwrites_the_marker_then_fills_an_empty_slot(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    identities = [
        CredentialIdentity("66", "5001", datetime(2026, 8, 18, 4, tzinfo=UTC), None),
        CredentialIdentity("66", "5002", datetime(2026, 8, 18, 4, tzinfo=UTC), None),
    ]
    assert adapter.write_back_credentials("11587", "seos", identities) is True

    # The marker's slot is reused. There is no CardID column and none is
    # needed: a value in a slot's columns is that slot's card, whichever
    # card happens to be in it.
    first = written_slot(client, 1)
    assert first.card_number == "5001"
    # The facility code is absent because the marker already carries 66, and
    # a diff does not restate what already matches.
    assert first.facility_code is None
    assert "7919" not in client.imported_csv[-1]
    # Already active, so a diff has no Active cell to send.
    assert first.active is None
    # The second credential goes into the empty slot. No CardID column is
    # sent there either — the slot is the address.
    second = written_slot(client, 2)
    assert second.card_number == "5002"
    assert second.facility_code == "66", "an empty slot needs it stated"
    # Written with the trigger format's display name, so the next cycle
    # recognises them. The import's Card Format column is a drop-down and
    # takes the name, where the page's <select> carries the numeric id.
    assert second.card_format == "HID 37"
    source = CardholderForm.parse(page)
    assert source.value("Card_2_CardFormat") != TRIGGER  # unchanged in the source


def test_seos_write_records_the_slots_it_used(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    adapter = make_millennium_adapter(_client({"11587": page}), mode=MODE_SEOS)
    adapter.write_back_credentials(
        "11587", "seos", [CredentialIdentity("66", "5001")],
    )
    assert SeosLedger.get("11587", "seos") == [
        {"slot": 1, "card_number": "5001", "facility_code": "66"},
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
    # Returns False because nothing was written — the invariant that matters
    # is that the cardholder was not touched.
    assert adapter.write_back_credentials(
        "11587", "seos", [CredentialIdentity("66", "5001")],
    ) is False
    assert client.saved == []


def test_seos_refuses_a_card_number_already_in_use(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot)
    client = _client({"11587": page})
    client.number_free = False
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.write_back_credentials(
        "11587", "seos", [CredentialIdentity("66", "5001")],
    ) is False
    assert client.saved == []


def test_seos_writes_nothing_when_slots_run_short(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    page = _seos_page(millennium_page, set_slot, empty_slots=())
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.write_back_credentials(
        "11587", "seos",
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
        "11587", "seos",
        [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
    )
    client = _writable_client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    list(adapter.list_people())
    assert adapter.update_credential_status(
        "11587", "seos", CredentialStatus.SUSPENDED,
    )
    csv_text = _imported(client)
    assert "agsync_card_2_active" in csv_text
    # The trigger card itself is the operator's. It is not in the file, so
    # nothing this write does can reach it.
    assert "agsync_card_1_active" not in csv_text


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
        "11587", "seos",
        [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
    )
    client = _writable_client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    assert adapter.update_credential_status(
        "11587", "seos", CredentialStatus.ACTIVE,
    ) is False
    assert client.saved == []


def test_ledger_keeps_one_entry_per_physical_card(seos_ledger):
    # A rewritten card must not accumulate a second ledger row.
    SeosLedger.record(
        "11587", "seos",
        [
            {"slot": 2, "card_number": "5001", "facility_code": "66"},
            {"slot": 3, "card_number": "5001", "facility_code": "66"},
        ],
    )
    assert SeosLedger.get("11587", "seos") == [
        {"slot": 3, "card_number": "5001", "facility_code": "66"},
    ]


def test_a_credential_removed_in_the_pacs_is_never_recreated(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """An operator's deletion in Millennium is final.

    Phase 4 re-offers every tracked card's identities on every cycle. Without
    this, a card somebody deliberately deleted in Millennium would be written
    straight back on the next sync — this integration overruling a person
    working in their own system, forever.
    """
    page = _seos_page(millennium_page, set_slot)          # slots 2 and 3 empty
    SeosLedger.record(
        "11587", "seos",
        [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)

    assert adapter.write_back_credentials(
        "11587", "seos", [CredentialIdentity("66", "5001")],
    ) is False
    assert client.saved == []
    # And it stays remembered, so a later cycle does not rediscover it.
    assert SeosLedger.get("11587", "seos") == [
        {"slot": 2, "card_number": "5001", "facility_code": "66"},
    ]


def test_a_genuinely_new_device_is_still_written_alongside_a_removed_one(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    # Refusing to resurrect must not block the second device a holder
    # installs later, which is the whole point of re-offering.
    page = _seos_page(millennium_page, set_slot)
    SeosLedger.record(
        "11587", "seos",
        [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)

    assert adapter.write_back_credentials(
        "11587", "seos",
        [CredentialIdentity("66", "5001"), CredentialIdentity("66", "5002")],
    ) is True
    assert written_slot(client, 1).card_number == "5002"      # the new one
    assert "5001" not in client.imported_csv[-1]              # not the deleted one


def test_a_long_sweep_reports_progress(
    make_millennium_adapter, millennium_page, caplog, monkeypatch
):
    """Five silent minutes is indistinguishable from a hang.

    The first sweep of a large install reads every cardholder page and used
    to log nothing between starting and finishing, which reads as a stopped
    sync rather than a working one.
    """
    from agsync.lib.pacs.millennium_ultra import adapter as module

    monkeypatch.setattr(module, "PROGRESS_EVERY", 2)
    roster = [{"ID": i, "IsActive": False, "Name": f"Person{i}, A"} for i in range(5)]
    pages = {str(i): millennium_page for i in range(5)}
    adapter = make_millennium_adapter(FakeMillenniumClient(roster=roster, pages=pages))

    with caplog.at_level("INFO"):
        for person in adapter.list_people():
            list(adapter.list_credentials(person.id))

    progress = [m for m in (r.getMessage() for r in caplog.records) if "read" in m and "of" in m]
    assert progress, "a long sweep must say something while it runs"
    assert "of 5 cardholder pages" in progress[0]


def test_the_marker_is_told_apart_from_our_own_cards(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The ledger is the only thing that distinguishes them.

    Cards we write carry the trigger format, so `_is_trigger` is true of
    ours too. Without the ledger, a second credential would overwrite the
    first one we had just issued instead of the operator's placeholder.
    """
    page = _seos_page(millennium_page, set_slot)
    page = set_slot(
        page, 2, card_id="7930", card_number="5001", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    SeosLedger.record(
        "11587", "seos",
        [{"slot": 2, "card_number": "5001", "facility_code": "66"}],
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)

    assert adapter.write_back_credentials(
        "11587", "seos",
        [CredentialIdentity("66", "5001"), CredentialIdentity("66", "5002")],
    ) is True
    # Slot 1 is the marker, so 5002 lands there.
    assert written_slot(client, 1).card_number == "5002"
    # Slot 2 holds a card we issued, and keeps it by not being in the file.
    assert written_slot(client, 2) is None


def test_a_cardholder_with_no_marker_left_writes_into_empty_slots(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Every trigger-format card is ours, so there is nothing to overwrite."""
    page = _seos_page(millennium_page, set_slot)
    page = set_slot(
        page, 1, card_id="7919", card_number="5001", facility_code="66",
        card_format=TRIGGER, active=True,
    )
    SeosLedger.record(
        "11587", "seos",
        [{"slot": 1, "card_number": "5001", "facility_code": "66"}],
    )
    client = _client({"11587": page})
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)

    assert adapter.write_back_credentials(
        "11587", "seos",
        [CredentialIdentity("66", "5001"), CredentialIdentity("66", "5002")],
    ) is True
    # 5001 already sits in slot 1, so only the new card is written, into the
    # empty slot beside it.
    assert written_slot(client, 2).card_number == "5002"
    assert written_slot(client, 1) is None
