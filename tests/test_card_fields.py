"""Pass details: start date, title and classification.

`start_date` now comes from the card's own validity in the PACS rather than
"whenever the sync noticed it". Title and classification describe the
deployment, not the person, so they are configured once — but a PACS that
does carry a per-person title still wins.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import Credential, CredentialStatus, Person
from agsync.lib.pacs.millennium_ultra.adapter import format_datetime, parse_datetime
from agsync.sync.phases import phase1_provision
from agsync.sync.snapshot import Snapshot


class FakeCards:
    def __init__(self):
        self.calls: list[dict] = []

    def provision(self, **params):
        self.calls.append(params)
        return SimpleNamespace(id="card-1", state="active")


class FakeAG:
    def __init__(self):
        self.access_cards = FakeCards()


@pytest.fixture
def stub_tracking(monkeypatch):
    t = phase1_provision.tracking
    monkeypatch.setattr(t, "get", lambda *a, **k: None)
    monkeypatch.setattr(t, "upsert", lambda *a, **k: None)
    monkeypatch.setattr(t, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(t, "mark_deduped", lambda *a, **k: None)
    monkeypatch.setattr(t, "update_last_known_ag_state", lambda *a, **k: None)


def _snapshot(cred: Credential, title: str = "") -> Snapshot:
    snap = Snapshot()
    snap.people["p1"] = Person(
        id="p1", full_name="Test User", email="t@e.com", title=title, active=True,
    )
    snap.credentials_by_person["p1"] = [cred]
    return snap


def _cred(**kwargs) -> Credential:
    base = dict(
        id="slot1", person_id="p1", card_number="1234", site_code="66",
        status=CredentialStatus.ACTIVE, trigger_active=True,
    )
    base.update(kwargs)
    return Credential(**base)


# --- dates ---------------------------------------------------------------


def test_millennium_dates_round_trip_through_the_install_offset():
    # -14400 is the offset the browser reported at sign-in; the stored text
    # is local to it, so a naive parse would be four hours out.
    text = "08/18/2026 12:00 AM"
    parsed = parse_datetime(text, -14400)
    assert parsed == datetime(2026, 8, 18, 4, 0, tzinfo=UTC)
    assert format_datetime(parsed, -14400) == text


@pytest.mark.parametrize("value", ["", "   ", "not a date", "13/45/2026"])
def test_unusable_dates_become_none(value):
    # Better to fall back to a sensible default than to provision a pass
    # whose validity is nonsense.
    assert parse_datetime(value, -14400) is None


def test_the_cards_own_validity_reaches_accessgrid(stub_tracking):
    starts = datetime(2026, 8, 18, 4, tzinfo=UTC)
    ends = datetime(2028, 8, 17, 4, tzinfo=UTC)
    ag = FakeAG()
    phase1_provision.run(
        _snapshot(_cred(activate_date=starts, deactivate_date=ends)), ag, "tpl",
    )
    params = ag.access_cards.calls[0]
    assert params["start_date"] == starts.isoformat()
    assert params["expiration_date"] == ends.isoformat()


def test_a_credential_with_no_dates_still_provisions(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(_snapshot(_cred()), ag, "tpl")
    params = ag.access_cards.calls[0]
    assert params["start_date"] and params["expiration_date"]


# --- title and classification -------------------------------------------


def test_configured_title_and_classification_are_sent(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(
        _snapshot(_cred()), ag, "tpl",
        card_title="Resident", card_classification="Resident",
    )
    params = ag.access_cards.calls[0]
    assert params["title"] == "Resident"
    assert params["classification"] == "Resident"


def test_a_per_person_title_from_the_pacs_wins(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(
        _snapshot(_cred(), title="Building Manager"), ag, "tpl", card_title="Resident",
    )
    assert ag.access_cards.calls[0]["title"] == "Building Manager"


def test_nothing_is_sent_when_nothing_is_configured(stub_tracking):
    # An empty setting must not become an empty title on the pass.
    ag = FakeAG()
    phase1_provision.run(_snapshot(_cred()), ag, "tpl")
    params = ag.access_cards.calls[0]
    assert "title" not in params
    assert "classification" not in params


# --- editing settings must not drop what it does not know about ---------


def test_updating_params_merges_rather_than_replaces(monkeypatch):
    """The settings forms know about one field each.

    The params dict also holds the enrollment trigger, written by the connect
    flow. A form that replaced the dict would silently un-enroll everybody.
    """
    from agsync import settings_store

    store: dict = {
        "pacs": {"vendor": "millennium_ultra",
                 "params": {"base_url": "https://m.test",
                            "email_domain": "old.test",
                            "trigger_card_format": "8"},
                 "options": {}}
    }
    monkeypatch.setattr(settings_store, "_get_encrypted_json", lambda k: store.get(k))
    monkeypatch.setattr(
        settings_store, "_set_encrypted_json", lambda k, v: store.__setitem__(k, v),
    )

    assert settings_store.PacsConfig.update_params(email_domain="new.test") is True
    params = store["pacs"]["params"]
    assert params["email_domain"] == "new.test"
    assert params["trigger_card_format"] == "8"
    assert params["base_url"] == "https://m.test"


def test_updating_params_before_setup_is_a_no_op(monkeypatch):
    from agsync import settings_store

    monkeypatch.setattr(settings_store, "_get_encrypted_json", lambda k: None)
    assert settings_store.PacsConfig.update_params(email_domain="x.test") is False
