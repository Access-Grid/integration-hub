"""Avigilon Unity: the site's card format is picked in setup.

A Unity token names no card format — a live box's tokens carry only their
numbers, status and dates — so the format, and with it the facility code
sent as every DESFire pass's site code, is chosen once from Unity's list.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agsync.lib.pacs.avigilon.adapter import AvigilonAdapter
from agsync.routes import wizard
from agsync.settings_store import PacsConfig
from tests.test_avigilon_unity import FakeUnity, _client, _identity, _token

H10301 = {"cn": "0", "plasecName": "HID 26 Bit", "plaseccfmtFacilitycode": "239", "plaseccfmtMaxdigits": "26"}
CORP = {"cn": "4", "plasecName": "Corporate 1000", "plaseccfmtFacilitycode": "12", "plaseccfmtMaxdigits": "35"}


def _adapter(fake, **params) -> AvigilonAdapter:
    a = AvigilonAdapter("unity", "admin", "pw", **params)
    a._client = _client(fake)
    return a


def _fake():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]}, details={"a": {"id": "a", "attributes": {}}})
    fake.card_formats = [H10301, CORP]
    return fake


def test_formats_are_read_as_xml_when_json_is_refused():
    a = _adapter(_fake())
    assert a.card_formats() == [("0", "HID 26 Bit (FC 239, 26-bit)"), ("4", "Corporate 1000 (FC 12, 35-bit)")]


def test_choosing_a_format_stores_its_facility_code_and_length():
    params = _adapter(_fake()).card_format_params({"host": "unity"}, " 0 ")
    assert params == {"host": "unity", "card_format": "0", "card_format_label": "HID 26 Bit (FC 239, 26-bit)",
                      "facility_code": "239", "card_format_bits": "26"}


def test_a_format_unity_does_not_define_is_refused():
    with pytest.raises(ValueError, match="not defined"):
        _adapter(_fake()).card_format_params({}, "99")


def test_a_format_with_no_facility_code_is_refused():
    fake = _fake()
    fake.card_formats = [{**H10301, "plaseccfmtFacilitycode": ""}]
    with pytest.raises(ValueError, match="no facility code"):
        _adapter(fake).card_format_params({}, "0")


def test_the_formats_facility_code_is_each_cards_site_code():
    a = _adapter(_fake(), card_format="0", facility_code="239", card_format_bits="26")
    list(a.list_people())
    [cred] = a.list_credentials("a")
    assert (cred.site_code, cred.card_number) == ("239", "42069")


def test_with_no_format_chosen_the_site_code_from_settings_stands():
    a = _adapter(_fake())
    list(a.list_people())
    [cred] = a.list_credentials("a")
    assert cred.site_code == ""


def test_a_format_that_is_not_26_bit_is_warned_about(caplog):
    with caplog.at_level(logging.WARNING):
        _adapter(_fake(), card_format="4", card_format_label="Corporate 1000", facility_code="12",
                 card_format_bits="35")
    assert "Corporate 1000 is 35-bit" in caplog.text


# --- the wizard ----------------------------------------------------------------


@pytest.fixture
def store(monkeypatch):
    blob: dict[str, dict] = {}
    monkeypatch.setattr("agsync.settings_store._get_encrypted_json", lambda key: blob.get(key))
    monkeypatch.setattr("agsync.settings_store._set_encrypted_json", lambda key, payload: blob.__setitem__(key, payload))
    monkeypatch.setattr(wizard, "_require_admin_if_bootstrapped", lambda request: None)
    started = []

    class _Engine:
        def invalidate_pacs_adapter(self):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(wizard, "get_engine", lambda: _Engine())
    monkeypatch.setattr(wizard, "is_configured", lambda: True)
    fake = _fake()
    monkeypatch.setattr(wizard, "build_adapter", lambda vendor, params: _adapter(
        fake, **{k: v for k, v in params.items() if k not in ("host", "username", "password")}))
    return blob, started


class _Form(dict):
    async def form(self):
        return self


def test_saving_unity_credentials_goes_to_the_format_step_without_starting(store):
    blob, started = store
    resp = asyncio.run(wizard.wizard_pacs(_Form(vendor="avigilon", host="unity", username="u", password="p")))
    assert resp.headers["location"] == "/wizard/card-format"
    assert started == []
    assert wizard._needs_card_format(PacsConfig.load())


def test_choosing_the_format_saves_it_and_starts(store):
    blob, started = store
    PacsConfig.save("avigilon", {"host": "unity", "username": "u", "password": "p"}, {})
    resp = wizard.wizard_card_format_save(None, card_format="0")
    assert resp.headers["location"] == "/wizard"
    assert PacsConfig.load()["params"]["facility_code"] == "239"
    assert started == [True]
    assert not wizard._needs_card_format(PacsConfig.load())


def test_an_unknown_format_goes_back_to_the_picker(store):
    blob, started = store
    PacsConfig.save("avigilon", {"host": "unity", "username": "u", "password": "p"}, {})
    resp = wizard.wizard_card_format_save(None, card_format="99")
    assert resp.headers["location"] == "/wizard/card-format"
    assert "card_format" not in PacsConfig.load()["params"] and started == []
