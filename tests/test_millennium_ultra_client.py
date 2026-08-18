"""Millennium Ultra client: expiry detection, roster paging, fault tolerance."""

from __future__ import annotations

import json

import httpx
import pytest

from agsync.lib.pacs.millennium_ultra.client import (
    MillenniumUltraClient,
    MillenniumUltraError,
    SessionExpired,
)
from agsync.lib.pacs.millennium_ultra.parse import ROSTER_BUCKETS


def build(handler) -> MillenniumUltraClient:
    return MillenniumUltraClient(
        base_url="https://mu.test",
        session_cookies={".AspNet.UltraAuth": "abc"},
        max_workers=2,
        transport=httpx.MockTransport(handler),
    )


# --- session expiry ------------------------------------------------------


def test_redirect_to_login_raises_session_expired():
    def handler(request):
        return httpx.Response(302, headers={"location": "/Account/Login?ReturnUrl=%2f"})

    with build(handler) as client, pytest.raises(SessionExpired) as excinfo:
        client.list_roster()
    # The message is shown to the operator, so it has to say what to do.
    assert "sign in again" in str(excinfo.value).lower()


def test_session_expired_is_distinct_from_transport_failure():
    # One needs a human; the other heals itself. The engine treats them
    # differently, so they must not collapse into one exception type.
    assert issubclass(SessionExpired, MillenniumUltraError)

    def handler(request):
        raise httpx.ConnectError("no route to host")

    with build(handler) as client:
        with pytest.raises(MillenniumUltraError) as excinfo:
            client.list_roster()
        assert not isinstance(excinfo.value, SessionExpired)


def test_other_redirects_are_not_treated_as_expiry():
    def handler(request):
        return httpx.Response(302, headers={"location": "/Somewhere/Else"})

    with build(handler) as client, pytest.raises(MillenniumUltraError) as excinfo:
        client.list_roster()
    assert not isinstance(excinfo.value, SessionExpired)


def test_http_error_surfaces():
    def handler(request):
        return httpx.Response(500)

    with build(handler) as client, pytest.raises(MillenniumUltraError):
        client.get_cardholder_html("1")


# --- roster --------------------------------------------------------------


def test_roster_covers_every_bucket_and_dedupes():
    seen_buckets = []

    def handler(request):
        bucket = request.url.params.get("firstLetter")
        seen_buckets.append(bucket)
        # Two buckets both report cardholder 1 — the roster must dedupe by ID.
        rows = [{"ID": 1, "Name": "B, HID. A"}] if bucket in ("65", "66") else []
        return httpx.Response(200, content=json.dumps(rows), headers={"content-type": "application/json"})

    with build(handler) as client:
        roster = client.list_roster()

    assert len(seen_buckets) == len(ROSTER_BUCKETS)
    assert "0" in seen_buckets  # the "Other" bucket is not forgotten
    assert len(roster) == 1


def test_roster_sends_xhr_header():
    # The helper endpoint returns JSON only for XMLHttpRequest callers.
    headers = {}

    def handler(request):
        headers.update(request.headers)
        return httpx.Response(200, content="[]", headers={"content-type": "application/json"})

    with build(handler) as client:
        client.list_roster()
    assert headers["x-requested-with"] == "XMLHttpRequest"


# --- detail pages --------------------------------------------------------


def test_one_bad_cardholder_does_not_lose_the_sweep():
    def handler(request):
        if "/Index/2" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, text="<html>ok</html>")

    with build(handler) as client:
        pages = client.get_cardholders_html(["1", "2", "3"])

    # The unreadable record is skipped; the rest of the cycle survives.
    assert set(pages) == {"1", "3"}


def test_expired_session_mid_sweep_propagates():
    def handler(request):
        return httpx.Response(302, headers={"location": "/Account/Login"})

    with build(handler) as client, pytest.raises(SessionExpired):
        client.get_cardholders_html(["1", "2"])


def test_empty_id_list_is_a_no_op():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("should not issue a request")

    with build(handler) as client:
        assert client.get_cardholders_html([]) == {}


# --- connection test -----------------------------------------------------


def test_test_connection_reports_expiry_message():
    def handler(request):
        return httpx.Response(302, headers={"location": "/Account/Login"})

    with build(handler) as client:
        ok, message = client.test_connection()
    assert ok is False
    assert "expired" in message.lower()
