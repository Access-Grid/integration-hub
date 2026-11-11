"""The tokens the hub created in Unity for Seos passes: the `unity_tokens` table.

A created token carries the `accessgrid` embossed number, like an operator's
enrollment token, so nothing on it says it is ours. This table is what does.
A row is written before its token is created and gets the token's id after,
so a crash in between leaves a row to adopt the token by, not a stray token
that reads as a new enrollment. Card numbers and facility codes are stored
encrypted.
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

    cur = get_db().execute(
        "INSERT INTO unity_tokens (pacs_person_id, pacs_credential_id, facility_code, card_number, "
        "active, activate_date, deactivate_date) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (person_id, credential_id, encrypt(facility_code), encrypt(card_number), activate_date, deactivate_date),
    )
    logger.info("Unity tokens: %s %s — about to create a token for card %s/%s",
                person_id, credential_id, facility_code, card_number)
    return cur.lastrowid


def set_token(row: int, token_id: str) -> None:
    from ....db.connection import get_db

    get_db().execute("UPDATE unity_tokens SET token_id = ? WHERE id = ?", (token_id, row))
    logger.info("Unity tokens: now tracking token %s", token_id)


def set_active(token_id: str, active: bool) -> None:
    from ....db.connection import get_db

    get_db().execute("UPDATE unity_tokens SET active = ? WHERE token_id = ?", (int(active), token_id))


def remove(row: int) -> None:
    from ....db.connection import get_db

    get_db().execute("DELETE FROM unity_tokens WHERE id = ?", (row,))
    logger.info("Unity tokens: no longer tracking row %s", row)
