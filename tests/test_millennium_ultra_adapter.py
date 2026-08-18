"""Millennium Ultra adapter: mapping, the card-format trigger, session paste."""

from __future__ import annotations

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.client import (
    base_url_from_paste,
    parse_pasted_session,
)
from tests._fakes import FakeMillenniumUltraClient


def _client(html, ids=("12345",)):
    return FakeMillenniumUltraClient(
        roster=[{"ID": int(i)} for i in ids],
        pages={i: html for i in ids},
    )


def _people_then_creds(adapter, person_id="12345"):
    # list_people() populates the per-cycle page cache list_credentials() reads.
    list(adapter.list_people())
    return list(adapter.list_credentials(person_id))


# --- people mapping ------------------------------------------------------


def test_person_maps_name_from_detail_page(make_mu_adapter, mu_cardholder_html):
    people = list(make_mu_adapter(_client(mu_cardholder_html)).list_people())
    assert len(people) == 1
    p = people[0]
    assert p.id == "12345"
    assert p.first_name == "Rosalind"
    assert p.last_name == "Marchetti"
    # Composed from the detail page, not the roster's "Last, HID. First".
    assert p.full_name == "Rosalind Marchetti"


def test_person_email_is_synthesized(make_mu_adapter, mu_cardholder_html):
    people = list(make_mu_adapter(_client(mu_cardholder_html)).list_people())
    # Nobody on this tenant has an address; without one, phase 1 would skip
    # every cardholder and nothing would ever provision.
    assert people[0].email == "rosalindmarchetti@iconcreds.com"


def test_real_email_wins_over_synthesized(make_mu_adapter, mu_cardholder_html):
    html = mu_cardholder_html.replace(
        'id="EMail" name="EMail" type="text" value=""',
        'id="EMail" name="EMail" type="text" value="real@example.com"',
    )
    people = list(make_mu_adapter(_client(html)).list_people())
    assert people[0].email == "real@example.com"


def test_email_domain_is_configurable(make_mu_adapter, mu_cardholder_html):
    adapter = make_mu_adapter(_client(mu_cardholder_html), email_domain="example.org")
    people = list(adapter.list_people())
    assert people[0].email == "rosalindmarchetti@example.org"


def test_person_is_active_regardless_of_roster_isactive(make_mu_adapter, mu_cardholder_html):
    # The roster's IsActive flags the row *selected* in the UI, not whether
    # the cardholder is enabled, so the adapter must ignore it. If it were
    # honoured, this person would be skipped by the engine entirely.
    client = FakeMillenniumUltraClient(
        roster=[{"ID": 12345, "IsActive": False}],
        pages={"12345": mu_cardholder_html},
    )
    people = list(make_mu_adapter(client).list_people())
    assert people[0].active is True


def test_pages_fetched_in_one_concurrent_batch(make_mu_adapter, mu_cardholder_html):
    ids = ("12345", "12346", "12347")
    client = _client(mu_cardholder_html, ids)
    list(make_mu_adapter(client).list_people())
    # One batched call, not one round trip per cardholder.
    assert client.page_calls == [list(ids)]


# --- credential mapping --------------------------------------------------


def test_empty_slot_is_not_a_credential(make_mu_adapter, mu_cardholder_html):
    creds = _people_then_creds(make_mu_adapter(_client(mu_cardholder_html)))
    # Slot 3 is empty and must not surface as a credential.
    assert len(creds) == 2
    assert all(c.card_number for c in creds)


def test_card_fields_map_without_decoding(make_mu_adapter, mu_cardholder_html):
    creds = {c.id: c for c in _people_then_creds(make_mu_adapter(_client(mu_cardholder_html)))}
    one = creds["0:30373420026"]
    # Facility and number are already separate decimals — nothing to decode.
    assert one.site_code == "0"
    assert one.card_number == "30373420026"
    assert one.person_id == "12345"
    assert one.file_data == ""


def test_status_follows_the_active_checkbox(make_mu_adapter, mu_cardholder_html):
    creds = {c.id: c for c in _people_then_creds(make_mu_adapter(_client(mu_cardholder_html)))}
    assert creds["0:30373420026"].status is CredentialStatus.ACTIVE
    assert creds["12:884422"].status is CredentialStatus.SUSPENDED


def test_dates_are_carried_through(make_mu_adapter, mu_cardholder_html):
    creds = {c.id: c for c in _people_then_creds(make_mu_adapter(_client(mu_cardholder_html)))}
    one = creds["0:30373420026"]
    assert one.activate_date.year == 2025
    assert one.deactivate_date.year == 2026


# --- the card-format trigger ---------------------------------------------


def test_trigger_matches_configured_format(make_mu_adapter, mu_cardholder_html):
    creds = {c.id: c for c in _people_then_creds(make_mu_adapter(_client(mu_cardholder_html)))}
    # Configured format is 7; slot 1 is format 7, slot 2 is format 1.
    assert creds["0:30373420026"].trigger_active is True
    assert creds["12:884422"].trigger_active is False


def test_trigger_is_per_slot_not_per_cardholder(make_mu_adapter, mu_cardholder_html):
    adapter = make_mu_adapter(_client(mu_cardholder_html), card_format="1")
    creds = {c.id: c for c in _people_then_creds(adapter)}
    # Switching the configured format moves the trigger to the other slot.
    assert creds["0:30373420026"].trigger_active is False
    assert creds["12:884422"].trigger_active is True


def test_no_format_configured_enrolls_nothing(make_mu_adapter, mu_cardholder_html):
    adapter = make_mu_adapter(_client(mu_cardholder_html), card_format="")
    creds = _people_then_creds(adapter)
    # Fail closed: a blank setting must not enroll the whole building.
    assert all(c.trigger_active is False for c in creds)


def test_unknown_person_yields_nothing(make_mu_adapter, mu_cardholder_html):
    adapter = make_mu_adapter(_client(mu_cardholder_html))
    list(adapter.list_people())
    assert list(adapter.list_credentials("99999")) == []


# --- card format discovery -----------------------------------------------


def test_card_formats_read_from_a_live_page(make_mu_adapter, mu_cardholder_html):
    formats = dict(make_mu_adapter(_client(mu_cardholder_html)).card_formats())
    assert formats["7"] == "HID 37"
    assert len(formats) == 7


def test_card_formats_empty_roster(make_mu_adapter):
    assert make_mu_adapter(FakeMillenniumUltraClient()).card_formats() == []


# --- connection ----------------------------------------------------------


def test_test_connection_reports_the_count(make_mu_adapter, mu_cardholder_html):
    result = make_mu_adapter(_client(mu_cardholder_html, ("1", "2", "3"))).test_connection()
    assert result.ok is True
    assert "3 cardholders" in result.message
    assert "ICON" in result.message


def test_test_connection_fails_when_roster_invisible(make_mu_adapter):
    result = make_mu_adapter(FakeMillenniumUltraClient()).test_connection()
    # A valid cookie with no roster visibility is a different failure from a
    # dead cookie, and the message has to say so.
    assert result.ok is False
    assert "no cardholders are visible" in result.message


# --- writeback is deliberately off in v1 ---------------------------------


def test_status_writeback_not_advertised(make_mu_adapter, mu_cardholder_html):
    adapter = make_mu_adapter(_client(mu_cardholder_html))
    assert adapter.supports_status_writeback is False
    assert adapter.update_credential_status("12345", "0:30373420026", CredentialStatus.SUSPENDED) is False


# --- pasted session parsing ----------------------------------------------


CURL = (
    "curl --url 'https://hosted8.mgiaccess.com/Cardholders/Cardholders' "
    "-H 'accept: text/html' "
    "-b '_ga=GA1.2.17; UltraCompanyName=ICON; .AspNet.UltraAuth=vPfIANngUp18; DisableAlarmSound=false' "
    "-H 'pragma: no-cache'"
)


def test_parse_curl_paste():
    cookies = parse_pasted_session(CURL)
    assert cookies[".AspNet.UltraAuth"] == "vPfIANngUp18"
    assert cookies["UltraCompanyName"] == "ICON"
    # Analytics and UI-preference cookies are dropped.
    assert "_ga" not in cookies
    assert "DisableAlarmSound" not in cookies


def test_parse_cookie_header_paste():
    cookies = parse_pasted_session("UltraCompanyName=ICON; .AspNet.UltraAuth=abc123")
    assert cookies == {"UltraCompanyName": "ICON", ".AspNet.UltraAuth": "abc123"}


def test_parse_bare_token_paste():
    token = "vPfIANngUp18fWOpuFn_m1RlSupOn1NC970MrP6zBB7t4sLi30qCvDpJH3oXjjx2"
    assert parse_pasted_session(token) == {".AspNet.UltraAuth": token}


def test_parse_cookie_header_style_h_flag():
    paste = """curl 'https://x' -H 'cookie: .AspNet.UltraAuth=zzz; UltraCompanyName=ACME'"""
    cookies = parse_pasted_session(paste)
    assert cookies[".AspNet.UltraAuth"] == "zzz"
    assert cookies["UltraCompanyName"] == "ACME"


def test_parse_empty_paste():
    assert parse_pasted_session("") == {}
    assert parse_pasted_session("   ") == {}


def test_base_url_recovered_from_paste():
    assert base_url_from_paste(CURL) == "https://hosted8.mgiaccess.com"
    assert base_url_from_paste("no url here") == ""
