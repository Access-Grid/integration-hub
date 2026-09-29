"""Changing the AccessGrid account, template or API key after setup.

These three were fixed by the wizard: /settings showed the account and
template as plain text and never showed the key at all, so rotating a key
or correcting a template meant patching the encrypted settings blob by
hand. That is ordinary operational work to be asking of somebody.

Driven through the route function rather than over HTTP, which is how the
rest of this suite treats routes.
"""

from __future__ import annotations

import pytest

from agsync.routes import settings as settings_routes
from agsync.settings_store import AccessGridConfig

STORED = {
    "account_id": "acct-old",
    "api_secret": "secret-old",
    "template_id": "tpl-old",
    "site_code": "99",
    "dedupe_by_site_card": True,
    "extra_metadata": {"building": "south"},
    "card_title": "ICON South Beach",
    "card_classification": "Resident",
}


@pytest.fixture
def store(monkeypatch):
    """The encrypted settings blob, in memory."""
    blob: dict[str, dict] = {AccessGridConfig.KEY: dict(STORED)}
    monkeypatch.setattr(
        "agsync.settings_store._get_encrypted_json", lambda key: blob.get(key),
    )
    monkeypatch.setattr(
        "agsync.settings_store._set_encrypted_json",
        lambda key, payload: blob.__setitem__(key, payload),
    )
    return blob


@pytest.fixture
def engine(monkeypatch):
    """The engine caches the config, so it has to be told."""
    calls: list[str] = []

    class _Engine:
        def invalidate_pacs_adapter(self):
            calls.append("invalidate")

        def trigger_now(self):
            calls.append("trigger")

    monkeypatch.setattr(settings_routes, "get_engine", lambda: _Engine())
    return calls


def _accepts(monkeypatch, seen=None):
    def _test(account_id, api_secret, template_id):
        if seen is not None:
            seen.append((account_id, api_secret, template_id))
        return True, "ok"

    monkeypatch.setattr(settings_routes, "ag_test", _test)


def _save(**form):
    return settings_routes.update_accessgrid(
        request=None,
        account_id=form.get("account_id", "acct-new"),
        template_id=form.get("template_id", "tpl-new"),
        api_secret=form.get("api_secret", ""),
    )


def test_the_account_and_template_can_be_changed(store, engine, monkeypatch):
    _accepts(monkeypatch)
    assert _save().headers["location"] == "/settings?ok=accessgrid"
    saved = store[AccessGridConfig.KEY]
    assert (saved["account_id"], saved["template_id"]) == ("acct-new", "tpl-new")


def test_a_blank_key_keeps_the_stored_one(store, engine, monkeypatch):
    """So an account or template can be corrected without fetching a key
    nobody is changing — and the page never has to render the secret back
    to the operator in order to submit it."""
    _accepts(monkeypatch)
    _save(api_secret="")
    assert store[AccessGridConfig.KEY]["api_secret"] == "secret-old"


def test_a_supplied_key_replaces_it(store, engine, monkeypatch):
    _accepts(monkeypatch)
    _save(api_secret="secret-new")
    assert store[AccessGridConfig.KEY]["api_secret"] == "secret-new"


def test_everything_else_is_left_alone(store, engine, monkeypatch):
    """Site code, dedupe, metadata and card fields describe the deployment
    rather than the credentials."""
    _accepts(monkeypatch)
    _save()
    saved = store[AccessGridConfig.KEY]
    assert saved["site_code"] == "99"
    assert saved["dedupe_by_site_card"] is True
    assert saved["extra_metadata"] == {"building": "south"}
    assert saved["card_title"] == "ICON South Beach"
    assert saved["card_classification"] == "Resident"


def test_the_stored_key_is_what_gets_verified_when_none_is_given(
    store, engine, monkeypatch
):
    seen: list[tuple] = []
    _accepts(monkeypatch, seen)
    _save(api_secret="")
    assert seen == [("acct-new", "secret-old", "tpl-new")]


def test_credentials_accessgrid_refuses_are_not_saved(store, engine, monkeypatch):
    """The trap this route exists to remove, not to reproduce.

    Storing a key that cannot read the card template stops every phase at
    once — and with no way back through this page, since the engine could
    not read the template it needs to know which direction to run in.
    """
    monkeypatch.setattr(
        settings_routes, "ag_test",
        lambda *a: (False, "API key does not have permission: Can manage templates"),
    )
    assert _save(api_secret="bad").headers["location"] == (
        "/settings?err=accessgrid_rejected"
    )
    saved = store[AccessGridConfig.KEY]
    assert saved["account_id"] == "acct-old"
    assert saved["api_secret"] == "secret-old"
    assert saved["template_id"] == "tpl-old"


def test_a_rejection_does_not_disturb_the_engine(store, engine, monkeypatch):
    monkeypatch.setattr(settings_routes, "ag_test", lambda *a: (False, "nope"))
    _save()
    assert engine == []


def test_a_successful_change_rebuilds_the_adapter_and_runs(store, engine, monkeypatch):
    """The engine caches the config and the adapter built from it, so a
    change that never reaches it would apply on the next restart only."""
    _accepts(monkeypatch)
    _save()
    assert engine == ["invalidate", "trigger"]


@pytest.mark.parametrize("field", ["account_id", "template_id"])
def test_a_blank_required_field_is_refused(store, engine, monkeypatch, field):
    _accepts(monkeypatch)
    assert _save(**{field: "   "}).headers["location"] == "/settings?err=accessgrid"
    assert store[AccessGridConfig.KEY]["account_id"] == "acct-old"


def test_an_unconfigured_install_is_refused(monkeypatch, engine):
    """Nothing to update — that is the wizard's job."""
    monkeypatch.setattr("agsync.settings_store._get_encrypted_json", lambda key: None)
    _accepts(monkeypatch)
    assert _save().headers["location"] == "/settings?err=not_configured"
