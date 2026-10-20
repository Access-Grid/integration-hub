"""Migration 011: the Seos ledger moves from a settings blob into `pacs_cards`."""

from __future__ import annotations

import json
import sqlite3

from agsync.crypto import decrypt, encrypt
from agsync.db.schema import _seos_ledger_becomes_pacs_cards, apply_migrations


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def test_every_card_becomes_a_row_and_the_blob_goes():
    conn = _db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('millennium_seos_slots', ?)",
        (encrypt(json.dumps({
            "11587:seos": [
                {"slot": 2, "card_number": "1216", "facility_code": "2", "active": False},
                {"slot": 3, "card_number": "1217", "facility_code": "2"},
            ],
        })),),
    )
    _seos_ledger_becomes_pacs_cards(conn)

    rows = [dict(r) for r in conn.execute(
        "SELECT pacs_person_id, pacs_credential_id, slot, card_number, status, active, pending "
        "FROM pacs_cards ORDER BY slot"
    )]
    # The card number is stored encrypted, never as itself.
    assert all(r["card_number"] not in ("1216", "1217") for r in rows)
    for r in rows:
        r["card_number"] = decrypt(r["card_number"])
    assert rows == [
        {"pacs_person_id": "11587", "pacs_credential_id": "seos", "slot": 2,
         "card_number": "1216", "status": "live", "active": 0, "pending": None},
        {"pacs_person_id": "11587", "pacs_credential_id": "seos", "slot": 3,
         "card_number": "1217", "status": "live", "active": None, "pending": None},
    ]
    assert conn.execute(
        "SELECT 1 FROM settings WHERE key = 'millennium_seos_slots'"
    ).fetchone() is None


def test_an_install_with_no_ledger_gets_an_empty_table():
    conn = _db()
    assert conn.execute("SELECT COUNT(*) FROM pacs_cards").fetchone()[0] == 0
