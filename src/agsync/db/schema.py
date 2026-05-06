"""SQLite schema, applied idempotently at startup.

Each entry in MIGRATIONS is run once, in order. Already-applied migrations
are tracked in the `_migrations` table so we can add to this list without
breaking existing installs.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: list[tuple[str, str]] = [
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
    for name, sql in MIGRATIONS:
        if name in applied:
            continue
        conn.executescript(sql)
        conn.execute("INSERT INTO _migrations(name) VALUES(?)", (name,))
    conn.commit()
