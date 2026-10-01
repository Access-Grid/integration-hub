"""SQLite schema, applied idempotently at startup.

Each entry in MIGRATIONS is run once, in order. Already-applied migrations
are tracked in the `_migrations` table so we can add to this list without
breaking existing installs.

A step is either SQL or a callable taking the connection — the latter for
data that SQL cannot reach, such as the encrypted settings blobs, whose
contents are ciphertext to SQLite.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

Step = str | Callable[[sqlite3.Connection], None]


def _seos_credential_id_drops_the_slot(conn: sqlite3.Connection) -> None:
    """Key a Seos credential on the cardholder, not on a slot position.

    The id used to be "seos-slot<N>", N being the slot of the lowest-indexed
    card carrying the trigger format. Cards we write carry that format too,
    so writing into a slot below the operator's marker moved the id — and a
    moved id reads as a brand-new credential, which phase 1 provisions a
    second pass for. There is exactly one Seos credential per cardholder, so
    the position never earned its place in the key.
    """
    conn.execute(
        "UPDATE ag_credentials SET pacs_credential_id = 'seos' "
        "WHERE pacs_credential_id LIKE 'seos-slot%'"
    )

    # The ledger of what we wrote into Millennium is an encrypted blob keyed
    # "<person>:<credential>", so it has to be rewritten in Python.
    from ..crypto import decrypt, encrypt

    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'millennium_seos_slots'"
    ).fetchone()
    if row is None:
        return
    ledger = json.loads(decrypt(row[0]))
    migrated: dict[str, list] = {}
    for key, entries in ledger.items():
        person, _, credential = key.partition(":")
        if credential.startswith("seos-slot"):
            key = f"{person}:seos"
        # Merge rather than overwrite: two slot-keyed entries for one
        # cardholder are exactly the duplicate this migration exists to stop.
        migrated.setdefault(key, []).extend(entries)
    conn.execute(
        "UPDATE settings SET value = ? WHERE key = 'millennium_seos_slots'",
        (encrypt(json.dumps(migrated)),),
    )


def _pacs_session_is_keyed_by_vendor(conn: sqlite3.Connection) -> None:
    """Move the captured browser session under a vendor-keyed name.

    It was stored as "millennium_session", which is the shape the whole
    connect flow had: one vendor's cookie names in core. The payload is
    unchanged — only core's name for it — so the row is renamed rather than
    rewritten, and it stays encrypted throughout.
    """
    conn.execute(
        "UPDATE OR REPLACE settings SET key = 'pacs_session:millennium_ultra' "
        "WHERE key = 'millennium_session'"
    )


def _release_card_ids_on_deleted_rows(conn: sqlite3.Connection) -> None:
    """Unstick cardholders whose pass was deleted before that was fixed.

    Phase 1 decides a credential is already handled by the presence of an
    ag_card_id, not by the row's status, and deleting a pass used to leave
    the id behind. Those rows are permanently skipped: delete the card in
    the PACS, add a new one, and nothing happens and nothing is logged.

    Clearing the id on rows already marked deleted lets them be provisioned
    again. Only 'deleted' rows are touched — a suspended or active row
    still has a pass, and its id is the only way back to it.
    """
    conn.execute(
        "UPDATE ag_credentials SET ag_card_id = NULL "
        "WHERE status = 'deleted' AND ag_card_id IS NOT NULL"
    )


def _drop_abandoned_desfire_rows(conn: sqlite3.Connection) -> None:
    """Remove tracking rows left behind by a spell in DESFire mode.

    DESFire keys a credential on the slot it sits in — "slot1", "slot2" —
    while Seos keys it on the cardholder. An install switched between the
    two, and phase 1's rows from the DESFire era outlived the mode: eleven
    of them on the install this was written for, against cardholders that
    are now tracked under "seos".

    Only rows that never became a pass are removed: still 'pending', with
    no AccessGrid card id. Those two together mean provisioning was never
    completed, so there is nothing behind the row to lose — and a genuine
    DESFire install is unaffected, because phase 1 writes the row again on
    the next cycle from what the PACS actually holds.

    Deliberately narrow. A DESFire row that did get a pass keeps its id,
    which is the only way back to that pass.
    """
    conn.execute(
        "DELETE FROM ag_credentials "
        " WHERE pacs_credential_id GLOB 'slot[0-9]*' "
        "   AND status = 'pending' "
        "   AND ag_card_id IS NULL"
    )


MIGRATIONS: list[tuple[str, Step]] = [
    (
        "001_init",
        """
        CREATE TABLE IF NOT EXISTS admin (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            username      TEXT    NOT NULL,
            password_hash TEXT    NOT NULL,
            created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Tracking table: one row per (pacs_person_id, pacs_credential_id) we've
        -- ever provisioned to AccessGrid. Survives across PACS-side deletions
        -- so Phase 3 can detect "this used to exist, now it doesn't."
        CREATE TABLE IF NOT EXISTS ag_credentials (
            pacs_person_id     TEXT NOT NULL,
            pacs_credential_id TEXT NOT NULL,
            ag_card_id         TEXT,
            full_name          TEXT,
            employee_id        TEXT,
            status             TEXT NOT NULL DEFAULT 'pending',
            -- last-known values for direction-of-change detection
            last_synced_email     TEXT,
            last_synced_phone     TEXT,
            last_synced_full_name TEXT,
            last_synced_title     TEXT,
            last_known_ag_state   TEXT,
            -- retry tracking
            sync_error  TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (pacs_person_id, pacs_credential_id)
        );

        CREATE INDEX IF NOT EXISTS idx_ag_credentials_card_id
            ON ag_credentials(ag_card_id);
        CREATE INDEX IF NOT EXISTS idx_ag_credentials_status
            ON ag_credentials(status);

        -- Rolling log buffer; capped via FIFO eviction in logs.store
        CREATE TABLE IF NOT EXISTS logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT    NOT NULL,
            level     TEXT    NOT NULL,
            phase     TEXT,
            message   TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_logs_ts    ON logs(ts);
        CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
        CREATE INDEX IF NOT EXISTS idx_logs_phase ON logs(phase);
        """,
    ),
    (
        "002_login_rate_limit",
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            key           TEXT PRIMARY KEY,
            fail_count    INTEGER NOT NULL DEFAULT 0,
            first_fail_at REAL,
            locked_until  REAL
        );
        """,
    ),
    (
        "003_login_rate_limit_lock_level",
        """
        ALTER TABLE login_attempts
        ADD COLUMN lock_level INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        "004_ag_credentials_sync_ref",
        """
        -- Unique per issue, minted before the API call and stamped into the
        -- card's metadata, so a pass can be found again by something we
        -- chose rather than by whatever id the response happened to return.
        ALTER TABLE ag_credentials ADD COLUMN sync_ref TEXT;

        CREATE INDEX IF NOT EXISTS idx_ag_credentials_sync_ref
            ON ag_credentials(sync_ref);
        """,
    ),
    ("005_seos_credential_id_drops_the_slot", _seos_credential_id_drops_the_slot),
    ("006_pacs_session_is_keyed_by_vendor", _pacs_session_is_keyed_by_vendor),
    ("007_release_card_ids_on_deleted_rows", _release_card_ids_on_deleted_rows),
    ("008_drop_abandoned_desfire_rows", _drop_abandoned_desfire_rows),
]


def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {r[0] for r in conn.execute("SELECT name FROM _migrations")}
    for name, step in MIGRATIONS:
        if name in applied:
            continue
        if callable(step):
            step(conn)
        else:
            conn.executescript(step)
        conn.execute("INSERT INTO _migrations(name) VALUES(?)", (name,))
    conn.commit()
