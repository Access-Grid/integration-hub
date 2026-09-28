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


# --- refusing a session that does not work ------------------------------


def _adapter():
    """A Millennium adapter with no session of its own to validate against."""
    from agsync.lib.pacs.millennium_ultra.adapter import MillenniumUltraAdapter

    return MillenniumUltraAdapter(base_url="https://pacs.test", auth_cookie="x")


def _session():
    return {"auth_cookie": "cookie", "base_url": "https://pacs.test"}


def test_a_session_that_reads_nothing_is_rejected(monkeypatch):
    """A cookie can authenticate and still return nothing.

    A sign-in captured mid-flow does exactly that, and the failure is
    silent afterwards — an empty roster reads as a PACS with no people.
    """
    class Blank:
        def __init__(self, **kwargs):
            pass

        def first_cardholder_id(self):
            return ""

        def close(self):
            pass

    # Built before patching: the adapter makes a client of its own at
    # construction, which is not the one under test here.
    adapter = _adapter()
    monkeypatch.setattr(
        "agsync.lib.pacs.millennium_ultra.adapter.MillenniumUltraClient", Blank,
    )
    ok, detail = adapter.validate_session(_session())
    assert ok is False
    assert "no cardholders" in detail


def test_a_working_session_is_accepted(monkeypatch):
    class Working:
        def __init__(self, **kwargs):
            pass

        def first_cardholder_id(self):
            return "11587"

        def close(self):
            pass

    # Built before patching: the adapter makes a client of its own at
    # construction, which is not the one under test here.
    adapter = _adapter()
    monkeypatch.setattr(
        "agsync.lib.pacs.millennium_ultra.adapter.MillenniumUltraClient", Working,
    )
    assert adapter.validate_session(_session()) == (True, "")


def test_an_unreachable_pacs_is_reported_not_stored(monkeypatch):
    class Broken:
        def __init__(self, **kwargs):
            raise RuntimeError("connection refused")

    # Built before patching: the adapter makes a client of its own at
    # construction, which is not the one under test here.
    adapter = _adapter()
    monkeypatch.setattr(
        "agsync.lib.pacs.millennium_ultra.adapter.MillenniumUltraClient", Broken,
    )
    ok, detail = adapter.validate_session(_session())
    assert ok is False
    assert "connection refused" in detail


def test_the_cookie_jar_becomes_the_stored_session():
    """Core captures cookies without knowing what they mean.

    Turning them into a session is the adapter's job — including carrying
    `timeoffset`, which Millennium's date fields are parsed against.
    """
    session = _adapter().session_from_cookies({
        ".AspNet.UltraAuth": "auth-value",
        "UltraCompanyName": "Acme",
        "timeoffset": "-240",
        "irrelevant": "ignored",
    })
    assert session["auth_cookie"] == "auth-value"
    assert session["company_name"] == "Acme"
    assert session["time_offset"] == "-240"
    assert "irrelevant" not in session


def test_the_connect_package_names_no_vendor():
    """The point of the refactor, kept honest.

    Cookie names used to sit in connect/protocol.py as constants and in
    capture.py as default arguments, so a module that looked generic
    answered for exactly one PACS. Anything vendor-specific belongs on the
    descriptor now.
    """
    import pathlib

    import agsync.connect as pkg

    root = pathlib.Path(pkg.__file__).parent
    offenders = [
        path.name
        for path in root.glob("*.py")
        if "millennium" in path.read_text().lower()
    ]
    assert offenders == []


def test_capture_requires_being_told_which_cookie():
    """No default: a wrong guess waits for a cookie that never arrives and
    times out looking like a failed sign-in."""
    import inspect

    from agsync.connect.capture import capture_session

    assert inspect.signature(capture_session).parameters["cookie_name"].default \
        is inspect.Parameter.empty
