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

import httpx
import pytest

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


def _client(response: httpx.Response) -> MillenniumUltraClient:
    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test", auth_cookie="session"
    )
    client._http = httpx.Client(transport=httpx.MockTransport(lambda r: response))
    return client


# =====================================================================
# A 200 that is not the form is not a rejection of the fields
# =====================================================================


def test_the_error_page_says_the_record_is_unchanged(millennium_page):
    with pytest.raises(MillenniumError, match="the record is unchanged"):
        _client(httpx.Response(200, text=ERROR_PAGE)).save_cardholder(
            "12247", CardholderForm.parse(millennium_page)
        )


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
