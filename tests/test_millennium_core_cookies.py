"""Millennium Ultra moved from classic ASP.NET to ASP.NET Core.

Every cookie the integration depends on was renamed except `timeoffset`,
which broke session capture outright: AG Connect seals the session the
moment the required cookie appears, and the name it waited for no longer
exists. The operator signed in successfully and the window sat there.

  classic (retired)              ASP.NET Core
  .AspNet.UltraAuth              Ultra
  UltraCompanyName               UltraCoreCompanyName
  __RequestVerificationToken     .AspNetCore.Antiforgery._<per-app suffix>
  timeoffset                     timeoffset

The last of those is the awkward one: the suffix is generated per
application, so no constant can name it and no exact match can find it.
"""

from __future__ import annotations

import pytest

from agsync.connect.capture import _wanted
from agsync.lib.pacs.millennium_ultra import DESCRIPTOR
from agsync.lib.pacs.millennium_ultra.client import (
    ANTIFORGERY_COOKIE_PREFIX,
    AUTH_COOKIE,
    COMPANY_COOKIE,
    MillenniumUltraClient,
)

ANTIFORGERY = ".AspNetCore.Antiforgery._1OIVLT0AFo"


# =====================================================================
# What the descriptor asks the side-car to watch for
# =====================================================================


def test_the_session_cookie_is_the_core_one():
    assert DESCRIPTOR.browser_login.required_cookie == "Ultra"


def test_the_companions_are_the_core_ones():
    extras = DESCRIPTOR.browser_login.extra_cookies

    assert "UltraCoreCompanyName" in extras
    assert "timeoffset" in extras
    assert ".AspNetCore.Antiforgery.*" in extras
    assert "DisableAlarmSound" in extras


def test_every_cookie_the_signed_in_browser_holds_is_captured():
    """Taken from a real signed-in session on hosted105.

    Carrying one we turn out not to need costs a few bytes; missing one we
    do need costs a 502 that reads as the server being unwell.
    """
    observed = [
        ".AspNetCore.Antiforgery._1OIVLT0AFo",
        "DisableAlarmSound",
        "timeoffset",
        "Ultra",
        "UltraCoreCompanyName",
    ]
    spec = DESCRIPTOR.browser_login
    patterns = (spec.required_cookie, *spec.extra_cookies)

    missed = [name for name in observed if not _wanted(name, patterns)]
    assert missed == []


def test_the_login_path_is_where_core_serves_it():
    """/Account/LogIn answers 302 to /Account on the Core build."""
    assert DESCRIPTOR.browser_login.login_path == "/Account"


# =====================================================================
# Matching a name that is only known by its prefix
# =====================================================================


@pytest.mark.parametrize(("name", "expected"), [
    ("Ultra", True),
    ("timeoffset", True),
    (ANTIFORGERY, True),
    (".AspNetCore.Antiforgery._somethingElse", True),
    # The prefix must not widen to the rest of the framework's cookies.
    (".AspNetCore.Session", False),
    (".AspNetCore.Identity.Application", False),
    ("UltraCompanyName", False),
    (".AspNet.UltraAuth", False),
    ("unrelated", False),
])
def test_which_cookies_are_captured(name, expected):
    patterns = ("Ultra", "timeoffset", ".AspNetCore.Antiforgery.*")

    assert _wanted(name, patterns) is expected


def test_a_pattern_without_a_star_stays_exact():
    """So one vendor's prefix cannot accidentally widen another's name."""
    assert _wanted("UltraCoreCompanyNameExtra", ("UltraCoreCompanyName",)) is False


# =====================================================================
# Sending them back
# =====================================================================


def _jar(**kw) -> dict[str, str]:
    client = MillenniumUltraClient(
        base_url="https://hosted105.mgiaccess.test",
        auth_cookie="auth-value", **kw,
    )
    try:
        return {c.name: c.value for c in client._http.cookies.jar}
    finally:
        client.close()


def test_the_session_cookie_goes_out_as_ultra():
    assert _jar()[AUTH_COOKIE] == "auth-value"
    assert AUTH_COOKIE == "Ultra"


def test_the_company_cookie_goes_out_under_the_core_name():
    assert _jar(company_name="Acme")[COMPANY_COOKIE] == "Acme"
    assert COMPANY_COOKIE == "UltraCoreCompanyName"


def test_the_antiforgery_cookie_goes_out_under_its_captured_name():
    jar = _jar(request_token="token-value", request_token_cookie=ANTIFORGERY)

    assert jar[ANTIFORGERY] == "token-value"


def test_a_token_with_no_name_is_not_guessed_at():
    """Sending it under a guessed name is no better than omitting it, and
    is worse to debug: the export endpoints answer 502 rather than 403, so
    a wrong name reads as the server being unwell."""
    jar = _jar(request_token="token-value")

    assert "token-value" not in jar.values()
    assert not [n for n in jar if n.startswith(ANTIFORGERY_COOKIE_PREFIX)]


# =====================================================================
# The rename that must NOT happen
# =====================================================================


def test_the_antiforgery_form_field_keeps_its_name(millennium_page):
    """`__RequestVerificationToken` names two different things.

    The *cookie* was renamed by the move to Core. The hidden *form field*
    was not — Core's AntiforgeryOptions.FormFieldName still defaults to it,
    and the live login page confirms it still carries that name. Renaming
    the field along with the cookie would break every form the adapter
    posts back, so this reads it the way the adapter does.
    """
    from agsync.lib.pacs.millennium_ultra.html_form import CardholderForm

    assert CardholderForm.parse(millennium_page).token == "PAGE-TOKEN"
