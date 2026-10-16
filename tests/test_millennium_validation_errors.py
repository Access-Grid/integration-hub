"""Saying why Millennium refused a save.

A rejection is a 200 carrying the same form back with the complaints
written into it, so the reasons are already in hand — the log just threw
them away and said "(validation error)". That is the difference between a
cardholder that stops syncing for a reason somebody can act on and one
that stops for no stated reason at all, which is how 12247 sat refused
cycle after cycle.

The markup shapes here are taken from a live hosted105 cardholder page: in
the accepted state the same elements are present but empty and classed
"-valid", so the class is what separates a complaint from a placeholder.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.millennium_ultra.html_form import validation_errors

FIELD_ERROR = (
    '<div class="text-danger field-validation-error validator" '
    'data-valmsg-replace="true" data-valmsg-for="Card_1_EncodedCardNumber">'
    "The Card 1 field Encoded Card No. is duplicated</div>"
)
FIELD_VALID = (
    '<div class="text-danger field-validation-valid validator" '
    'data-valmsg-replace="true" data-valmsg-for="FirstName"></div>'
)
SUMMARY_ERRORS = (
    '<div class="text-danger validation-summary-errors" data-valmsg-summary="true">'
    "<ul><li>Employee ID already exists.</li>"
    "<li>The field Last Name is required</li></ul></div>"
)
SUMMARY_VALID = (
    '<div class="text-danger validation-summary-valid" data-valmsg-summary="true">'
    '<ul><li style="display:none"></li></ul></div>'
)


def test_a_field_complaint_names_the_field_and_the_reason():
    assert validation_errors(f"<form>{FIELD_ERROR}</form>") == [
        "Card_1_EncodedCardNumber: The Card 1 field Encoded Card No. is duplicated"
    ]


def test_a_summary_lists_every_complaint():
    assert validation_errors(f"<form>{SUMMARY_ERRORS}</form>") == [
        "Employee ID already exists.",
        "The field Last Name is required",
    ]


def test_an_accepted_page_carries_no_complaint():
    """The same elements are there when nothing is wrong, classed -valid and
    empty. Reading those as errors would report a problem on every save."""
    page = f"<form>{SUMMARY_VALID}{FIELD_VALID}</form>"

    assert validation_errors(page) == []


def test_the_real_page_is_quiet(millennium_page):
    """The captured cardholder page is a successful render."""
    assert validation_errors(millennium_page) == []


def test_both_kinds_are_collected_together():
    page = f"<form>{SUMMARY_ERRORS}{FIELD_ERROR}</form>"

    assert len(validation_errors(page)) == 3


def test_the_same_complaint_is_not_reported_twice():
    """A field's message is rendered beside it and in the summary, and one
    problem read as two sends somebody looking for a second."""
    duplicated = (
        '<div class="validation-summary-errors"><ul><li>Employee ID already '
        "exists.</li></ul></div>"
        '<div class="field-validation-error">Employee ID already exists.</div>'
    )

    assert validation_errors(duplicated) == ["Employee ID already exists."]


def test_markup_inside_a_message_is_read_as_text():
    nested = (
        '<div class="field-validation-error" data-valmsg-for="Card_2_FacilityCode">'
        "The Card 2 <b>Facility Code</b> is wrong</div>"
    )

    assert validation_errors(nested) == [
        "Card_2_FacilityCode: The Card 2 Facility Code is wrong"
    ]


def test_whitespace_is_collapsed():
    messy = (
        '<div class="field-validation-error">\n   The field   is\n\n required  '
        "</div>"
    )

    assert validation_errors(messy) == ["The field is required"]


def test_entities_are_decoded():
    assert validation_errors(
        '<div class="field-validation-error">Dates don&#x27;t overlap</div>'
    ) == ["Dates don't overlap"]


@pytest.mark.parametrize("page", ["", "not html at all", "<html><body></body></html>"])
def test_a_page_with_nothing_in_it_is_not_an_error(page):
    """An empty answer is reported as "no complaint found", which is a
    different thing from a crash and a different thing from a clean save."""
    assert validation_errors(page) == []


def test_a_broken_page_does_not_raise():
    """Diagnostics run on whatever the server sent, which on a bad day is
    not well-formed."""
    assert isinstance(validation_errors("<div class='field-validation-error'>x"), list)


# =====================================================================
# Reaching the log, which is the point
# =====================================================================


def _rejecting_client(body: str):
    """A client whose save is refused the way Millennium refuses one: 200,
    carrying the form back rather than a redirect."""
    import httpx

    from agsync.lib.pacs.millennium_ultra.client import MillenniumUltraClient

    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
    )
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))
    )
    return client


def test_the_reasons_reach_the_log(millennium_page, caplog):
    import logging

    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

    client = _rejecting_client(f"<form>{SUMMARY_ERRORS}{FIELD_ERROR}</form>")
    form = CardholderForm.parse(millennium_page)

    with caplog.at_level(logging.ERROR):
        assert client.save_cardholder("12247", form) is False

    assert "Employee ID already exists." in caplog.text
    assert "The Card 1 field Encoded Card No. is duplicated" in caplog.text
    assert "12247" in caplog.text


def test_a_refusal_with_no_message_says_so(millennium_page, caplog):
    """Rather than an empty list rendered as nothing, which reads like the
    save was refused for no reason at all."""
    import logging

    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

    client = _rejecting_client("<form><p>Something else went wrong</p></form>")
    form = CardholderForm.parse(millennium_page)

    with caplog.at_level(logging.ERROR):
        client.save_cardholder("12247", form)

    assert "no validation message" in caplog.text


def test_a_flood_of_complaints_is_capped(millennium_page, caplog):
    """One cardholder must not be able to fill the log."""
    import logging

    from agsync.lib.pacs.millennium_ultra.client import MAX_VALIDATION_ERRORS
    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

    many = "".join(
        f'<div class="field-validation-error">complaint {i}</div>' for i in range(25)
    )
    client = _rejecting_client(f"<form>{many}</form>")
    form = CardholderForm.parse(millennium_page)

    with caplog.at_level(logging.ERROR):
        client.save_cardholder("12247", form)

    assert "complaint 0" in caplog.text
    assert f"and {25 - MAX_VALIDATION_ERRORS} more complaint(s)" in caplog.text
    assert "complaint 24" not in caplog.text
