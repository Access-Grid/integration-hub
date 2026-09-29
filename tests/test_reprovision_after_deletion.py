"""Provisioning again after a pass has been deleted.

Phase 1 decides a credential is already handled by the presence of an
ag_card_id, not by the row's status — and deleting a pass used to leave the
id behind. Those rows were then skipped forever: delete a cardholder's card
in the PACS, add a new one, and nothing happened and nothing was logged,
because the check is a bare continue.

Keying the Seos credential on the cardholder rather than the slot made this
reachable. Before, re-adding a card in a different slot produced a
different credential id, so a fresh row and a fresh provision — by
accident.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from agsync.db.schema import _release_card_ids_on_deleted_rows, apply_migrations
from agsync.sync import tracking
from agsync.sync.phases import phase1_provision
from agsync.sync.snapshot import Snapshot


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("AG_SYNC_DB_PATH", str(tmp_path / "probe.db"))
    from agsync.config import get_settings
    get_settings.cache_clear()
    from agsync.db import connection
    connection._conn = None  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()


def _row(**kw):
    defaults = dict(
        pacs_person_id="9390", pacs_credential_id="seos", ag_card_id="pass-1",
        full_name="George Lampon", employee_id="9390", status="active",
    )
    defaults.update(kw)
    return defaults


# --- marking a deletion --------------------------------------------------


def test_marking_deleted_releases_the_card_id(db):
    tracking.upsert(**_row())
    tracking.mark_deleted("9390", "seos")

    got = tracking.get("9390", "seos")
    assert got.ag_card_id in (None, "")
    assert got.status == "deleted"


def test_the_row_survives_so_the_history_does(db):
    tracking.upsert(**_row())
    tracking.mark_deleted("9390", "seos")
    assert tracking.get("9390", "seos") is not None


def test_suspending_does_not_release_the_card_id(db):
    """A suspended pass still exists, and the id is the only way back."""
    tracking.upsert(**_row())
    tracking.update_status("9390", "seos", "suspended", last_known_ag_state="suspended")
    assert tracking.get("9390", "seos").ag_card_id == "pass-1"


# --- what phase 1 then does ----------------------------------------------


def _provision_once(monkeypatch, tracked):
    person = SimpleNamespace(
        id="9390", full_name="George Lampon", email="g@icon.test", phone="",
        title="", active=True,
    )
    cred = SimpleNamespace(
        id="seos", trigger_active=True, status=None, site_code="",
        allocate_identity=True, card_number="", file_data=None,
        activate_date=None, deactivate_date=None,
    )
    snap = Snapshot()
    snap.people["9390"] = person
    snap.credentials_by_person["9390"] = [cred]

    monkeypatch.setattr(phase1_provision.tracking, "get", lambda *a: tracked)
    monkeypatch.setattr(phase1_provision.tracking, "upsert", lambda **kw: None)
    monkeypatch.setattr(
        phase1_provision.tracking, "update_last_known_ag_state", lambda *a: None
    )

    issued: list[dict] = []
    ag = SimpleNamespace(access_cards=SimpleNamespace(
        provision=lambda **kw: (
            issued.append(kw), SimpleNamespace(id="new-card", state="created")
        )[1],
        list=lambda **kw: [],
    ))
    phase1_provision.run(snap, ag, "tpl-1")
    return issued


def test_a_deleted_row_is_provisioned_again(monkeypatch):
    """The whole point: adding a card back gets the holder a pass again."""
    deleted = SimpleNamespace(
        ag_card_id=None, status="deleted", last_known_ag_state="deleted",
        sync_error=None, retry_count=0,
    )
    assert len(_provision_once(monkeypatch, deleted)) == 1


def test_a_live_row_is_still_skipped(monkeypatch):
    live = SimpleNamespace(
        ag_card_id="pass-1", status="active", last_known_ag_state="created",
        sync_error=None, retry_count=0,
    )
    assert _provision_once(monkeypatch, live) == []


# --- migration 007 -------------------------------------------------------


def _memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def test_the_migration_unsticks_rows_deleted_before_the_fix():
    conn = _memory_db()
    conn.execute(
        "INSERT INTO ag_credentials (pacs_person_id, pacs_credential_id, "
        "ag_card_id, status) VALUES ('9390', 'seos', 'pass-1', 'deleted')"
    )
    _release_card_ids_on_deleted_rows(conn)
    got = conn.execute("SELECT ag_card_id FROM ag_credentials").fetchone()
    assert got["ag_card_id"] is None


def test_the_migration_leaves_live_rows_alone():
    """An active or suspended row still has a pass behind that id."""
    conn = _memory_db()
    for status in ("active", "suspended", "pending"):
        conn.execute(
            "INSERT INTO ag_credentials (pacs_person_id, pacs_credential_id, "
            "ag_card_id, status) VALUES (?, 'seos', 'pass-1', ?)",
            (f"p-{status}", status),
        )
    _release_card_ids_on_deleted_rows(conn)
    kept = conn.execute(
        "SELECT COUNT(*) c FROM ag_credentials WHERE ag_card_id = 'pass-1'"
    ).fetchone()["c"]
    assert kept == 3


def test_the_migration_is_recorded_once():
    conn = _memory_db()
    applied = {r[0] for r in conn.execute("SELECT name FROM _migrations")}
    assert "007_release_card_ids_on_deleted_rows" in applied
    apply_migrations(conn)  # idempotent
