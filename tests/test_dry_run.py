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
    CredentialStatus,
    Person,
)
from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS
from agsync.sync import tracking
from agsync.sync.phases import (
    phase1_provision,
    phase2_local_to_ag,
    phase3_deletions,
    phase6_field_changes,
)
from agsync.sync.snapshot import Snapshot
from tests._fakes import FakeMillenniumClient, export_row

# Captured at import, before the suite's autouse fixture replaces it, so a
# test can ask for the real behaviour without reverting every other patch
# in the test — monkeypatch.undo() would also drop the temporary database
# and any stubbed engine.
_REAL_ACTIVE = dryrun.active

PID = "11665"


@pytest.fixture
def dry(monkeypatch):
    """Dry run, as shipped. The suite otherwise runs with it off."""
    monkeypatch.setattr(dryrun, "active", lambda: True)
    yield
    dryrun.invalidate()


@pytest.fixture
def real_dryrun(monkeypatch):
    """Ask the real `active()`, which reads the stored setting."""
    monkeypatch.setattr(dryrun, "active", _REAL_ACTIVE)
    dryrun.invalidate()
    yield
    dryrun.invalidate()


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


def test_an_install_does_nothing_unless_told_to(db, real_dryrun, monkeypatch):
    """The shipped default: no stored setting means dry run.

    Pinned explicitly because the suite's own fixture turns dry run off —
    without this, flipping the default would change nothing visible here.
    """

    assert dryrun.active() is True


def test_the_setting_is_stored_not_read_from_the_environment(db, real_dryrun, monkeypatch):
    """It lives on the settings page: the hub runs as a Windows service, so
    an environment variable means a trip to System Properties by somebody
    who already has the hub's own UI open."""
    monkeypatch.setenv("AG_SYNC_DRY_RUN", "false")
    dryrun.invalidate()

    assert dryrun.active() is True, "the environment does not decide this"

    dryrun.set_active(False)
    assert dryrun.active() is False


def test_saving_it_takes_effect_at_once(db, real_dryrun, monkeypatch):
    """Rather than when a cache happens to expire — an operator who turns
    it off and sees nothing happen will turn something else off next."""
    dryrun.set_active(False)
    assert dryrun.active() is False

    dryrun.set_active(True)
    assert dryrun.active() is True


def test_an_unreadable_setting_means_dry_run(real_dryrun, monkeypatch):
    """The safe reading of "I don't know" is "don't touch anything"."""
    dryrun.invalidate()

    def explode(_key):
        raise RuntimeError("the settings table is unreadable")

    monkeypatch.setattr("agsync.settings_store.get", explode)
    assert dryrun.active() is True


def test_a_failure_is_not_cached(real_dryrun, monkeypatch):
    """A transient read failure must not pin the install into dry run."""
    dryrun.invalidate()
    monkeypatch.setattr(
        "agsync.settings_store.get",
        lambda _k: (_ for _ in ()).throw(RuntimeError("transient")),
    )
    assert dryrun.active() is True

    monkeypatch.setattr("agsync.settings_store.get", lambda _k: "false")
    assert dryrun.active() is False, "recovered without waiting for a TTL"


@pytest.mark.parametrize("stored", ["true", "True", "yes", "", "nonsense", None])
def test_only_an_explicit_false_lets_it_act(real_dryrun, monkeypatch, stored):
    dryrun.invalidate()
    monkeypatch.setattr("agsync.settings_store.get", lambda _k: stored)

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


def test_reading_still_happens(dry, make_millennium_adapter):
    """Dry run stops writes, not sight. A cycle that cannot read tells us
    nothing about what it would have done."""
    marker = {"card_number": "1", "facility_code": "99", "card_format": "HID 37"}
    client = FakeMillenniumClient(rows=[export_row(PID, "Greg", "Dorvil", slots=[marker])])
    adapter = make_millennium_adapter(client, mode=MODE_SEOS)
    list(adapter.list_people())

    assert [c.id for c in adapter.list_credentials(PID)] == ["card"]


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


# =====================================================================
# The settings page
# =====================================================================


@pytest.fixture
def engine(monkeypatch):
    from agsync.routes import settings as settings_routes

    calls: list[str] = []

    class _Engine:
        def trigger_now(self):
            calls.append("trigger")

    monkeypatch.setattr(settings_routes, "get_engine", lambda: _Engine())
    return calls


def _post(dry_run: str = ""):
    from agsync.routes import settings as settings_routes

    return settings_routes.update_dry_run(
        request=None, dry_run=dry_run, _user=None
    )


def test_ticking_the_box_lets_it_act(db, engine, real_dryrun, monkeypatch):
    result = _post("on")

    assert result.headers["location"] == "/settings?ok=dry_run"
    assert dryrun.active() is False


def test_an_absent_checkbox_means_dry_run(db, engine, real_dryrun, monkeypatch):
    """The value that matters is the one the browser does not send.

    An unticked checkbox is omitted from the form entirely, so reading the
    field as "whatever arrived" would leave the install acting forever.
    """
    dryrun.set_active(False)

    _post()

    assert dryrun.active() is True


def test_the_change_is_visible_on_the_page(db, engine, real_dryrun, monkeypatch):
    """The page reports what is in force, not what was last submitted."""
    from agsync.routes import settings as settings_routes

    dryrun.set_active(False)
    assert settings_routes.dryrun.active() is False

    _post()
    assert settings_routes.dryrun.active() is True


def test_a_cycle_runs_so_the_operator_sees_the_result(db, engine, real_dryrun, monkeypatch):
    """Turning it off and watching nothing happen for a minute is how
    somebody decides the setting did not work."""
    _post("on")

    assert engine == ["trigger"]
