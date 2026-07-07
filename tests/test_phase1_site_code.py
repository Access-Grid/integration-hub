"""Phase 1 site_code precedence: per-credential value wins over the global
settings value, but a blank one falls back to it (the Avigilon Unity case).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agsync.lib.pacs.base import Credential, CredentialStatus, Person
from agsync.sync.phases import phase1_provision
from agsync.sync.snapshot import Snapshot


class FakeCards:
    def __init__(self):
        self.calls: list[dict] = []

    def provision(self, **params):
        self.calls.append(params)
        return SimpleNamespace(id="agcard-1", state="active")


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


def _snapshot(cred: Credential) -> Snapshot:
    snap = Snapshot()
    snap.people["p1"] = Person(id="p1", full_name="Test User", email="t@e.com", active=True)
    snap.credentials_by_person["p1"] = [cred]
    return snap


def _cred(site_code: str) -> Credential:
    return Credential(
        id="c1",
        person_id="p1",
        card_number="42069",
        site_code=site_code,
        status=CredentialStatus.ACTIVE,
        trigger_active=True,
    )


def test_per_credential_site_code_wins_over_global(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(_snapshot(_cred("71")), ag, "tpl", site_code="999")

    assert len(ag.access_cards.calls) == 1
    params = ag.access_cards.calls[0]
    assert params["site_code"] == 71                  # int, from the credential
    assert params["metadata"]["site_code"] == "71"    # not the global "999"


def test_blank_credential_site_code_falls_back_to_global(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(_snapshot(_cred("")), ag, "tpl", site_code="999")

    params = ag.access_cards.calls[0]
    assert params["site_code"] == 999
    assert params["metadata"]["site_code"] == "999"


# --- file_data encoding mode --------------------------------------------


def _cred_with_file_data(file_data: str) -> Credential:
    return Credential(
        id="c1",
        person_id="p1",
        card_number="228",
        site_code="99",
        file_data=file_data,
        status=CredentialStatus.ACTIVE,
        trigger_active=True,
    )


def test_file_data_mode_sends_blob_and_keeps_site_card_metadata(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(
        _snapshot(_cred_with_file_data("00000000004500E4")),
        ag, "tpl", site_code="", use_file_data=True,
    )
    params = ag.access_cards.calls[0]
    assert params["file_data"] == "00000000004500E4"
    # Wire identity is file_data — site_code/card_number are NOT sent...
    assert "site_code" not in params
    assert "card_number" not in params
    # ...but they remain in metadata for dedupe + debugging.
    assert params["metadata"]["site_code"] == "99"
    assert params["metadata"]["card_number"] == "228"


def test_file_data_mode_falls_back_when_credential_has_no_blob(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(
        _snapshot(_cred_with_file_data("")),
        ag, "tpl", site_code="", use_file_data=True,
    )
    params = ag.access_cards.calls[0]
    assert "file_data" not in params
    assert params["site_code"] == 99
    assert params["card_number"] == "228"


def test_default_mode_never_sends_file_data(stub_tracking):
    ag = FakeAG()
    phase1_provision.run(
        _snapshot(_cred_with_file_data("00000000004500E4")),
        ag, "tpl", site_code="",  # use_file_data defaults False
    )
    params = ag.access_cards.calls[0]
    assert "file_data" not in params
    assert params["site_code"] == 99
    assert params["card_number"] == "228"
