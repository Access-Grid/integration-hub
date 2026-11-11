"""The tokens the hub created in Unity for Seos passes: the `unity_tokens` table.

A created token carries the `accessgrid` embossed number, like an operator's
enrollment token, so nothing on it says it is ours. This table is what does.
Card numbers and facility codes are stored encrypted.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _rows(where: str = "", params: tuple = ()) -> list[dict]:
    from ....crypto import decrypt
    from ....db.connection import execute

    return [
        {
            "person_id": r["pacs_person_id"],
            "credential_id": r["pacs_credential_id"],
            "token_id": r["token_id"],
            "facility_code": decrypt(r["facility_code"]),
            "card_number": decrypt(r["card_number"]),
            "active": bool(r["active"]),
        }
        for r in execute(f"SELECT * FROM unity_tokens {where} ORDER BY id", params)
    ]


def all_tokens() -> list[dict]:
    return _rows()


def for_person(person_id: str) -> list[dict]:
    return _rows("WHERE pacs_person_id = ?", (person_id,))


def for_credential(person_id: str, credential_id: str) -> list[dict]:
    return _rows("WHERE pacs_person_id = ? AND pacs_credential_id = ?", (person_id, credential_id))


def add(person_id: str, credential_id: str, token_id: str, facility_code: str, card_number: str) -> None:
    """Record a token we created, switched off until its pass is installed."""
    from ....crypto import encrypt
    from ....db.connection import get_db

    get_db().execute(
        "INSERT INTO unity_tokens (pacs_person_id, pacs_credential_id, token_id, "
        "facility_code, card_number, active) VALUES (?, ?, ?, ?, ?, 0)",
        (person_id, credential_id, token_id, encrypt(facility_code), encrypt(card_number)),
    )
    logger.info("Unity tokens: %s %s — now tracking token %s (card %s/%s)",
                person_id, credential_id, token_id, facility_code, card_number)


def set_active(token_id: str, active: bool) -> None:
    from ....db.connection import get_db

    get_db().execute("UPDATE unity_tokens SET active = ? WHERE token_id = ?", (int(active), token_id))


def remove(token_id: str) -> None:
    from ....db.connection import get_db

    get_db().execute("DELETE FROM unity_tokens WHERE token_id = ?", (token_id,))
    logger.info("Unity tokens: no longer tracking token %s", token_id)
