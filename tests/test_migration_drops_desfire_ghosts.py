"""Migration 008 — tracking rows left behind by a spell in DESFire mode.

DESFire keys a credential on the slot it occupies ("slot1"), Seos on the
cardholder ("seos"). An install that ran as one and then the other kept both
sets of rows, and the DESFire ones never became anything: eleven rows against
cardholders now tracked under "seos", all still 'pending' with no card id.

They are inert — every phase skips a pending row with no AccessGrid card —
but they clutter the only place that says what this integration believes it
has issued, and the per-card credential ids being designed next would be read
right alongside them.
"""

from __future__ import annotations

import sqlite3

import pytest

from agsync.db.schema import _drop_abandoned_desfire_rows, apply_migrations


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c)
    return c


def _add(conn, person, credential, *, status, ag_card_id=None):
    conn.execute(
        """
        INSERT INTO ag_credentials (
            pacs_person_id, pacs_credential_id, ag_card_id, full_name,
            employee_id, status, retry_count, created_at, updated_at
        ) VALUES (?, ?, ?, 'Someone', ?, ?, 0, '2026-10-01', '2026-10-01')
        """,
        (person, credential, ag_card_id, person, status),
    )


def _ids(conn):
    return sorted(
        (r["pacs_person_id"], r["pacs_credential_id"])
        for r in conn.execute("SELECT * FROM ag_credentials")
    )


def test_a_pending_desfire_row_with_no_pass_is_removed(conn):
    _add(conn, "11591", "slot1", status="pending")
    _add(conn, "11618", "slot3", status="pending")

    _drop_abandoned_desfire_rows(conn)

    assert _ids(conn) == []


def test_a_desfire_row_that_holds_a_pass_is_kept(conn):
    """Its id is the only way back to the pass it issued."""
    _add(conn, "11591", "slot1", status="active", ag_card_id="pass-1")
    _add(conn, "11618", "slot2", status="pending", ag_card_id="pass-2")

    _drop_abandoned_desfire_rows(conn)

    assert _ids(conn) == [("11591", "slot1"), ("11618", "slot2")]


def test_a_desfire_row_whose_pass_was_deleted_is_kept(conn):
    """It did become a pass once, and the row is the record of that.

    `mark_deleted` clears the card id, so "no id" alone does not mean
    "never issued" — the status is what separates the two.
    """
    _add(conn, "11591", "slot1", status="deleted")
    _add(conn, "11618", "slot2", status="suspended")

    _drop_abandoned_desfire_rows(conn)

    assert _ids(conn) == [("11591", "slot1"), ("11618", "slot2")]


def test_seos_rows_are_untouched(conn):
    """Including a pending one, which is a Seos provision still in flight."""
    _add(conn, "11591", "seos", status="active", ag_card_id="pass-1")
    _add(conn, "9146", "seos", status="pending")
    _add(conn, "11652", "seos", status="deleted")

    _drop_abandoned_desfire_rows(conn)

    assert _ids(conn) == [("11591", "seos"), ("11652", "seos"), ("9146", "seos")]


def test_an_id_that_merely_starts_with_slot_is_not_a_slot(conn):
    """The match is "slot" then a digit, not any id beginning with it."""
    _add(conn, "74", "slotted-token", status="pending")
    _add(conn, "75", "73:99:31313", status="pending")

    _drop_abandoned_desfire_rows(conn)

    assert _ids(conn) == [("74", "slotted-token"), ("75", "73:99:31313")]


def test_the_whole_live_shape(conn):
    """The eleven ghosts beside the rows that matter, as the install has them."""
    for person, slots in (
        ("11632", (1, 2, 3)), ("11591", (1, 2, 3)),
        ("11652", (1, 2)), ("11618", (1, 2, 3)),
    ):
        for n in slots:
            _add(conn, person, f"slot{n}", status="pending")
    _add(conn, "11591", "seos", status="active", ag_card_id="baaMKBJ2UvRxs7o")
    _add(conn, "9146", "seos", status="active", ag_card_id="zhMS_2vFr4A_ZX8")
    _add(conn, "11652", "seos", status="deleted")

    _drop_abandoned_desfire_rows(conn)

    assert _ids(conn) == [
        ("11591", "seos"), ("11652", "seos"), ("9146", "seos"),
    ]


def test_the_migration_runs_and_is_recorded_once(conn):
    _add(conn, "11591", "slot1", status="pending")
    apply_migrations(conn)  # idempotent

    assert _ids(conn) == [("11591", "slot1")], (
        "already-applied migrations must not re-run on rows created since"
    )
    recorded = [
        r[0] for r in conn.execute(
            "SELECT name FROM _migrations WHERE name = '008_drop_abandoned_desfire_rows'"
        )
    ]
    assert len(recorded) == 1
