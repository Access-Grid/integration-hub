"""Doing nothing, and saying what would have been done.

Added after a renamed field on the PACS side turned into mass suspension
and a burst of re-provisioning: one silently-empty value made every card
look revoked, so every pass was switched off and a new one issued. The
code was confident and wrong, and nobody read the log until afterwards.

Dry run is therefore the default. Two things have to hold for it to be
worth anything:

  * it gates the call that changes something AND the bookkeeping that
    follows — recording a pass we did not issue is its own damage
  * it covers both directions — gating only the PACS adapter would leave
    phase 1 minting AccessGrid passes and phase 2 suspending them, which
    is where the licences and the locked-out residents came from
"""

from __future__ import annotations

import logging
import pathlib
from types import SimpleNamespace

import pytest

from agsync import dryrun
from agsync.lib.pacs.base import (
    Credential,
    CredentialIdentity,
    CredentialStatus,
    Person,
)
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS, SeosLedger
from agsync.sync import tracking
from agsync.sync.phases import (
    phase1_provision,
    phase2_local_to_ag,
    phase3_deletions,
    phase6_field_changes,
)
from agsync.sync.snapshot import Snapshot
from tests._fakes import FakeMillenniumClient

TRIGGER = "7"
PID = "11665"
ROSTER = [{"ID": PID, "IsActive": True, "Name": "Dorvil, Greg"}]


@pytest.fixture
def dry(monkeypatch):
    """Dry run, as shipped. The suite otherwise runs with it off."""
    monkeypatch.setenv("AG_SYNC_DRY_RUN", "true")
    from agsync.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "dry.db"))
    from agsync.config import get_settings

    get_settings.cache_clear()
    from agsync.db import connection

    connection._conn = None  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()


# =====================================================================
# The switch itself
# =====================================================================


def test_an_install_does_nothing_unless_told_to(monkeypatch):
    """The shipped default.

    Pinned explicitly because the suite's own fixture turns dry run off —
    without this, flipping the default would change nothing visible here.
    """
    monkeypatch.delenv("AG_SYNC_DRY_RUN", raising=False)
    from agsync.config import Settings

    assert Settings(encryption_key="x").dry_run is True


def test_unreadable_settings_mean_dry_run(monkeypatch):
    """The safe reading of "I don't know" is "don't touch anything"."""
    def explode():
        raise RuntimeError("settings are unreadable")

    monkeypatch.setattr("agsync.config.get_settings", explode)
    assert dryrun.active() is True


def test_the_logs_say_so(caplog):
    with caplog.at_level(logging.INFO):
        dryrun.would("delete card %s", "99/74")

    assert "[DRYRUN] would delete card 99/74" in caplog.text


def test_every_mutation_is_gated():
    """A completeness guard, because this is easy to half-do.

    Every file that changes something — at AccessGrid or in Millennium —
    must hold at least one dry-run check per mutating call. A new phase, or
    a new client method, that writes without a gate fails here rather than
    in production.

    Counted per file rather than per line: phase 1's gate sits well above
    its provision call, because the pending tracking row is written in
    between and a dry run must not leave that behind either.
    """
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "agsync"
    mutators = re.compile(
        r"ag\.access_cards\.(provision|issue|update|delete|suspend|resume|unlink|manage)\("
        r"|_client\.(save_cardholder|delete_card)\("
    )
    short: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        calls = len(mutators.findall(text))
        if not calls:
            continue
        gates = text.count("dryrun.active()")
        if gates < calls:
            short.append(f"{path.relative_to(root)}: {calls} mutation(s), {gates} gate(s)")

    assert short == [], "ungated mutation(s):\n" + "\n".join(short)


# =====================================================================
# The PACS side
# =====================================================================


def _adapter(make_millennium_adapter, millennium_page, set_slot):
    page = set_slot(
        millennium_page, 1, card_id="7919", card_number="1",
        facility_code="99", card_format=TRIGGER, active=True,
    )
    for index in (2, 3):
        page = set_slot(page, index, card_id="", card_number="", card_format=None)
    client = FakeMillenniumClient(roster=ROSTER, pages={PID: page})
    adapter = make_millennium_adapter(
        client, mode=MODE_SEOS, trigger_card_format=TRIGGER
    )
    list(adapter.list_people())
    return adapter, client


def test_no_card_is_written_into_the_pacs(
    dry, make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    adapter, client = _adapter(make_millennium_adapter, millennium_page, set_slot)

    with caplog.at_level(logging.INFO):
        ok = adapter.write_back_credentials(
            PID, "seos", [CredentialIdentity("99", "1238")]
        )

    assert ok is True, "the cycle carries on as though it had worked"
    assert client.saved == [], "nothing reached Millennium"
    assert "[DRYRUN] would write 1 credential(s)" in caplog.text


def test_the_ledger_is_not_written_either(
    dry, make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """A ledger naming cards that are not on the cardholder is how a later
    cycle decides a pass has been revoked — which is the failure this whole
    switch exists because of."""
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot)

    adapter.write_back_credentials(PID, "seos", [CredentialIdentity("99", "1238")])

    assert SeosLedger.get(PID, "seos") == []


def test_no_card_is_switched_off(
    dry, make_millennium_adapter, millennium_page, set_slot, seos_ledger, caplog
):
    adapter, client = _adapter(make_millennium_adapter, millennium_page, set_slot)
    SeosLedger.record(PID, "seos", [
        {"slot": 1, "card_number": "1", "facility_code": "99"},
    ])

    with caplog.at_level(logging.INFO):
        adapter.update_credential_status(PID, "seos", CredentialStatus.SUSPENDED)

    assert client.saved == []
    assert "[DRYRUN] would set cardholder 11665 credential seos to inactive" in caplog.text


def test_reading_still_happens(
    dry, make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """Dry run stops writes, not sight. A cycle that cannot read tells us
    nothing about what it would have done."""
    adapter, _ = _adapter(make_millennium_adapter, millennium_page, set_slot)

    assert [c.id for c in adapter.list_credentials(PID)] == ["seos"]


# =====================================================================
# The AccessGrid side — where the licences and the lockouts came from
# =====================================================================


class FakeAG:
    def __init__(self):
        self.calls: list[str] = []
        self.access_cards = SimpleNamespace(
            provision=lambda **kw: self.calls.append("provision"),
            delete=lambda **kw: self.calls.append("delete"),
            suspend=lambda **kw: self.calls.append("suspend"),
            resume=lambda **kw: self.calls.append("resume"),
            update=lambda **kw: self.calls.append("update"),
        )


def _person(**kw):
    """The real dataclass, so a field added later shows up here."""
    return Person(
        id=PID, full_name="Greg Dorvil", first_name="Greg", last_name="Dorvil",
        email="greg@example.com", phone="555", **kw,
    )


def _cred(status=CredentialStatus.ACTIVE, **kw):
    return Credential(
        id="seos", person_id=PID, card_number="1", site_code="99",
        status=status, trigger_active=True, **kw,
    )


def test_no_pass_is_provisioned(dry, db, caplog):
    ag = FakeAG()
    snapshot = Snapshot(
        people={PID: _person()}, credentials_by_person={PID: [_cred()]},
    )

    with caplog.at_level(logging.INFO):
        phase1_provision.run(snapshot, ag, "tpl", "99")

    assert ag.calls == [], "no licence spent"
    assert "[DRYRUN] would provision a pass" in caplog.text
    assert tracking.get(PID, "seos") is None, "and nothing recorded as issued"


def test_no_pass_is_suspended(dry, db, caplog):
    tracking.upsert(
        pacs_person_id=PID, pacs_credential_id="seos", ag_card_id="card-1",
        full_name="Greg Dorvil", employee_id=PID, status="active",
    )
    ag = FakeAG()
    snapshot = Snapshot(
        people={PID: _person()},
        credentials_by_person={PID: [_cred(status=CredentialStatus.SUSPENDED)]},
    )

    with caplog.at_level(logging.INFO):
        phase2_local_to_ag.run(snapshot, ag)

    assert ag.calls == []
    assert "[DRYRUN] would suspend AG card card-1" in caplog.text
    assert tracking.get(PID, "seos").status == "active", "row left alone"


def test_no_pass_is_deleted(dry, db, caplog):
    tracking.upsert(
        pacs_person_id=PID, pacs_credential_id="seos", ag_card_id="card-1",
        full_name="Greg Dorvil", employee_id=PID, status="active",
    )
    ag = FakeAG()
    # The cardholder is read, but the credential is gone.
    snapshot = Snapshot(people={PID: _person()}, credentials_by_person={PID: []})

    with caplog.at_level(logging.INFO):
        deleted = phase3_deletions.run(snapshot, ag)

    assert ag.calls == []
    assert deleted == 0
    assert "[DRYRUN] would delete AG card card-1" in caplog.text
    assert tracking.get(PID, "seos").status == "active", "not marked deleted"


def test_no_contact_detail_is_pushed(dry, db, caplog):
    tracking.upsert(
        pacs_person_id=PID, pacs_credential_id="seos", ag_card_id="card-1",
        full_name="Greg Dorvil", employee_id=PID, status="active",
        last_synced_email="old@example.com",
    )
    ag = FakeAG()
    snapshot = Snapshot(
        people={PID: _person()}, credentials_by_person={PID: [_cred()]},
    )

    with caplog.at_level(logging.INFO):
        phase6_field_changes.run(snapshot, ag)

    assert ag.calls == []
    assert "[DRYRUN] would update AG card card-1" in caplog.text
