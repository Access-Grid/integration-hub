"""AG Connect: the session hand-off, and the properties that keep it safe.

The payload is a live PACS session travelling between two processes, so the
things worth asserting are the negative ones — a launch can only be claimed
once, a stale launch yields nothing, and a payload sealed for one launch is
undecryptable with another's key.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agsync.connect.protocol import build_launch_uri, parse_launch_uri, seal, unseal
from agsync.routes.connect import LAUNCH_TTL_S, _LaunchRegistry


@pytest.fixture
def key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


# --- the URI -------------------------------------------------------------


def test_launch_uri_round_trips():
    uri = build_launch_uri("abc123", "https://127.0.0.1:5355")
    assert uri.startswith("agconnect://")
    assert parse_launch_uri(uri) == ("abc123", "https://127.0.0.1:5355")


def test_launch_uri_carries_no_key():
    # The URI passes through the OS; only the launch id may travel on it.
    uri = build_launch_uri("abc123", "https://127.0.0.1:5355")
    assert "key" not in uri


def test_foreign_scheme_is_rejected():
    with pytest.raises(ValueError):
        parse_launch_uri("https://evil.example.com/connect?launch=x&server=y")


def test_incomplete_uri_is_rejected():
    with pytest.raises(ValueError):
        parse_launch_uri("agconnect://v1/connect?launch=x")


# --- the sealed payload --------------------------------------------------


def test_payload_round_trips(key):
    payload = {"cookies": [{"name": ".AspNet.UltraAuth", "value": "secret"}]}
    assert unseal(key, seal(key, payload)) == payload


def test_another_launchs_key_cannot_open_it(key):
    other = AESGCM.generate_key(bit_length=256)
    sealed = seal(key, {"cookies": []})
    with pytest.raises(InvalidTag):
        unseal(other, sealed)


def test_ciphertext_does_not_leak_the_cookie(key):
    sealed = seal(key, {"cookies": [{"name": "x", "value": "super-secret-cookie"}]})
    assert "super-secret-cookie" not in sealed


def test_each_seal_differs(key):
    # Fresh nonce per seal, so two captures never produce the same blob.
    payload = {"cookies": [{"name": "x", "value": "y"}]}
    assert seal(key, payload) != seal(key, payload)


# --- the launch registry -------------------------------------------------


def test_launch_can_be_claimed_once():
    registry = _LaunchRegistry()
    launch = registry.create("https://pacs.test/Account/LogIn")
    assert registry.claim(launch.launch_id) is not None
    # A replayed link gets nothing — the key is already out.
    assert registry.claim(launch.launch_id) is None


def test_unknown_launch_is_not_claimable():
    assert _LaunchRegistry().claim("nope") is None


def test_expired_launch_is_dropped(monkeypatch):
    registry = _LaunchRegistry()
    launch = registry.create("https://pacs.test/Account/LogIn")
    # Age the launch past its TTL rather than the clock, so this stays a
    # test of the expiry rule and not of monkeypatching time.
    launch.created_at -= LAUNCH_TTL_S + 1
    assert registry.get(launch.launch_id) is None
    assert registry.claim(launch.launch_id) is None


def test_each_launch_gets_its_own_key():
    registry = _LaunchRegistry()
    first = registry.create("https://pacs.test/Account/LogIn")
    second = registry.create("https://pacs.test/Account/LogIn")
    assert first.key != second.key
    assert first.launch_id != second.launch_id
    assert len(first.key) == 32  # AES-256
