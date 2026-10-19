"""Reading the cardholder page the Core build actually sends.

The move to ASP.NET Core corrected one misspelling: Card_N_FaciltyCode
became Card_N_FacilityCode. Nothing else on the page moved — the field
names here were checked one by one against a live hosted105 cardholder
page on 2026-10-16, including Card_N_AccessLevels, which did not change.

That single rename cost a production incident, and the shape of the
failure is the thing worth remembering. `form.value()` answers "" for a
field that is not there, so every card read with an empty facility code.
The ledger's 99/165 stopped matching the slot's ("", "165"), every pass
was read as revoked and suspended, and the trigger card behind it then
looked unclaimed and earned a new one. The write path had refused to guess
all along — set_value raises for an unknown field — and only the read path
was credulous, so the first sign of trouble was a KeyError on write, long
after the reads had already decided everyone's pass was gone.
"""

from __future__ import annotations

import re

import pytest

from agsync.lib.pacs.millennium_ultra.client import MillenniumError
from agsync.lib.pacs.millennium_ultra.export import (
    _REQUIRED_SLOT_FIELDS,
    _check_slot_shape,
    read_slots,
)
from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm


@pytest.fixture
def form(millennium_page) -> CardholderForm:
    return CardholderForm.parse(millennium_page)


def _strip(page: str, field: str) -> str:
    """Remove one control, whichever element renders it.

    CardFormat is a <select>; the rest are <input>. A helper that only knew
    about inputs would quietly leave the select in place and report that
    the guard had been tested when it had not.
    """
    name = re.escape(field)
    stripped = re.sub(rf'<input[^>]*name="{name}"[^>]*/?>', "", page)
    stripped = re.sub(
        rf'<select[^>]*name="{name}".*?</select>', "", stripped, flags=re.S
    )
    assert stripped != page, f"{field} was not in the page to begin with"
    return stripped


def _without(page: str, field: str) -> CardholderForm:
    """The page as it looked after a field was renamed under us."""
    return CardholderForm.parse(_strip(page, field))


# =====================================================================
# The name itself
# =====================================================================


def test_the_facility_code_is_spelled_the_way_core_spells_it(form):
    assert form.value("Card_1_FacilityCode") != ""


def test_the_old_misspelling_is_gone_from_the_page(millennium_page):
    assert "FaciltyCode" not in millennium_page


def test_a_card_reads_with_its_facility_code(form):
    """The read that went wrong. An empty facility code here is what made
    the ledger's 99/165 stop matching the slot's ("", "165")."""
    slots = read_slots(form)

    populated = [s for s in slots if not s.empty]
    assert populated, "the fixture should have at least one card"
    assert all(s.facility_code for s in populated), _describe_facility(populated)


def _describe_facility(slots) -> str:
    return "facility code missing on: " + ", ".join(
        f"slot {s.index} ({s.facility_code!r}/{s.card_number})" for s in slots
    )


def test_access_levels_did_not_move(form):
    """Checked against the live page too — this one was the bigger worry,
    since it would have stopped access levels transferring silently."""
    assert form.find("Card_1_AccessLevels") is not None


def test_the_antiforgery_form_field_did_not_move(form):
    """Renamed as a cookie, not as a form field."""
    assert form.token != ""


# =====================================================================
# The guard that would have caught it on cycle one
# =====================================================================


@pytest.mark.parametrize("field", _REQUIRED_SLOT_FIELDS)
def test_a_renamed_field_stops_the_cycle(millennium_page, field):
    """Loudly, rather than reading as blank and revoking everybody."""
    broken = _without(millennium_page, f"Card_1_{field}")

    with pytest.raises(MillenniumError, match="page shape has changed"):
        _check_slot_shape(broken, "Card_1_")


def test_the_error_names_the_field_that_moved(millennium_page):
    broken = _without(millennium_page, "Card_1_FacilityCode")

    with pytest.raises(MillenniumError, match="Card_1_FacilityCode"):
        _check_slot_shape(broken, "Card_1_")


def test_reading_a_whole_cardholder_stops_too(millennium_page):
    """The guard has to sit in the read path, not beside it."""
    broken = _without(millennium_page, "Card_2_FacilityCode")

    with pytest.raises(MillenniumError):
        read_slots(broken)


def test_a_page_that_is_merely_intact_is_left_alone(form):
    for index in (1, 2, 3):
        _check_slot_shape(form, f"Card_{index}_")


def test_a_slot_that_is_not_rendered_at_all_is_not_an_error(millennium_page):
    """Absence of a whole slot is a different claim from a renamed field,
    and only the second one means the page shape moved under us."""
    page = millennium_page
    for field in _REQUIRED_SLOT_FIELDS:
        page = _strip(page, f"Card_3_{field}")

    _check_slot_shape(CardholderForm.parse(page), "Card_3_")


# =====================================================================
# What we send back, against a real Core save
# =====================================================================

# The Card_1 block of a live update POST to hosted105, captured from the
# browser on 2026-10-16 (cardholder 12478). The order is the page's own, so
# this is really an assertion that we echo the form rather than rebuild it
# from our own idea of the shape.
CORE_CARD_BLOCK = (
    "CardID",
    "PrintedCardNumber",
    "ActivationDate",
    "Active",
    "EncodedCardNumber",
    "ExpirationDate",
    "FacilityCode",
    "CardFormat",
    "BadgeType",
    "CardClass",
    "PinCode",
    "IssueCode",
    "CardExpirationCount",
    "Tenant_0",
    "AccessLevels",
)


def _submitted_names(form: CardholderForm) -> list[str]:
    _content_type, body = form.to_multipart()
    return re.findall(r'name="([^"]+)"', body.decode("utf-8", "replace"))


def test_a_saved_card_matches_the_shape_core_sends(form):
    form.set_checked("Card_1_Active", True)

    names = _submitted_names(form)
    block = [n.removeprefix("Card_1_") for n in names if n.startswith("Card_1_")]

    assert block == list(CORE_CARD_BLOCK)


def test_an_inactive_card_omits_the_checkbox(form):
    """Unchecked checkboxes are not submitted, which is why Active is the
    one field whose presence varies between two otherwise identical saves."""
    form.set_checked("Card_1_Active", False)

    assert "Card_1_Active" not in _submitted_names(form)


def test_a_checked_box_is_submitted_as_the_browser_submits_it(form):
    form.set_checked("Card_1_Active", True)

    _content_type, body = form.to_multipart()
    text = body.decode("utf-8", "replace")
    assert re.search(r'name="Card_1_Active"\r\n\r\ntrue\r\n', text)


def test_the_antiforgery_token_goes_where_the_page_puts_it(millennium_page):
    """Core renders it last; the build before it rendered it first. Nothing
    here should know or care, because the form is echoed in page order."""
    moved = millennium_page.replace(
        '<input name="__RequestVerificationToken" type="hidden" value="PAGE-TOKEN" />',
        "", 1,
    ).replace(
        "</form>",
        '<input name="__RequestVerificationToken" type="hidden" value="PAGE-TOKEN" />'
        "</form>", 1,
    )

    names = _submitted_names(CardholderForm.parse(moved))

    assert names[-1] == "__RequestVerificationToken"
