"""Avigilon Unity: the site's card format is picked in setup.

A Unity token names no card format — a live box's tokens carry only their
numbers, status and dates — so the format, and with it the facility code
sent as every DESFire pass's site code, is chosen once from Unity's list.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agsync.lib.pacs.avigilon import wiegand
from agsync.lib.pacs.avigilon.adapter import AvigilonAdapter
from agsync.routes import wizard
from agsync.settings_store import PacsConfig
from tests.test_avigilon_unity import FakeUnity, _client, _identity, _token


def _format(cn, name, fc, bits, fc_at, card_at, even, odd):
    """A card format as Unity's XML lists it; the 26-bit one as a live box sent it."""
    return {"cn": cn, "plasecName": name, "plaseccfmtFacilitycode": fc, "plaseccfmtMaxdigits": bits,
            "plaseccfmtFcodeloc": fc_at[0], "plaseccfmtFcodelen": fc_at[1],
            "plaseccfmtCardloc": card_at[0], "plaseccfmtCardlen": card_at[1],
            "plaseccfmtEvenparityloc": even[0], "plaseccfmtEvenparitylen": even[1],
            "plaseccfmtOddparityloc": odd[0], "plaseccfmtOddparitylen": odd[1],
            "plaseccfmtIssuelen": "0", "plaseccfmtIssueloc": "0", "plaseccfmtOffset": "0", "plaseccfmtFlags": "0",
            "plaseccfmtType": "1", "plaseccfmtMindigits": "0"}


H10301 = _format("0", "HID 26 Bit", "239", "26", ("1", "8"), ("9", "16"), ("0", "13"), ("13", "13"))
H10304 = _format("2", "HID 37 FC", "1234", "37", ("1", "16"), ("17", "19"), ("0", "19"), ("18", "19"))
H10302 = _format("3", "HID 37 no FC", "", "37", ("0", "0"), ("1", "35"), ("0", "19"), ("18", "19"))
# Corporate 1000's parity is not two contiguous fields: not one we encode.
CORP = _format("4", "Corporate 1000", "12", "35", ("2", "12"), ("14", "20"), ("1", "17"), ("17", "18"))
H10301_LAYOUT = {"total_bits": 26, "fc_loc": 1, "fc_len": 8, "card_loc": 9, "card_len": 16, "even_loc": 0,
                 "even_len": 13, "odd_loc": 13, "odd_len": 13, "issue_len": 0, "offset": 0, "flags": 0}


def _adapter(fake, **params) -> AvigilonAdapter:
    a = AvigilonAdapter("unity", "admin", "pw", **params)
    a._client = _client(fake)
    return a


def _fake():
    fake = FakeUnity([_identity("a")], tokens={"a": [_token("t1")]}, details={"a": {"id": "a", "attributes": {}}})
    fake.card_formats = [H10301, CORP, H10304, H10302]
    return fake


def test_formats_are_read_as_xml_when_json_is_refused():
    a = _adapter(_fake())
    assert a.card_formats()[:2] == [("0", "HID 26 Bit (FC 239, 26-bit)"), ("4", "Corporate 1000 (FC 12, 35-bit)")]


def test_choosing_a_format_stores_its_facility_code_and_length():
    params = _adapter(_fake()).card_format_params({"host": "unity"}, " 0 ")
    assert params == {"host": "unity", "card_format": "0", "card_format_label": "HID 26 Bit (FC 239, 26-bit)",
                      "facility_code": "239", "card_format_bits": "26", "card_format_layout": H10301_LAYOUT}


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


def test_a_format_this_sync_cannot_encode_is_named_at_start(caplog):
    with caplog.at_level(logging.INFO):
        _adapter(_fake(), **_params(_fake(), "4"))
    assert "Corporate 1000" in caplog.text and "enrollments will be held" in caplog.text


def _params(fake, format_id):
    return _adapter(fake).card_format_params({}, format_id)


@pytest.fixture
def mailed(monkeypatch):
    sent = []
    monkeypatch.setattr("agsync.notifications.notify_card_problem", lambda key, problem: sent.append(problem))
    return sent


def _enrollment(format_id, internal="42069", **params):
    fake = _fake()
    fake.tokens["a"] = [_token("t1", internal=internal)]
    a = _adapter(fake, **{**_params(fake, format_id), **params})
    list(a.list_people())
    [cred] = a.list_credentials("a")
    return cred


def test_a_26_bit_card_goes_as_site_code_and_card_number(mailed):
    cred = _enrollment("0")
    assert (cred.site_code, cred.card_number, cred.file_data, cred.send_file_data, cred.hold) == \
        ("239", "42069", "", False, False)


def test_a_26_bit_card_number_too_big_for_its_16_bits_is_held_and_mailed(mailed):
    cred = _enrollment("0", internal="65536")
    assert cred.hold and not cred.file_data
    assert "65536 does not fit its 16 bits" in mailed[0]


def test_an_h10304_card_goes_as_file_data(mailed):
    cred = _enrollment("2", internal="42069")
    assert cred.send_file_data and not cred.hold and mailed == []
    assert cred.file_data == wiegand.encode(wiegand.STANDARDS["H10304"], "1234", "42069")


def test_an_h10302_format_needs_no_facility_code(mailed):
    params = _params(_fake(), "3")
    assert params["facility_code"] == ""
    cred = _enrollment("3", internal="12345678901")
    assert cred.send_file_data and not cred.hold
    assert cred.file_data == wiegand.encode(wiegand.STANDARDS["H10302"], "", "12345678901")


def test_a_card_in_a_format_this_sync_cannot_encode_is_held_and_mailed(mailed):
    cred = _enrollment("4")
    assert cred.hold and not cred.file_data
    assert "Corporate 1000" in mailed[0] and "not issuing a pass" in mailed[0]


def test_a_pass_already_issued_is_not_held_for_its_format(mailed, monkeypatch):
    from types import SimpleNamespace

    from agsync.sync import tracking

    row = SimpleNamespace(pacs_person_id="a", pacs_credential_id="t1", ag_card_id="pass", status="active",
                          last_synced_full_name="", last_synced_email="", last_synced_phone="", last_synced_title="")
    monkeypatch.setattr(tracking, "all_tracked", lambda: [row])
    cred = _enrollment("4")
    assert not cred.hold and mailed == []


def test_a_format_chosen_before_its_layout_was_stored_is_read_on_the_first_cycle(mailed):
    cred = _enrollment("2", card_format_layout=None)
    assert cred.send_file_data and not cred.hold


def test_a_layout_unity_will_not_give_holds_enrollments_without_mail(mailed):
    fake = _fake()
    params = {k: v for k, v in _params(fake, "2").items() if k != "card_format_layout"}
    fake.fail["/card_formats.xml"] = 500
    a = _adapter(fake, **params)
    list(a.list_people())
    [cred] = a.list_credentials("a")
    assert cred.hold and mailed == []


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
