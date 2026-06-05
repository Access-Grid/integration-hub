"""Adapter mapping, trigger gating, status, and writeback tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from agsync.lib.pacs.base import CredentialStatus
from tests._fakes import FakeAltaClient


def _people_then_creds(adapter, person_id="39734661"):
    # list_people() populates the externalId cache the trigger relies on.
    list(adapter.list_people())
    return list(adapter.list_credentials(person_id))


# --- mapping -------------------------------------------------------------


def test_wiegand_card_maps_facility_to_site_and_cardid_to_number(
    make_adapter, sample_user, wiegand_cred
):
    client = FakeAltaClient(users=[sample_user], creds_by_user={"39734661": [wiegand_cred]})
    creds = _people_then_creds(make_adapter(client))

    assert len(creds) == 1
    c = creds[0]
    assert c.site_code == "69"        # card.fields.facilityCode
    assert c.card_number == "42069"   # card.fields.cardId, NOT the long encoded number
    assert c.id == "58943905"
    assert c.person_id == "39734661"


def test_non_wiegand_credentials_are_filtered_out(
    make_adapter, sample_user, wiegand_cred, mobile_cred
):
    client = FakeAltaClient(
        users=[sample_user],
        creds_by_user={"39734661": [mobile_cred, wiegand_cred, mobile_cred]},
    )
    creds = _people_then_creds(make_adapter(client))
    assert [c.id for c in creds] == ["58943905"]


# --- trigger gating ------------------------------------------------------


def test_trigger_active_when_external_id_is_accessgrid(
    make_adapter, sample_user, wiegand_cred
):
    client = FakeAltaClient(users=[sample_user], creds_by_user={"39734661": [wiegand_cred]})
    creds = _people_then_creds(make_adapter(client))
    assert creds[0].trigger_active is True


def test_trigger_is_case_insensitive(make_adapter, sample_user, wiegand_cred):
    user = copy.deepcopy(sample_user)
    user["externalId"] = "AccessGrid"
    client = FakeAltaClient(users=[user], creds_by_user={"39734661": [wiegand_cred]})
    creds = _people_then_creds(make_adapter(client))
    assert creds[0].trigger_active is True


def test_trigger_inactive_for_other_external_id(make_adapter, sample_user, wiegand_cred):
    user = copy.deepcopy(sample_user)
    user["externalId"] = "kingking"
    client = FakeAltaClient(users=[user], creds_by_user={"39734661": [wiegand_cred]})
    creds = _people_then_creds(make_adapter(client))
    assert creds[0].trigger_active is False


def test_trigger_inactive_for_empty_external_id(make_adapter, sample_user, wiegand_cred):
    user = copy.deepcopy(sample_user)
    user["externalId"] = None
    client = FakeAltaClient(users=[user], creds_by_user={"39734661": [wiegand_cred]})
    creds = _people_then_creds(make_adapter(client))
    assert creds[0].trigger_active is False


# --- status from dates ---------------------------------------------------


def test_status_active_when_enddate_in_future(make_adapter, sample_user, wiegand_cred):
    client = FakeAltaClient(users=[sample_user], creds_by_user={"39734661": [wiegand_cred]})
    creds = _people_then_creds(make_adapter(client))
    assert creds[0].status == CredentialStatus.ACTIVE


def test_status_suspended_when_enddate_in_past(make_adapter, sample_user, wiegand_cred, now):
    expired = copy.deepcopy(wiegand_cred)
    expired["endDate"] = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    client = FakeAltaClient(users=[sample_user], creds_by_user={"39734661": [expired]})
    creds = _people_then_creds(make_adapter(client))
    assert creds[0].status == CredentialStatus.SUSPENDED


def test_person_active_flag_from_status(make_adapter, sample_user, wiegand_cred):
    inactive = copy.deepcopy(sample_user)
    inactive["status"] = "I"
    client = FakeAltaClient(users=[inactive], creds_by_user={})
    people = list(make_adapter(client).list_people())
    assert people[0].active is False


# --- writeback (the only path not exercised against the live tenant) -----


def test_suspend_patches_enddate_to_now_and_echoes_card(
    make_adapter, sample_user, wiegand_cred
):
    client = FakeAltaClient(users=[sample_user], creds_by_user={"39734661": [wiegand_cred]})
    adapter = make_adapter(client)

    ok = adapter.update_credential_status("39734661", "58943905", CredentialStatus.SUSPENDED)
    assert ok is True
    assert len(client.patch_calls) == 1
    call = client.patch_calls[0]

    # endDate moved to ~now (deactivation), startDate preserved untouched.
    end = datetime.fromisoformat(call["end_date"])
    assert abs((datetime.now(UTC) - end).total_seconds()) < 30
    assert call["start_date"] == wiegand_cred["startDate"]

    # Card block echoed back so the PATCH doesn't blank the physical card.
    assert call["card_number"] == "11732486708497743872"  # the encoded number
    assert call["card_format_id"] == 5150
    assert call["is_output_enabled"] is False


def test_reactivate_patches_enddate_into_future(make_adapter, sample_user, wiegand_cred):
    client = FakeAltaClient(users=[sample_user], creds_by_user={"39734661": [wiegand_cred]})
    adapter = make_adapter(client)

    ok = adapter.update_credential_status("39734661", "58943905", CredentialStatus.ACTIVE)
    assert ok is True
    end = datetime.fromisoformat(client.patch_calls[0]["end_date"])
    assert end > datetime.now(UTC) + timedelta(days=300)


def test_writeback_returns_false_when_credential_missing(make_adapter, sample_user):
    client = FakeAltaClient(users=[sample_user], creds_by_user={"39734661": []})
    adapter = make_adapter(client)
    ok = adapter.update_credential_status("39734661", "nope", CredentialStatus.SUSPENDED)
    assert ok is False
    assert client.patch_calls == []
