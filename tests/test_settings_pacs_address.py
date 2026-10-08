"""Changing where this install reaches the PACS, after setup.

The address was fixed by the wizard: a server that moved, or one mistyped
during setup, meant setting the integration up again from scratch. The
fields are read from the adapter's own descriptor rather than named here,
so a new PACS gets this for free.

Driven through the route function rather than over HTTP, which is how the
rest of this suite treats routes.
"""

from __future__ import annotations

import asyncio

import pytest

from agsync.lib.pacs import get_descriptor
from agsync.routes import settings as settings_routes
from agsync.settings_store import PacsConfig, PacsSession

VENDOR = "millennium_ultra"
STORED_PARAMS = {
    "base_url": "https://hosted8.mgiaccess.com",
    "email_domain": "cards.example.com",
    "notify_email": "security@example.com",
    "trigger_card_format": "7",
}


@pytest.fixture
def store(monkeypatch):
    """The encrypted settings blob, in memory."""
    blob: dict[str, dict] = {
        PacsConfig.KEY: {"vendor": VENDOR, "params": dict(STORED_PARAMS), "options": {}},
        f"pacs_session:{VENDOR}": {"auth_cookie": "cookie-for-the-old-host"},
    }
    monkeypatch.setattr(
        "agsync.settings_store._get_encrypted_json", lambda key: blob.get(key))
    monkeypatch.setattr(
        "agsync.settings_store._set_encrypted_json",
        lambda key, payload: blob.__setitem__(key, payload))
    monkeypatch.setattr(
        "agsync.settings_store.delete", lambda key: blob.pop(key, None))
    return blob


@pytest.fixture
def engine(monkeypatch):
    calls: list[str] = []

    class _Engine:
        def invalidate_pacs_adapter(self):
            calls.append("invalidate")

        def trigger_now(self):
            calls.append("trigger")

    monkeypatch.setattr(settings_routes, "get_engine", lambda: _Engine())
    return calls


class _Request:
    def __init__(self, form: dict):
        self._form = form

    async def form(self):
        return self._form


def _save(**form):
    return asyncio.run(
        settings_routes.update_pacs_address(request=_Request(form), _user=None)
    )


def _params(store) -> dict:
    return store[PacsConfig.KEY]["params"]


# =====================================================================
# Which fields appear, and which must never
# =====================================================================


def test_the_address_comes_from_the_adapters_own_descriptor():
    fields = settings_routes._address_fields(get_descriptor(VENDOR))

    assert [f.id for f in fields] == ["base_url"]


def test_no_adapters_credential_is_ever_offered_here():
    """The reason this is narrowed to url-kind rather than "not secret".

    Four of the shipped adapters take a password. The settings page renders
    saved values back into the form, so a password reaching this list would
    put a stored credential into the HTML.
    """
    import agsync.lib.pacs  # noqa: F401 — registers the adapters
    from agsync.lib.pacs.registry import available_pacs

    for descriptor in available_pacs():
        for field in settings_routes._address_fields(descriptor):
            assert field.kind == "url", f"{descriptor.vendor}.{field.id}"
            assert "password" not in field.id


def test_an_adapter_with_no_url_field_gets_no_section():
    """Avigilon's address is a bare host, so it does not appear yet."""
    assert settings_routes._address_fields(get_descriptor("avigilon")) == []


def test_an_unconfigured_install_has_nothing_to_show():
    assert settings_routes._address_fields(None) == []


# =====================================================================
# Saving one
# =====================================================================


def test_the_address_can_be_changed(store, engine):
    result = _save(base_url="https://hosted9.mgiaccess.com")

    assert result.headers["location"] == "/settings?ok=pacs_address"
    assert _params(store)["base_url"] == "https://hosted9.mgiaccess.com"


def test_the_other_settings_survive_it(store, engine):
    """update_params merges; the trigger in particular was written by the
    connect flow and this form has never heard of it."""
    _save(base_url="https://hosted9.mgiaccess.com")

    saved = _params(store)
    assert saved["trigger_card_format"] == "7"
    assert saved["email_domain"] == "cards.example.com"


def test_changing_it_signs_the_install_out(store, engine):
    """The cookie was issued by the old host.

    Left in place, requests to the new server would simply look
    unauthenticated — which reads downstream as an expired session rather
    than as a changed address, and sends the operator looking in the wrong
    place.
    """
    _save(base_url="https://hosted9.mgiaccess.com")

    assert PacsSession.load(VENDOR) is None


def test_the_cached_adapter_is_dropped(store, engine):
    """It holds the old address until it is rebuilt."""
    _save(base_url="https://hosted9.mgiaccess.com")

    assert "invalidate" in engine


def test_no_cycle_is_started(store, engine):
    """Unlike the other settings forms. There is nothing to sync against
    until a human has signed in again, so a cycle would only fail."""
    _save(base_url="https://hosted9.mgiaccess.com")

    assert "trigger" not in engine


# =====================================================================
# Normalising and refusing
# =====================================================================


@pytest.mark.parametrize(("given", "expected"), [
    ("https://hosted8.mgiaccess.com", "https://hosted8.mgiaccess.com"),
    # A trailing slash would make the login path "//Account/LogIn".
    ("https://hosted8.mgiaccess.com/", "https://hosted8.mgiaccess.com"),
    ("hosted8.mgiaccess.com", "https://hosted8.mgiaccess.com"),
    ("http://10.0.0.5:8080/", "http://10.0.0.5:8080"),
    ("  https://hosted8.mgiaccess.com  ", "https://hosted8.mgiaccess.com"),
    ("hosted8.mgiaccess.com:8080", "https://hosted8.mgiaccess.com:8080"),
    ("http://[::1]:5000/", "http://[::1]:5000"),
])
def test_an_address_is_normalised(store, engine, given, expected):
    _save(base_url=given)

    assert _params(store)["base_url"] == expected


@pytest.mark.parametrize("given", [
    "", "   ", "not a url", "ftp://hosted8.mgiaccess.com", "https://",
    # Both have no "://", so filling in the missing scheme would turn them
    # into a host with a nonsense port rather than refusing them.
    "javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
])
def test_a_bad_address_is_refused(store, engine, given):
    result = _save(base_url=given)

    assert result.headers["location"] == "/settings?err=pacs_address"
    assert _params(store)["base_url"] == STORED_PARAMS["base_url"], "left alone"
    assert PacsSession.load(VENDOR) is not None, "and still signed in"


def test_saving_the_same_address_costs_nothing(store, engine):
    """Submitting the form unchanged must not demand a reconnect — for a
    captcha-gated PACS that is a walk to the machine."""
    result = _save(base_url=STORED_PARAMS["base_url"])

    assert result.headers["location"] == "/settings?ok=pacs_address"
    assert PacsSession.load(VENDOR) is not None
    assert engine == []


def test_the_same_address_written_untidily_also_costs_nothing(store, engine):
    """It is the same server, however it was typed."""
    _save(base_url=STORED_PARAMS["base_url"] + "/")

    assert PacsSession.load(VENDOR) is not None


def test_an_unconfigured_install_cannot_set_one(monkeypatch, engine):
    monkeypatch.setattr(
        "agsync.settings_store._get_encrypted_json", lambda key: None)
    result = _save(base_url="https://x.example.com")

    assert result.headers["location"] == "/settings?err=not_configured"
