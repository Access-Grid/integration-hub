"""Avigilon Unity: a Seos pass needs an email address.

AccessGrid issues a Seos pass only with an email. A holder Unity has no
usable address for gets a synthetic one, first.last.<identity id>@domain,
when setup gave a domain; without one, their enrollment is held and the
operator is mailed rather than a pass issued that cannot be. DESFire passes
need an email or a phone, as before, and nothing is made up for them.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agsync.lib.pacs import build_adapter, get_descriptor
from agsync.routes import settings as settings_routes
from agsync.settings_store import PacsConfig
from agsync.sync import tracking
from tests.test_avigilon_unity import FakeUnity, _client, _token


def _identity_with(email=None):
    attrs = {"plasecFname": "Jim", "plasecLname": "Cooper"}
    if email is not None:
        attrs["plasecidentityEmailaddress"] = email
    return {"id": "a", "attributes": attrs}


def _read(email=None, seos=True, email_domain="", tracked=()):
    """Unity holding identity a with enrollment t1; returns (person, credential)."""
    fake = FakeUnity([{"cn": "a", "plasecName": "Cooper, Jim", "plasecIdstatus": "Active"}],
                     tokens={"a": [_token("t1")]}, details={"a": _identity_with(email)})
    a = build_adapter("avigilon", {"host": "unity", "username": "u", "password": "p",
                                   "mode": "seos" if seos else "desfire", "email_domain": email_domain})
    a._client = _client(fake)
    [person] = list(a.list_people())
    [cred] = a.list_credentials("a")
    return person, cred


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr("agsync.notifications.notify_card_problem", lambda key, problem: sent.append(problem))
    return sent


@pytest.fixture(autouse=True)
def untracked(monkeypatch):
    rows: list = []
    monkeypatch.setattr(tracking, "all_tracked", lambda: rows)
    return rows


def test_a_seos_holder_with_an_email_is_issued_to_it(mailed):
    person, cred = _read(email="jim@x.test")
    assert person.email == "jim@x.test" and not cred.hold and mailed == []


def test_a_seos_holder_with_no_email_and_no_domain_is_held_and_mailed(mailed):
    person, cred = _read()
    assert person.email == "" and cred.hold
    assert len(mailed) == 1 and "no email address" in mailed[0] and "Jim Cooper" in mailed[0]


def test_a_seos_holder_with_no_email_gets_a_synthetic_one_when_setup_gave_a_domain(mailed):
    person, cred = _read(email_domain="@cards.example.com")
    assert person.email == "jim.cooper.a@cards.example.com"
    assert not cred.hold and mailed == []


def test_an_email_field_holding_something_else_counts_as_none(mailed, caplog):
    with caplog.at_level(logging.WARNING):
        person, cred = _read(email="call the front desk", email_domain="cards.example.com")
    assert person.email == "jim.cooper.a@cards.example.com" and not cred.hold
    assert "is not an address" in caplog.text


def test_a_seos_pass_already_issued_is_not_held_for_a_missing_email(mailed, untracked):
    untracked.append(SimpleNamespace(pacs_person_id="a", pacs_credential_id="t1", ag_card_id="pass", status="active",
                                     last_synced_full_name="", last_synced_email="", last_synced_phone="",
                                     last_synced_title=""))
    _, cred = _read()
    assert not cred.hold and mailed == []


def test_a_desfire_holder_gets_no_synthetic_email_and_is_not_held(mailed):
    person, cred = _read(seos=False, email_domain="cards.example.com")
    assert person.email == "" and not cred.hold and mailed == []


def test_setup_asks_unity_for_both_optional():
    fields = {f.id: f for f in get_descriptor("avigilon").connection_fields}
    assert not fields["email_domain"].required and not fields["notify_email"].required


# --- settings, for an install set up before these fields existed ---------------


@pytest.fixture
def store(monkeypatch):
    blob = {PacsConfig.KEY: {"vendor": "avigilon", "params": {"host": "unity"}, "options": {}}}
    monkeypatch.setattr("agsync.settings_store._get_encrypted_json", lambda key: blob.get(key))
    monkeypatch.setattr("agsync.settings_store._set_encrypted_json", lambda key, payload: blob.__setitem__(key, payload))

    class _Engine:
        def invalidate_pacs_adapter(self):
            pass

        def trigger_now(self):
            pass

    monkeypatch.setattr(settings_routes, "get_engine", lambda: _Engine())
    return blob


def _params(store):
    return store[PacsConfig.KEY]["params"]


def test_unity_email_domain_can_be_set_and_cleared(store):
    resp = settings_routes.update_email_domain(None, email_domain="@cards.example.com", _user=None)
    assert "ok=email_domain" in resp.headers["location"] and _params(store)["email_domain"] == "cards.example.com"
    resp = settings_routes.update_email_domain(None, email_domain="  ", _user=None)
    assert "ok=email_domain" in resp.headers["location"] and _params(store)["email_domain"] == ""


def test_millennium_email_domain_still_cannot_be_cleared(store):
    store[PacsConfig.KEY]["vendor"] = "millennium_ultra"
    resp = settings_routes.update_email_domain(None, email_domain="", _user=None)
    assert "err=email_domain" in resp.headers["location"]


def test_notify_email_is_saved_checked_and_clearable(store):
    resp = settings_routes.update_notify_email(None, notify_email="security@example.com", _user=None)
    assert "ok=notify_email" in resp.headers["location"] and _params(store)["notify_email"] == "security@example.com"
    resp = settings_routes.update_notify_email(None, notify_email="security desk", _user=None)
    assert "err=notify_email" in resp.headers["location"] and _params(store)["notify_email"] == "security@example.com"
    settings_routes.update_notify_email(None, notify_email="", _user=None)
    assert _params(store)["notify_email"] == ""


def test_unity_alerts_go_to_the_notify_email(store, monkeypatch):
    from agsync import notifications

    _params(store)["notify_email"] = "security@example.com"
    sent = []
    monkeypatch.setattr(notifications, "_should_send", lambda key: True)
    monkeypatch.setattr(notifications, "send_mail", lambda to, subject, body: sent.append(to) or True)
    assert notifications.notify_card_problem("a:t1:email", "no email")
    assert sent == ["security@example.com"]
