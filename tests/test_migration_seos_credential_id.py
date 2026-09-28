"""Keying a Seos credential on the cardholder rather than on a slot.

The id used to be "seos-slot<N>", N being the lowest-indexed slot carrying
the trigger format. Cards we write carry that format too, so writing into a
slot below the operator's marker moved the id — and phase 1 reads a moved
id as a credential it has never seen, which it provisions a second pass for.
"""

from __future__ import annotations

import json
import sqlite3

from agsync.crypto import decrypt, encrypt
from agsync.db.schema import _seos_credential_id_drops_the_slot, apply_migrations

LEDGER_KEY = "millennium_seos_slots"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def _ledger(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (LEDGER_KEY,)
    ).fetchone()
    return json.loads(decrypt(row["value"])) if row else None


def test_tracking_rows_lose_the_slot():
    conn = _db()
    conn.execute(
        "INSERT INTO ag_credentials (pacs_person_id, pacs_credential_id, ag_card_id) "
        "VALUES ('11587', 'seos-slot1', 'card-1')"
    )
    _seos_credential_id_drops_the_slot(conn)
    got = conn.execute("SELECT pacs_credential_id FROM ag_credentials").fetchone()
    assert got["pacs_credential_id"] == "seos"


def test_a_desfire_credential_id_is_untouched():
    """Only Seos ids carried the slot; DESFire's "slot2" means the card."""
    conn = _db()
    conn.execute(
        "INSERT INTO ag_credentials (pacs_person_id, pacs_credential_id, ag_card_id) "
        "VALUES ('900', 'slot2', 'card-9')"
    )
    _seos_credential_id_drops_the_slot(conn)
    got = conn.execute("SELECT pacs_credential_id FROM ag_credentials").fetchone()
    assert got["pacs_credential_id"] == "slot2"


def test_the_ledger_is_rekeyed():
    conn = _db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (LEDGER_KEY, encrypt(json.dumps({
            "11587:seos-slot1": [{"slot": 2, "card_number": "1216", "facility_code": "2"}],
        }))),
    )
    _seos_credential_id_drops_the_slot(conn)
    assert _ledger(conn) == {
        "11587:seos": [{"slot": 2, "card_number": "1216", "facility_code": "2"}],
    }


def test_two_slot_keyed_entries_for_one_cardholder_merge():
    """Exactly the duplicate this migration exists to stop, already on disk."""
    conn = _db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (LEDGER_KEY, encrypt(json.dumps({
            "11618:seos-slot1": [{"slot": 1, "card_number": "1238", "facility_code": "2"}],
            "11618:seos-slot2": [{"slot": 2, "card_number": "1243", "facility_code": "2"}],
        }))),
    )
    _seos_credential_id_drops_the_slot(conn)
    entries = _ledger(conn)["11618:seos"]
    assert sorted(e["card_number"] for e in entries) == ["1238", "1243"]


def test_a_missing_ledger_is_not_an_error():
    conn = _db()
    _seos_credential_id_drops_the_slot(conn)  # no settings row at all
    assert _ledger(conn) is None


def test_the_migration_runs_once():
    conn = _db()
    applied = {r[0] for r in conn.execute("SELECT name FROM _migrations")}
    assert "005_seos_credential_id_drops_the_slot" in applied
    apply_migrations(conn)  # idempotent


def test_the_id_no_longer_depends_on_the_slot(
    make_millennium_adapter, millennium_page, set_slot, seos_ledger
):
    """The bug itself: the marker in slot 2 used to yield a different id.

    A card written into slot 1 then made the lowest trigger-format slot 1,
    the id became "seos-slot1", and phase 1 saw a credential with no
    tracking row.
    """
    from agsync.lib.pacs.millennium_ultra.adapter import MODE_SEOS
    from tests._fakes import FakeMillenniumClient

    roster = [{"ID": 11587, "IsActive": True, "Name": "Grid, Accessg"}]

    def _ids_for(marker_slot):
        page = millennium_page
        for index in (1, 2, 3):
            if index == marker_slot:
                page = set_slot(
                    page, index, card_id="7919", card_number="1",
                    facility_code="99", card_format="7", active=True,
                )
            else:
                page = set_slot(page, index, card_id="", card_number="", card_format=None)
        adapter = make_millennium_adapter(
            FakeMillenniumClient(roster=roster, pages={"11587": page}), mode=MODE_SEOS,
        )
        list(adapter.list_people())
        return [c.id for c in adapter.list_credentials("11587")]

    assert _ids_for(1) == _ids_for(2) == _ids_for(3) == ["seos"]
