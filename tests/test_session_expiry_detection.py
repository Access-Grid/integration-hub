"""Recognising a dead Millennium session.

A dead cookie does not return an error. It has been seen returning an empty
body, a redirect to the login page, and — the one that caused real trouble —
a valid empty `[]` alongside HTTP 500 on the cardholder screens. That last
shape reads as "this PACS has no cardholders", which stops the sync without
reporting anything.
"""

from __future__ import annotations

import httpx
import pytest

from agsync.lib.pacs.base import PacsAuthExpired
from agsync.lib.pacs.millennium_ultra.client import (
    MillenniumAuthError,
    MillenniumUltraClient,
)


def _client(handler) -> MillenniumUltraClient:
    client = MillenniumUltraClient(
        base_url="https://millennium.test", auth_cookie="cookie",
    )
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://millennium.test",
        follow_redirects=False,
    )
    return client


def _roster(body: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if "GetList" in request.url.path:
            return httpx.Response(status, text=body)
        return httpx.Response(200, text="<form id='myForm'><input name='x'></form>")
    return handler


# --- the shapes a dead session produces ---------------------------------


def test_an_empty_body_is_an_expired_session():
    with pytest.raises(MillenniumAuthError):
        _client(_roster("")).list_cardholders()


def test_a_valid_but_empty_roster_is_an_expired_session():
    # The shape that went undetected: well-formed JSON, no rows, HTTP 200.
    with pytest.raises(MillenniumAuthError, match="no cardholders"):
        _client(_roster("[]")).list_cardholders()


def test_a_redirect_to_the_login_page_is_an_expired_session():
    def handler(request):
        return httpx.Response(302, headers={"location": "/Account/LogIn?ReturnUrl=%2F"})
    with pytest.raises(MillenniumAuthError):
        _client(handler).list_cardholders()


def test_a_500_on_a_cardholder_screen_is_an_expired_session():
    def handler(request):
        if "GetList" in request.url.path:
            return httpx.Response(200, text='[{"ID": 1, "Name": "A, B"}]')
        return httpx.Response(500, text="<html>error</html>")
    with pytest.raises(MillenniumAuthError, match="probably expired"):
        _client(handler).get_cardholder_form(11587)


def test_all_of_them_are_the_engine_wide_signal():
    # The engine keys off PacsAuthExpired, not the vendor's own class.
    assert issubclass(MillenniumAuthError, PacsAuthExpired)


# --- and what a live session looks like ---------------------------------


def test_a_populated_roster_is_fine():
    rows = _client(_roster('[{"ID": 11587, "Name": "Grid, Accessg"}]')).list_cardholders()
    assert [r["ID"] for r in rows] == [11587]


def test_one_empty_letter_among_many_is_not_an_expiry():
    # Most letters are legitimately empty on a small install.
    def handler(request):
        letter = request.url.params.get("firstLetter")
        body = '[{"ID": 11587, "Name": "Grid, Accessg"}]' if letter == "71" else "[]"
        return httpx.Response(200, text=body)
    assert len(_client(handler).list_cardholders()) == 1


def test_test_connection_proves_a_read_rather_than_a_stored_cookie():
    ok, message = _client(_roster("[]")).test_connection()
    assert ok is False
    assert "expired" in message

    ok, message = _client(_roster('[{"ID": 11587, "Name": "Grid, A"}]')).test_connection()
    assert ok is True
    assert "11587" in message
