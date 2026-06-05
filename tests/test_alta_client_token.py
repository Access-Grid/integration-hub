"""Token-validity / expiry-skew logic (pure, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agsync.lib.pacs.avigilon_alta.client import TOKEN_EXPIRY_SKEW, AltaClient


def _client() -> AltaClient:
    # __init__ builds an httpx.Client but makes no network call.
    return AltaClient(email="x@y.com", password="pw")


def test_no_token_is_invalid():
    c = _client()
    assert c._token_valid() is False
    c.close()


def test_token_without_expiry_is_valid():
    c = _client()
    c._token = "abc"
    c._token_expires_at = None
    assert c._token_valid() is True
    c.close()


def test_token_well_before_expiry_is_valid():
    c = _client()
    c._token = "abc"
    c._token_expires_at = datetime.now(UTC) + timedelta(hours=1)
    assert c._token_valid() is True
    c.close()


def test_token_within_skew_window_is_invalid():
    c = _client()
    c._token = "abc"
    # Inside the skew window — treated as already expired so we re-login early.
    c._token_expires_at = datetime.now(UTC) + (TOKEN_EXPIRY_SKEW / 2)
    assert c._token_valid() is False
    c.close()


def test_expired_token_is_invalid():
    c = _client()
    c._token = "abc"
    c._token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    assert c._token_valid() is False
    c.close()
