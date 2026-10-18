"""Two ways a save was refused for a reason nothing reported.

Cardholder 12247 held a marker created without dates. Writing a credential
into it ticked Active and left the dates blank, which Millennium's own
rules forbid — the page marks both conditionalrequired on Card_N_Active —
and the server answered with a generic error page rather than a validation
message. Our side read any 200 as "the form came back with complaints", so
it reported a bad field, found none to name, and tried again next cycle.
Forever.

An activation date we can supply. An expiry we cannot: phase 3 deletes a
pass whose card has expired, so inventing one revokes a working pass on a
date nobody set. So the card is written and left inactive, and said out
loud.
"""

from __future__ import annotations

import logging
import re

import httpx
import pytest

from agsync.lib.pacs.base import CredentialIdentity
from agsync.lib.pacs.millennium_ultra.client import (
    MillenniumError,
    MillenniumUltraClient,
)
from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

ERROR_PAGE = """<!DOCTYPE html><html><head>
<title>Millennium Ultra - Millennium Ultra</title></head><body>
<div class="container body-content text-danger">
  <h3>System is temporarily unavailable.</h3>
  <div>Please close your browser and log in again.</div>
</div></body></html>"""


def _blank(page: str, field: str) -> str:
    return re.sub(rf'(name="{field}"[^>]*?value=")[^"]*(")', r"\g<1>\g<2>", page)


def _client(response: httpx.Response) -> MillenniumUltraClient:
    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
    )
    client._http = httpx.Client(transport=httpx.MockTransport(lambda r: response))
    return client


# =====================================================================
# A 200 that is not the form is not a rejection of the fields
# =====================================================================


def test_the_error_page_is_not_read_as_a_validation_failure(millennium_page):
    with pytest.raises(MillenniumError, match="System is temporarily unavailable"):
        _client(httpx.Response(200, text=ERROR_PAGE)).save_cardholder(
            "12247", CardholderForm.parse(millennium_page)
        )


def test_the_error_page_says_the_record_is_unchanged(millennium_page):
    with pytest.raises(MillenniumError, match="the record is unchanged"):
        _client(httpx.Response(200, text=ERROR_PAGE)).save_cardholder(
            "12247", CardholderForm.parse(millennium_page)
        )


def test_the_exchange_is_still_kept(millennium_page, tmp_path, monkeypatch):
    """It is the one case with nothing else to look at."""
    from agsync import failed_saves

    with pytest.raises(MillenniumError):
        _client(httpx.Response(200, text=ERROR_PAGE)).save_cardholder(
            "12247", CardholderForm.parse(millennium_page)
        )

    (row,) = failed_saves.recent()
    assert "temporarily unavailable" in row["response"]


def test_a_page_with_no_heading_at_all_still_reports_something(millennium_page):
    with pytest.raises(MillenniumError, match="carrying no cardholder form"):
        _client(httpx.Response(200, text="<html><body>nothing</body></html>")
                ).save_cardholder("12247", CardholderForm.parse(millennium_page))


def test_a_real_rejection_is_still_a_rejection(millennium_page):
    """The form coming back with complaints must keep working as before —
    False, not an exception."""
    rejected = millennium_page.replace(
        '<div id="tabHolder">',
        '<div class="field-validation-error" data-valmsg-for="Card_1_FacilityCode">'
        "wrong</div><div id=\"tabHolder\">", 1,
    )

    assert _client(httpx.Response(200, text=rejected)).save_cardholder(
        "12247", CardholderForm.parse(millennium_page)
    ) is False


# =====================================================================
# A card with no expiry is written, but not switched on
# =====================================================================


@pytest.fixture
def writing(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """A cardholder whose marker sits in slot 1, with dates I can strip."""
    from tests._fakes import FakeMillenniumClient

    def _build(page: str):
        client = FakeMillenniumClient(
            roster=[{"ID": "11587", "IsActive": True, "Name": "Grid, Access"}],
            pages={"11587": page},
        )
        adapter = make_millennium_adapter(client, mode="seos", trigger_card_format="7")
        list(adapter.list_people())
        return adapter, client
    return _build


def _written(client, slot: int = 1):
    """What the import carried for that slot. The decisions below are
    unchanged by the move from a full-form save to an import; only where
    the values travel changed."""
    from tests._fakes import written_slot

    return written_slot(client, slot)


def test_a_card_with_no_expiry_is_left_inactive(
    writing, millennium_page, set_slot, caplog
):
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format="7", active=True,
    )
    page = _blank(page, "Card_1_ExpirationDate")
    adapter, client = writing(page)

    with caplog.at_level(logging.WARNING):
        assert adapter.write_back_credentials(
            "11587", "seos", [CredentialIdentity("66", "1238")]
        ) is True

    written = _written(client)
    assert written.card_number == "1238", "the card is written"
    assert written.active is False, "but not switched on"
    assert "holds no expiration date" in caplog.text


def test_the_warning_names_the_card_so_somebody_can_fix_it(
    writing, millennium_page, set_slot, caplog
):
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format="7", active=True,
    )
    page = _blank(page, "Card_1_ExpirationDate")
    adapter, _ = writing(page)

    with caplog.at_level(logging.WARNING):
        adapter.write_back_credentials(
            "11587", "seos", [CredentialIdentity("66", "1238")]
        )

    assert "66/1238" in caplog.text


def test_a_card_with_an_expiry_is_switched_on_as_before(
    writing, millennium_page, set_slot
):
    adapter, client = writing(set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format="7", active=True,
    ))

    adapter.write_back_credentials("11587", "seos", [CredentialIdentity("66", "1238")])

    # The marker it replaces is already active, so under a diff there is no
    # Active cell to send — and "ends up on" is expressed as "is never
    # switched off". The contrast with the dateless case above is the point:
    # there, Active is explicitly written False.
    assert _written(client).active is None


def test_a_missing_activation_date_is_supplied(
    writing, millennium_page, set_slot
):
    """That one is ours to choose, and without it an active card is just as
    invalid as one with no expiry."""
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format="7", active=True,
    )
    page = _blank(page, "Card_1_ActivationDate")
    adapter, client = writing(page)

    adapter.write_back_credentials("11587", "seos", [CredentialIdentity("66", "1238")])

    written = _written(client)
    assert written.activation, "a date was supplied"
    # Not switched off, because the slot carries an expiry. Absent rather
    # than True because the marker was already active.
    assert written.active is None


def test_an_activation_date_already_there_is_left_alone(
    writing, millennium_page, set_slot
):
    adapter, client = writing(set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format="7", active=True,
    ))

    adapter.write_back_credentials("11587", "seos", [CredentialIdentity("66", "1238")])

    # Left alone by not being mentioned. The import carries no activation
    # column at all here, which is a stronger guarantee than the form save
    # could give: a date absent from the file cannot be written wrongly,
    # whereas one echoed back could be.
    assert _written(client).activation is None
