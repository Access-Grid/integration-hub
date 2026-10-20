"""Migration 013: legacy cards on never-installed passes are ours to switch on.

Seen on-site: ledger rows from before the Active box was recorded, on passes
issued but never installed. Their cards were off because we wrote them off
to wait for the install, but with nothing recorded they read as switched off
by an operator, and the passes would have been suspended.
"""

from __future__ import annotations

import sqlite3

from agsync.db.schema import _legacy_cards_wait_for_install, apply_migrations


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def _pass(conn, pid, state, card_id="pass-1"):
    conn.execute(
        "INSERT INTO ag_credentials (pacs_person_id, pacs_credential_id, ag_card_id, "
        "status, last_known_ag_state) VALUES (?, 'seos', ?, 'active', ?)",
        (pid, card_id, state),
    )


def _card(conn, pid, active):
    conn.execute(
        "INSERT INTO pacs_cards (pacs_person_id, pacs_credential_id, slot, "
        "facility_code, card_number, active) VALUES (?, 'seos', 1, 'x', 'y', ?)",
        (pid, active),
    )


def _active(conn, pid):
    return conn.execute(
        "SELECT active FROM pacs_cards WHERE pacs_person_id = ?", (pid,)
    ).fetchone()["active"]


def test_an_unknown_card_on_a_never_installed_pass_is_recorded_as_ours():
    conn = _db()
    _pass(conn, "45000303", "created")
    _card(conn, "45000303", None)
    _legacy_cards_wait_for_install(conn)
    assert _active(conn, "45000303") == 0


def test_a_pass_that_has_been_installed_is_left_alone():
    """Off on a live pass is still an operator's decision."""
    conn = _db()
    for pid, state in [("1", "active"), ("2", "suspended"), ("3", "unlink")]:
        _pass(conn, pid, state)
        _card(conn, pid, None)
    _legacy_cards_wait_for_install(conn)
    assert [_active(conn, pid) for pid in "123"] == [None, None, None]


def test_a_recorded_box_is_never_overwritten():
    conn = _db()
    _pass(conn, "45000371", "created")
    _card(conn, "45000371", 1)
    _legacy_cards_wait_for_install(conn)
    assert _active(conn, "45000371") == 1


def test_a_row_without_a_pass_is_left_alone():
    conn = _db()
    _pass(conn, "45000800", "created", card_id=None)
    _card(conn, "45000800", None)
    _legacy_cards_wait_for_install(conn)
    assert _active(conn, "45000800") is None


def test_the_migration_is_recorded():
    conn = _db()
    applied = {r[0] for r in conn.execute("SELECT name FROM _migrations")}
    assert "013_legacy_cards_wait_for_install" in applied
