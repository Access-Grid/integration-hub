"""CDVI adapter: mapping, trigger gating, card decode, read-only writeback."""

from __future__ import annotations

import copy

from agsync.lib.pacs.base import CredentialStatus
from tests._fakes import FakeCdviClient


def _people_then_creds(adapter, person_id="5"):
    # list_people() populates the trigger cache list_credentials() reads.
    list(adapter.list_people())
    return list(adapter.list_credentials(person_id))


# --- people mapping ------------------------------------------------------


def test_person_maps_name_email_and_active(make_cdvi_adapter, cdvi_user):
    client = FakeCdviClient(users=[cdvi_user], cards=[])
    people = list(make_cdvi_adapter(client).list_people())
    assert len(people) == 1
    p = people[0]
    assert p.id == "5"
    assert p.full_name == "Amy Hyatt"
    assert p.first_name == "Amy"
    assert p.last_name == "Hyatt"
    assert p.email == "amy@example.com"
    assert p.active is True


def test_person_inactive_when_en_zero(make_cdvi_adapter, cdvi_user):
    user = copy.deepcopy(cdvi_user)
    user["en"] = "0"
    client = FakeCdviClient(users=[user], cards=[])
    people = list(make_cdvi_adapter(client).list_people())
    assert people[0].active is False


# --- card decode + mapping ----------------------------------------------


def test_card_number_decodes_site_and_card(make_cdvi_adapter, cdvi_user, cdvi_card):
    client = FakeCdviClient(users=[cdvi_user], cards=[cdvi_card])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert len(creds) == 1
    c = creds[0]
    assert c.id == "77"
    assert c.person_id == "5"
    assert c.site_code == "69"       # high byte 0x45
    assert c.card_number == "42069"  # low two bytes 0xA455


def test_only_cards_for_that_user_are_returned(make_cdvi_adapter, cdvi_user, cdvi_card):
    other = copy.deepcopy(cdvi_card)
    other["id"] = "88"
    other["user_id"] = "999"
    client = FakeCdviClient(users=[cdvi_user], cards=[cdvi_card, other])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert [c.id for c in creds] == ["77"]


def test_unassigned_cards_are_ignored(make_cdvi_adapter, cdvi_user, cdvi_card):
    floating = copy.deepcopy(cdvi_card)
    floating["id"] = "88"
    floating.pop("user_id")
    client = FakeCdviClient(users=[cdvi_user], cards=[cdvi_card, floating])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert [c.id for c in creds] == ["77"]


def test_card_status_active_when_enabled(make_cdvi_adapter, cdvi_user, cdvi_card):
    client = FakeCdviClient(users=[cdvi_user], cards=[cdvi_card])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].status == CredentialStatus.ACTIVE


def test_card_suspended_when_disabled(make_cdvi_adapter, cdvi_user, cdvi_card):
    disabled = copy.deepcopy(cdvi_card)
    disabled["en"] = "0"
    client = FakeCdviClient(users=[cdvi_user], cards=[disabled])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].status == CredentialStatus.SUSPENDED


def test_card_suspended_when_lost_or_stolen(make_cdvi_adapter, cdvi_user, cdvi_card):
    lost = copy.deepcopy(cdvi_card)
    lost["lost"] = "1"
    client = FakeCdviClient(users=[cdvi_user], cards=[lost])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].status == CredentialStatus.SUSPENDED


def test_bad_card_number_decodes_to_empty(make_cdvi_adapter, cdvi_user, cdvi_card):
    bad = copy.deepcopy(cdvi_card)
    bad["number"] = "not-hex"
    client = FakeCdviClient(users=[cdvi_user], cards=[bad])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].site_code == ""
    assert creds[0].card_number == ""


# --- trigger gating on the custom field ----------------------------------


def test_trigger_active_when_custom_field_set(make_cdvi_adapter, cdvi_user, cdvi_card):
    client = FakeCdviClient(users=[cdvi_user], cards=[cdvi_card])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].trigger_active is True


def test_trigger_inactive_when_custom_field_empty(make_cdvi_adapter, cdvi_user, cdvi_card):
    user = copy.deepcopy(cdvi_user)
    user["custom_large1"] = ""
    client = FakeCdviClient(users=[user], cards=[cdvi_card])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].trigger_active is False


def test_trigger_inactive_when_custom_field_absent(make_cdvi_adapter, cdvi_user, cdvi_card):
    user = copy.deepcopy(cdvi_user)
    user.pop("custom_large1")
    client = FakeCdviClient(users=[user], cards=[cdvi_card])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].trigger_active is False


def test_trigger_inactive_for_explicit_negative(make_cdvi_adapter, cdvi_user, cdvi_card):
    user = copy.deepcopy(cdvi_user)
    user["custom_large1"] = "No"
    client = FakeCdviClient(users=[user], cards=[cdvi_card])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].trigger_active is False


def test_trigger_reads_alternate_custom_field_key(make_cdvi_adapter, cdvi_card):
    # Controller returns the slot under a different spelling.
    user = {"id": "5", "fn": "Amy", "ln": "Hyatt", "en": "1", "customLarge1": "yes"}
    client = FakeCdviClient(users=[user], cards=[cdvi_card])
    creds = _people_then_creds(make_cdvi_adapter(client))
    assert creds[0].trigger_active is True


# --- read-only guarantees ------------------------------------------------


def test_writeback_is_unsupported_and_noop(make_cdvi_adapter, cdvi_user):
    adapter = make_cdvi_adapter(FakeCdviClient(users=[cdvi_user], cards=[]))
    assert adapter.supports_status_writeback is False
    assert adapter.update_credential_status("5", "77", CredentialStatus.SUSPENDED) is False


def test_descriptor_registered():
    from agsync.lib.pacs import available_pacs, build_adapter, get_descriptor

    vendors = {d.vendor for d in available_pacs()}
    assert "cdvi" in vendors
    assert get_descriptor("cdvi").display_name == "CDVI Atrium (On-Prem)"
    # Factory wiring: connection-field ids map straight to __init__ kwargs.
    adapter = build_adapter(
        "cdvi", {"base_url": "https://x", "username": "u", "password": "p"}
    )
    adapter._client.close()
    assert adapter.descriptor().vendor == "cdvi"
