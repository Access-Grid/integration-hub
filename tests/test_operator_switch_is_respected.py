"""An operator unticking a card is a revocation, not a state to correct.

Millennium's Active checkbox carries no reason. A card we switched off
because nobody had installed the pass yet looks exactly like one an
operator switched off to revoke somebody — and phase 4's install gate,
which only ever asks AccessGrid, read the second as the first and switched
it back on. Every cycle, silently, for a cardholder whose pass was
supposed to stop working.

So we remember what we wrote. A card that is off *and* that we last set on
was changed by somebody else, and is left alone. A card that is off
because we set it off is still ours to switch on when the pass installs —
otherwise the hand-off after an install would never happen.
"""

from __future__ import annotations

import logging

import pytest

from agsync.lib.pacs.base import CredentialStatus
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11666"
ROSTER = [{"ID": PID, "IsActive": True, "Name": "Mowatt, Kai"}]


@pytest.fixture
def adapter(make_millennium_adapter, millennium_page, set_slot, seos_ledger):
    """Kai: one card in slot 1, which the ledger knows is ours."""
    def _build(active: bool):
        page = set_slot(
            millennium_page, 1, card_id="8966", card_number="223",
            facility_code="99", card_format=TRIGGER, active=active,
        )
        for index in (2, 3):
            page = set_slot(page, index, card_id="", card_number="", card_format=None)
        client = FakeMillenniumClient(roster=ROSTER, pages={PID: page})
        built = make_millennium_adapter(
            client, mode=MODE_SEOS, trigger_card_format=TRIGGER
        )
        list(built.list_people())
        SeosLedger.record(PID, "seos", [
            {"slot": 1, "card_number": "223", "facility_code": "99"},
        ])
        return built, client
    return _build


def _set(adapter, status):
    return adapter.update_credential_status(PID, "seos", status)


# =====================================================================
# Remembering
# =====================================================================


def test_switching_a_card_on_is_remembered(adapter):
    built, _ = adapter(active=False)

    assert _set(built, CredentialStatus.ACTIVE) is True
    assert SeosLedger.last_written_active(PID, "seos") == {1: True}


def test_switching_a_card_off_is_remembered(adapter):
    built, _ = adapter(active=True)

    assert _set(built, CredentialStatus.AWAITING_INSTALL) is True
    assert SeosLedger.last_written_active(PID, "seos") == {1: False}


def test_a_card_already_as_asked_is_still_recorded(adapter):
    """How a card that nobody has touched since the upgrade acquires a
    record. Until it has one, an operator's untick cannot be recognised."""
    built, client = adapter(active=True)

    assert _set(built, CredentialStatus.ACTIVE) is True
    assert client.saved == [], "nothing to change, so nothing was written"
    assert SeosLedger.last_written_active(PID, "seos") == {1: True}


def test_a_dry_run_records_nothing(adapter, monkeypatch):
    from agsync import dryrun

    monkeypatch.setattr(dryrun, "active", lambda: True)
    built, _ = adapter(active=True)

    _set(built, CredentialStatus.AWAITING_INSTALL)

    assert SeosLedger.last_written_active(PID, "seos") == {}


# =====================================================================
# Refusing to overrule
# =====================================================================


def test_a_card_an_operator_switched_off_is_left_off(adapter, caplog):
    """The regression. We set it on; somebody set it off; the install gate
    asks for it back on because AccessGrid still shows the pass installed."""
    built, _ = adapter(active=True)
    _set(built, CredentialStatus.ACTIVE)          # our record: on

    switched_off, client = adapter(active=False)  # the operator unticks it

    with caplog.at_level(logging.WARNING):
        assert _set(switched_off, CredentialStatus.ACTIVE) is False

    assert client.saved == [], "the box was not written back on"
    assert "rather than overruling the operator" in caplog.text


def test_a_card_we_switched_off_is_ours_to_switch_on(adapter):
    """The install hand-off. Refusing here would mean a pass that is
    installed never gets a working card."""
    built, _ = adapter(active=True)
    _set(built, CredentialStatus.AWAITING_INSTALL)   # our record: off

    installed, client = adapter(active=False)

    assert _set(installed, CredentialStatus.ACTIVE) is True
    assert client.saved, "the hand-off still happens"


def test_a_card_with_no_record_is_left_to_the_old_behaviour(adapter):
    """An install that has not run this code yet has no record of any
    card. Refusing then would stop every hand-off on the install."""
    built, client = adapter(active=False)
    assert SeosLedger.last_written_active(PID, "seos") == {}

    assert _set(built, CredentialStatus.ACTIVE) is True
    assert client.saved


def test_switching_off_is_never_refused(adapter):
    """The gate exists to stop an uninstalled pass's card working, and
    that is a safety property — an operator ticking a card on for a pass
    nobody holds should not keep it on."""
    built, _ = adapter(active=True)
    _set(built, CredentialStatus.ACTIVE)

    ticked_on, client = adapter(active=True)

    assert _set(ticked_on, CredentialStatus.AWAITING_INSTALL) is True
    assert client.saved


def test_switching_off_an_already_off_card_is_not_refused(adapter):
    """The refusal is only ever about being asked to switch something ON.
    Asked to switch off a card that is already off — which is every cycle
    for an uninstalled pass — the answer is yes, trivially."""
    built, _ = adapter(active=True)
    _set(built, CredentialStatus.ACTIVE)          # our record: on

    already_off, client = adapter(active=False)

    assert _set(already_off, CredentialStatus.AWAITING_INSTALL) is True
    assert client.saved == [], "nothing to change"


def test_a_dry_run_records_nothing_even_when_there_is_nothing_to_do(
    adapter, monkeypatch
):
    """The quiet path. A card already as asked still gets recorded on a
    real run, so it must not on a dry one."""
    from agsync import dryrun

    monkeypatch.setattr(dryrun, "active", lambda: True)
    built, client = adapter(active=True)

    assert _set(built, CredentialStatus.ACTIVE) is True
    assert client.saved == []
    assert SeosLedger.last_written_active(PID, "seos") == {}


def test_one_card_switched_off_stops_the_whole_credential(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """A pass spans two cards. Switching one back on and not the other
    would leave the credential half-revoked, which is a state nobody
    asked for."""
    def build(slot_1_active: bool):
        page = millennium_page
        page = set_slot(page, 1, card_id="8966", card_number="223",
                        facility_code="99", card_format=TRIGGER,
                        active=slot_1_active)
        page = set_slot(page, 2, card_id="8967", card_number="226",
                        facility_code="99", card_format=TRIGGER, active=True)
        page = set_slot(page, 3, card_id="", card_number="", card_format=None)
        client = FakeMillenniumClient(roster=ROSTER, pages={PID: page})
        built = make_millennium_adapter(
            client, mode=MODE_SEOS, trigger_card_format=TRIGGER
        )
        list(built.list_people())
        SeosLedger.record(PID, "seos", [
            {"slot": 1, "card_number": "223", "facility_code": "99"},
            {"slot": 2, "card_number": "226", "facility_code": "99"},
        ])
        return built, client

    built, _ = build(slot_1_active=True)
    built.update_credential_status(PID, "seos", CredentialStatus.ACTIVE)

    half_off, client = build(slot_1_active=False)

    assert half_off.update_credential_status(
        PID, "seos", CredentialStatus.ACTIVE
    ) is False
    assert client.saved == []


def test_rewriting_a_cards_slots_keeps_what_we_know_about_it(seos_ledger):
    """record() replaces the entries, and the flag has to survive it —
    slots are rewritten often, and dropping it there would quietly return
    the gate to overruling whoever unticked the card."""
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "223", "facility_code": "99"},
    ])
    SeosLedger.note_active(PID, "seos", {1: True})

    SeosLedger.record(PID, "seos", [
        {"slot": 2, "card_number": "223", "facility_code": "99"},
    ])

    assert SeosLedger.last_written_active(PID, "seos") == {2: True}


def test_a_different_card_in_the_slot_starts_with_no_opinion(seos_ledger):
    """The flag belongs to the card, not the slot. A new card written into
    the same position has never been set either way by us."""
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "223", "facility_code": "99"},
    ])
    SeosLedger.note_active(PID, "seos", {1: True})

    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "999", "facility_code": "99"},
    ])

    assert SeosLedger.last_written_active(PID, "seos") == {}


# =====================================================================
# When we do not know what we last wrote
# =====================================================================
#
# Every one of these must read as "no opinion" and leave the old
# behaviour alone. Reading a missing record as "we set it off" would make
# the install hand-off refuse itself on every install that has not run
# this code yet.


def test_an_entry_from_before_this_was_recorded_is_unknown(seos_ledger):
    """The upgrade case: the ledger is full of entries with no flag."""
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "223", "facility_code": "99"},
    ])

    assert SeosLedger.last_written_active(PID, "seos") == {}


def test_no_entry_at_all_is_unknown(seos_ledger):
    assert SeosLedger.last_written_active(PID, "seos") == {}


@pytest.mark.parametrize("stored", ["false", "true", "", 0, 1, None, [], {}])
def test_a_value_that_is_not_a_boolean_is_unknown(seos_ledger, stored):
    """A string "false" is not a False. Coercing one with bool() would
    read it as True and then refuse to switch on a card that nobody had
    switched off."""
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "223", "facility_code": "99", "active": stored},
    ])

    assert SeosLedger.last_written_active(PID, "seos") == {}


@pytest.mark.parametrize("slot", ["x", None, "", 1.5, True, ["1"]])
def test_a_slot_that_will_not_parse_is_unknown(seos_ledger, slot):
    SeosLedger.record(PID, "seos", [
        {"slot": slot, "card_number": "223", "facility_code": "99", "active": True},
    ])

    assert SeosLedger.last_written_active(PID, "seos") == {}


def test_an_unreadable_ledger_is_unknown(monkeypatch, caplog):
    """Rather than an exception out of a status write. Failing the other
    way would stop every hand-off on the install over a transient read."""
    def explode(*a, **k):
        raise RuntimeError("the settings blob will not decrypt")

    monkeypatch.setattr(SeosLedger, "get", staticmethod(explode))

    with caplog.at_level(logging.WARNING):
        assert SeosLedger.last_written_active(PID, "seos") == {}

    assert "treating it as unknown" in caplog.text


def test_an_unknown_card_is_still_switched_on(adapter, seos_ledger):
    """The whole point of the fallback: an install that has never recorded
    anything behaves exactly as it did before."""
    built, client = adapter(active=False)
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "223", "facility_code": "99", "active": "nonsense"},
    ])

    assert _set(built, CredentialStatus.ACTIVE) is True
    assert client.saved, "the hand-off happened"


def test_a_known_slot_is_unaffected_by_an_unknown_one(seos_ledger):
    """One corrupt entry must not blind us to the rest."""
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "223", "facility_code": "99", "active": "junk"},
        {"slot": 2, "card_number": "226", "facility_code": "99", "active": True},
    ])

    assert SeosLedger.last_written_active(PID, "seos") == {2: True}
