"""The round-trip save: parse a real cardholder page, reproduce a real POST.

Millennium's save replaces the entire cardholder record, so a field we fail
to echo back is a field we delete. The strongest available check is that our
serializer reproduces, byte for byte, what a browser actually sent for the
same page — that is what test_round_trip_is_byte_identical_to_browser_post
asserts, using a captured page and the captured POST from the same session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

FIXTURES = Path(__file__).parent / "millennium_fixtures"
# The boundary the captured browser submit used; the body embeds it.
CAPTURED_BOUNDARY = "----WebKitFormBoundaryABoaI7NXhVpaIgxp"


@pytest.fixture
def page() -> str:
    return (FIXTURES / "cardholder_11587_form.html").read_text(encoding="utf-8")


@pytest.fixture
def browser_post() -> bytes:
    return (FIXTURES / "cardholder_11587_post.txt").read_bytes()


@pytest.fixture
def form(page) -> CardholderForm:
    return CardholderForm.parse(page)


# --- the guarantee -------------------------------------------------------


def test_round_trip_is_byte_identical_to_browser_post(form, browser_post):
    # The browser had refreshed its anti-forgery token, so pin ours to the
    # captured one; everything else must match without being touched.
    form.set_value("__RequestVerificationToken", "BROWSER-TOKEN")
    _, body = form.to_multipart(boundary=CAPTURED_BOUNDARY)
    assert body == browser_post


def test_every_named_control_is_parsed(form):
    # 90 named controls on the page; 83 are submitted (7 unchecked boxes).
    assert len(form.controls) == 90
    assert sum(1 for c in form.controls if c.submits) == 83


# --- browser submit semantics -------------------------------------------


def test_unchecked_checkboxes_are_omitted(form):
    # Every slot on this capture is inactive. The absence of Card_N_Active
    # *is* "inactive" — that omission is how a credential gets suspended.
    _, body = form.to_multipart()
    for slot in (1, 2, 3):
        assert f'name="Card_{slot}_Active"'.encode() not in body
    # The tenant checkbox is checked, so it is submitted with its value.
    assert b'name="Card_1_Tenant_0"\r\n\r\n0\r\n' in body


def test_checking_a_box_adds_it_back(form):
    # Resuming a suspended credential is exactly this write.
    form.set_checked("Card_1_Active", True)
    _, body = form.to_multipart()
    assert b'name="Card_1_Active"\r\n\r\ntrue\r\n' in body


def test_select_without_a_selected_option_submits_the_first(form):
    # Slot 3 holds no card, so its format select has no selection — the
    # browser submits the first option, and so must we.
    assert form.value("Card_3_CardFormat") == "4"
    assert form.options("Card_3_CardFormat")[0].value == "4"


def test_select_with_a_selected_option_submits_it(form):
    assert form.value("Card_1_CardFormat") == "1"


def test_setting_a_select_moves_the_selection(form):
    form.set_value("Card_3_CardFormat", "7")
    assert form.value("Card_3_CardFormat") == "7"
    assert [o.value for o in form.options("Card_3_CardFormat") if o.selected] == ["7"]


def test_setting_an_absent_select_option_raises(form):
    with pytest.raises(KeyError):
        form.set_value("Card_3_CardFormat", "does-not-exist")


def test_setting_an_unknown_field_raises(form):
    # A missing field means the page shape changed; guessing would risk
    # writing the wrong thing into a live record.
    with pytest.raises(KeyError):
        form.set_value("Card_9_EncodedCardNumber", "1")


def test_file_inputs_round_trip_as_empty_parts(form):
    # An empty photo part is a no-op — a cardholder's existing photo
    # survives it — whereas dropping the part is untested behaviour.
    _, body = form.to_multipart()
    assert b'name="photoImage"; filename=""' in body
    assert b'name="signImage"; filename=""' in body


def test_disabled_controls_are_still_submitted(form):
    # This app mis-renders `disabled` into the class attribute and its own
    # JS re-enables fields before submit, so the browser posts them.
    _, body = form.to_multipart()
    assert b'name="Sex"' in body
    assert b'name="Card_1_PinCode"' in body


# --- values the adapter depends on --------------------------------------


def test_escaped_json_values_are_unescaped(page):
    levels = '{"0":{"200":{"ALID":2,"AD":null,"ED":null}}}'
    escaped = levels.replace('"', "&quot;")
    populated = page.replace(
        '<input type="hidden" name="Card_1_AccessLevels" id="Card_1_AccessLevels" value="{}" />',
        f'<input type="hidden" name="Card_1_AccessLevels" id="Card_1_AccessLevels" value="{escaped}" />',
    )
    form = CardholderForm.parse(populated)
    # Access levels are read and replayed verbatim; this adapter never edits
    # them, and re-posting the escaped text would corrupt them.
    assert form.value("Card_1_AccessLevels") == levels
    _, body = form.to_multipart()
    assert levels.encode() in body


def test_reads_slot_values_and_token(form):
    assert form.token == "PAGE-TOKEN"
    assert form.action == "/Cardholders/Cardholders/Index/11587"
    assert form.value("ID") == "11587"
    assert form.value("Card_2_CardID") == "7922"
    assert form.value("Card_2_EncodedCardNumber") == "2"
    assert form.value("Card_2_FaciltyCode") == "2"
    assert form.value("Card_3_CardID") == ""
    assert form.is_checked("Card_2_Active") is False
    assert form.is_checked("Card_1_Tenant_0") is True


def test_parse_rejects_a_page_with_no_form():
    with pytest.raises(ValueError):
        CardholderForm.parse("<html><body>Signed out</body></html>")


def test_generated_boundary_does_not_collide_with_content(form):
    content_type, body = form.to_multipart()
    boundary = content_type.split("boundary=", 1)[1]
    assert body.count(f"--{boundary}".encode()) == 84  # 83 parts + the closer
