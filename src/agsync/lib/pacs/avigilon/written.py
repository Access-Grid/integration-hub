"""The tokens the hub created in Unity for Seos passes: the `unity_tokens` table.

A created token carries the `accessgrid` embossed number, like an operator's
enrollment token, so nothing on it says it is ours. This table is what does.
A row is written before its token is created and gets the token's id after,
so a crash in between leaves a reservation that holds a matching token for
operator review. A number match alone never establishes token ownership.
Card numbers and facility codes are stored encrypted.

`active` is the last acknowledged or observed token state. Independent
device restrictions are kept separately from the hub's own holds, and a
pending target is recorded before every status write so an interrupted
write cannot masquerade as a device restriction after restart.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _rows(where: str = "", params: tuple = ()) -> list[dict]:
    from ....crypto import decrypt
    from ....db.connection import execute

    return [
        {
            "row": r["id"],
            "person_id": r["pacs_person_id"],
            "credential_id": r["pacs_credential_id"],
            "token_id": r["token_id"],
            "facility_code": decrypt(r["facility_code"]),
            "card_number": decrypt(r["card_number"]),
            "active": bool(r["active"]),
            "operator_disabled": bool(r["operator_disabled"]),
            "pending_active": None if r["pending_active"] is None else bool(r["pending_active"]),
            "activate_date": r["activate_date"],
            "deactivate_date": r["deactivate_date"],
        }
        for r in execute(f"SELECT * FROM unity_tokens {where} ORDER BY id", params)
    ]


def all_tokens() -> list[dict]:
    return _rows()


def for_person(person_id: str) -> list[dict]:
    return _rows("WHERE pacs_person_id = ?", (person_id,))


def for_credential(person_id: str, credential_id: str) -> list[dict]:
    return _rows("WHERE pacs_person_id = ? AND pacs_credential_id = ?", (person_id, credential_id))


def add(person_id: str, credential_id: str, facility_code: str, card_number: str,
        activate_date: str | None, deactivate_date: str | None) -> int:
    """Record a token about to be created, switched off until its pass is installed."""
    from ....crypto import encrypt
    from ....db.connection import get_db

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        # Encryption uses random nonces, so a SQL uniqueness index over the
        # ciphertext cannot protect this identity. Compare inside the lock.
        existing = next((e for e in all_tokens()
                         if (e["facility_code"], e["card_number"]) == (facility_code, card_number)), None)
        if existing:
            if (existing["person_id"], existing["credential_id"]) != (person_id, credential_id):
                raise ValueError(f"Unity card {facility_code}/{card_number} is already tracked for {existing['person_id']}")
            row = existing["row"]
        else:
            cur = db.execute(
                "INSERT INTO unity_tokens (pacs_person_id, pacs_credential_id, facility_code, card_number, "
                "active, activate_date, deactivate_date) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (person_id, credential_id, encrypt(facility_code), encrypt(card_number), activate_date, deactivate_date),
            )
            row = cur.lastrowid
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    logger.info("Unity tokens: %s %s — tracking card %s/%s as row %s",
                person_id, credential_id, facility_code, card_number, row)
    return row


def set_token(row: int, token_id: str) -> None:
    from ....db.connection import get_db

    get_db().execute("UPDATE unity_tokens SET token_id = ? WHERE id = ?", (token_id, row))
    logger.info("Unity tokens: now tracking token %s", token_id)


def set_active(token_id: str, active: bool) -> None:
    from ....db.connection import get_db

    get_db().execute("UPDATE unity_tokens SET active = ?, pending_active = NULL WHERE token_id = ?",
                     (int(active), token_id))


def begin_status_write(token_id: str, active: bool) -> None:
    """Persist the target before Unity sees the write; retain it on any failure."""
    from ....db.connection import get_db

    get_db().execute("UPDATE unity_tokens SET pending_active = ? WHERE token_id = ?",
                     (int(active), token_id))


def observe_status(entry: dict, status: str) -> dict:
    """Distinguish an observed device edit from our own acknowledged write.

    A pending write that has not landed remains pending, rather than being
    misclassified as an operator change. Unknown statuses are handled by
    the adapter's hold. Only normal Active/Inactive writes come from us;
    Not Yet Active and Expired are independent device restrictions too.
    """
    from .... import dryrun
    from ....db.connection import get_db

    if status not in ("1", "2", "3", "4"):
        return entry
    active = status == "1"
    updated = dict(entry)
    if status in ("3", "4"):
        updated.update(active=False, operator_disabled=True, pending_active=None)
    elif entry["pending_active"] is not None:
        if active != entry["pending_active"]:
            return entry
        updated.update(active=active, pending_active=None)
    elif active != entry["active"]:
        updated.update(active=active, operator_disabled=not active)
    if updated == entry:
        return entry
    if updated["operator_disabled"] != entry["operator_disabled"]:
        logger.info("Unity token %s of %s: independent device restriction %s",
                    entry["token_id"], entry["person_id"],
                    "set" if updated["operator_disabled"] else "cleared")
    if not dryrun.active():
        get_db().execute(
            "UPDATE unity_tokens SET active = ?, operator_disabled = ?, pending_active = ? WHERE id = ?",
            (int(updated["active"]), int(updated["operator_disabled"]),
             None if updated["pending_active"] is None else int(updated["pending_active"]), entry["row"]),
        )
    return updated


def remove(row: int) -> None:
    from ....db.connection import get_db

    get_db().execute("DELETE FROM unity_tokens WHERE id = ?", (row,))
    logger.info("Unity tokens: no longer tracking row %s", row)
