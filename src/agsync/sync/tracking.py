"""CRUD for the `ag_credentials` tracking table.

One row per (pacs_person_id, pacs_credential_id) we have ever
provisioned. The row's lifecycle:

    pending  → first seen, provision in flight
    active   → AG card exists and AG state is active/created
    suspended → AG card exists, AG state is suspended
    deleted  → AG card was deleted (we keep the row briefly so phase 3
                can confirm before forgetting)
    error    → last attempt failed, retry_count tracks attempts

We never delete rows except in phase 3 after confirming the AG card is
truly gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..db.connection import execute, execute_one, get_db


@dataclass
class TrackedCredential:
    pacs_person_id: str
    pacs_credential_id: str
    ag_card_id: str | None
    full_name: str
    employee_id: str
    status: str
    last_synced_email: str
    last_synced_phone: str
    last_synced_full_name: str
    last_synced_title: str
    last_known_ag_state: str
    sync_error: str | None
    retry_count: int


def _row_to_tracked(row: Any) -> TrackedCredential:
    return TrackedCredential(
        pacs_person_id=row["pacs_person_id"],
        pacs_credential_id=row["pacs_credential_id"],
        ag_card_id=row["ag_card_id"],
        full_name=row["full_name"] or "",
        employee_id=row["employee_id"] or "",
        status=row["status"],
        last_synced_email=row["last_synced_email"] or "",
        last_synced_phone=row["last_synced_phone"] or "",
        last_synced_full_name=row["last_synced_full_name"] or "",
        last_synced_title=row["last_synced_title"] or "",
        last_known_ag_state=row["last_known_ag_state"] or "",
        sync_error=row["sync_error"],
        retry_count=row["retry_count"] or 0,
    )


def all_tracked() -> list[TrackedCredential]:
    rows = execute("SELECT * FROM ag_credentials ORDER BY created_at ASC")
    return [_row_to_tracked(r) for r in rows]


def get(pacs_person_id: str, pacs_credential_id: str) -> TrackedCredential | None:
    row = execute_one(
        "SELECT * FROM ag_credentials WHERE pacs_person_id = ? AND pacs_credential_id = ?",
        (pacs_person_id, pacs_credential_id),
    )
    return _row_to_tracked(row) if row else None


def get_by_ag_card_id(ag_card_id: str) -> TrackedCredential | None:
    row = execute_one(
        "SELECT * FROM ag_credentials WHERE ag_card_id = ?",
        (ag_card_id,),
    )
    return _row_to_tracked(row) if row else None


def upsert(
    *,
    pacs_person_id: str,
    pacs_credential_id: str,
    ag_card_id: str | None,
    full_name: str,
    employee_id: str,
    status: str,
    last_synced_email: str = "",
    last_synced_phone: str = "",
    last_synced_full_name: str = "",
    last_synced_title: str = "",
    last_known_ag_state: str = "",
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    get_db().execute(
        """
        INSERT INTO ag_credentials (
            pacs_person_id, pacs_credential_id, ag_card_id, full_name, employee_id,
            status, last_synced_email, last_synced_phone, last_synced_full_name,
            last_synced_title, last_known_ag_state, retry_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(pacs_person_id, pacs_credential_id) DO UPDATE SET
            ag_card_id            = excluded.ag_card_id,
            full_name             = excluded.full_name,
            employee_id           = excluded.employee_id,
            status                = excluded.status,
            last_synced_email     = excluded.last_synced_email,
            last_synced_phone     = excluded.last_synced_phone,
            last_synced_full_name = excluded.last_synced_full_name,
            last_synced_title     = excluded.last_synced_title,
            last_known_ag_state   = excluded.last_known_ag_state,
            sync_error            = NULL,
            updated_at            = excluded.updated_at
        """,
        (
            pacs_person_id, pacs_credential_id, ag_card_id, full_name, employee_id,
            status, last_synced_email, last_synced_phone, last_synced_full_name,
            last_synced_title, last_known_ag_state, now, now,
        ),
    )


def update_status(pacs_person_id: str, pacs_credential_id: str, status: str, last_known_ag_state: str = "") -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    get_db().execute(
        """
        UPDATE ag_credentials
           SET status = ?,
               last_known_ag_state = COALESCE(NULLIF(?, ''), last_known_ag_state),
               sync_error = NULL,
               retry_count = 0,
               updated_at = ?
         WHERE pacs_person_id = ? AND pacs_credential_id = ?
        """,
        (status, last_known_ag_state, now, pacs_person_id, pacs_credential_id),
    )


def record_error(pacs_person_id: str, pacs_credential_id: str, error: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    get_db().execute(
        """
        UPDATE ag_credentials
           SET sync_error = ?,
               retry_count = retry_count + 1,
               updated_at = ?
         WHERE pacs_person_id = ? AND pacs_credential_id = ?
        """,
        (error, now, pacs_person_id, pacs_credential_id),
    )


def remove(pacs_person_id: str, pacs_credential_id: str) -> None:
    get_db().execute(
        "DELETE FROM ag_credentials WHERE pacs_person_id = ? AND pacs_credential_id = ?",
        (pacs_person_id, pacs_credential_id),
    )


def failed_records(max_retries: int) -> list[TrackedCredential]:
    rows = execute(
        """
        SELECT * FROM ag_credentials
         WHERE sync_error IS NOT NULL
           AND retry_count < ?
           AND ag_card_id IS NULL
         ORDER BY updated_at ASC
        """,
        (max_retries,),
    )
    return [_row_to_tracked(r) for r in rows]


def update_field_tracking(
    pacs_person_id: str,
    pacs_credential_id: str,
    *,
    full_name: str,
    email: str,
    phone: str,
    title: str,
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    get_db().execute(
        """
        UPDATE ag_credentials
           SET last_synced_full_name = ?,
               last_synced_email     = ?,
               last_synced_phone     = ?,
               last_synced_title     = ?,
               updated_at            = ?
         WHERE pacs_person_id = ? AND pacs_credential_id = ?
        """,
        (full_name, email, phone, title, now, pacs_person_id, pacs_credential_id),
    )


def update_last_known_ag_state(
    pacs_person_id: str, pacs_credential_id: str, ag_state: str
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    get_db().execute(
        """
        UPDATE ag_credentials
           SET last_known_ag_state = ?,
               updated_at          = ?
         WHERE pacs_person_id = ? AND pacs_credential_id = ?
        """,
        (ag_state, now, pacs_person_id, pacs_credential_id),
    )


def mark_deduped(
    *,
    pacs_person_id: str,
    pacs_credential_id: str,
    existing_ag_card_id: str | None,
    full_name: str,
) -> None:
    """Record a row for a credential we declined to provision because another
    AG card with the same (site_code, card_number) already exists. We point
    ag_card_id at the foreign card so the UI can link to it, but mark status
    as 'deduped' so phase 3 / 6 know not to mutate it."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    get_db().execute(
        """
        INSERT INTO ag_credentials (
            pacs_person_id, pacs_credential_id, ag_card_id, full_name, employee_id,
            status, last_synced_email, last_synced_phone, last_synced_full_name,
            last_synced_title, last_known_ag_state, retry_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'deduped', '', '', ?, '', '', 0, ?, ?)
        ON CONFLICT(pacs_person_id, pacs_credential_id) DO UPDATE SET
            ag_card_id  = excluded.ag_card_id,
            status      = 'deduped',
            sync_error  = NULL,
            updated_at  = excluded.updated_at
        """,
        (
            pacs_person_id, pacs_credential_id, existing_ag_card_id,
            full_name, pacs_person_id, full_name, now, now,
        ),
    )
