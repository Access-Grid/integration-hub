"""Which Millennium cards each pass rests on, kept in the `pacs_cards` table.

Every change to it is logged, so the Logs tab shows what the sync is
tracking, card by card, as it happens.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Employee ID -> cardholder name, from the latest export, so every log line
# can say who a card belongs to.
NAMES: dict[str, str] = {}


def who(person_id: str) -> str:
    """How the logs name a cardholder: "990001 (Smoke Alpha)"."""
    name = NAMES.get(person_id)
    return f"{person_id} ({name})" if name else person_id

# What a pending change means, in the words the log uses.
_WAITING = {
    "write": "waiting for AccessGrid's card to replace it",
    "insert": "waiting to be written into Millennium",
    "activate": "waiting to be switched on",
    "deactivate": "waiting to be switched off",
    "delete": "waiting to be deleted",
}


def _log_changes(person_id: str, credential_id: str, before: list[dict], after: list[dict]) -> None:
    """One log line per card that was added, changed or dropped."""
    def card(e: dict) -> tuple[str, str]:
        return (str(e.get("facility_code")), str(e.get("card_number")))

    old = {card(e): e for e in before}
    new = {card(e): e for e in after}
    where = f"Millennium cards: {who(person_id)} {credential_id} —"
    for key, e in new.items():
        was = old.get(key)
        if was is None:
            notes = [f"slot {e.get('slot')}"]
            if e.get("pending"):
                notes.append(_WAITING.get(e["pending"], e["pending"]))
            if e.get("status") == "dormant":
                notes.append("dormant")
            logger.info("%s now tracking card %s/%s (%s)", where, *key, ", ".join(notes))
            continue
        changes = []
        if e.get("slot") != was.get("slot"):
            changes.append(f"moved to slot {e.get('slot')}")
        if e.get("pending") != was.get("pending"):
            changes.append(_WAITING.get(e.get("pending"), e.get("pending")) if e.get("pending") else "done")
        if (e.get("status") == "dormant") != (was.get("status") == "dormant"):
            changes.append("dormant" if e.get("status") == "dormant" else "back in the trigger format")
        if "active" in e and e.get("active") != was.get("active"):
            changes.append(f"Active recorded {'on' if e['active'] else 'off'}")
        if changes:
            logger.info(
                "%s card %s/%s in slot %s: %s", where, *key, was.get("slot"), "; ".join(changes),
            )
    for key in old.keys() - new.keys():
        logger.info(
            "%s no longer tracking card %s/%s (was slot %s)", where, *key, old[key].get("slot"),
        )


class PacsCards:
    """The cards each pass rests on: the `pacs_cards` table, one row per card.

    A Seos pass's cards are written into Millennium by us; a DESFire pass's
    card was already there. Either way nothing on the cardholder says which
    pass a card belongs to — stamping a marker into a customer-visible field
    would be a side effect on their data — so the mapping is kept here.

    Entries are read back as {"slot", "card_number", "facility_code"}, plus
    "active" when the Active box we last knew is recorded, "pending" when a
    change awaits the next import, and "status" when the card is dormant.
    The card number and facility code are stored encrypted.
    """

    @staticmethod
    def _entries(where: str = "", params: tuple = ()) -> dict[str, list[dict]]:
        from ....crypto import decrypt
        from ....db.connection import execute

        out: dict[str, list[dict]] = {}
        for r in execute(
            f"SELECT * FROM pacs_cards {where} "
            "ORDER BY pacs_person_id, pacs_credential_id, slot",
            params,
        ):
            entry = {
                "slot": r["slot"],
                "card_number": decrypt(r["card_number"]),
                "facility_code": decrypt(r["facility_code"]),
            }
            if r["active"] is not None:
                entry["active"] = bool(r["active"])
            if r["pending"]:
                entry["pending"] = r["pending"]
            if r["status"] == "dormant":
                entry["status"] = "dormant"
            out.setdefault(f"{r['pacs_person_id']}:{r['pacs_credential_id']}", []).append(entry)
        return out

    @staticmethod
    def all() -> dict[str, list[dict]]:
        """Every recorded card, keyed "person_id:credential_id"."""
        return PacsCards._entries()

    @staticmethod
    def get(person_id: str, credential_id: str) -> list[dict]:
        return PacsCards._entries(
            "WHERE pacs_person_id = ? AND pacs_credential_id = ?",
            (person_id, credential_id),
        ).get(f"{person_id}:{credential_id}", [])

    @staticmethod
    def record(person_id: str, credential_id: str, entries: list[dict]) -> None:
        """Replace a credential's recorded cards, one row per physical card.

        A card that was deleted in Millennium gets rewritten into a fresh
        slot, so the same facility code + card number can be offered twice
        over a credential's life; the later slot is the live one. The Active
        box we last knew survives a re-record of the same card.
        """
        from ....crypto import encrypt
        from ....db.connection import get_db

        before = PacsCards.get(person_id, credential_id)
        known = {
            (str(e["facility_code"]), str(e["card_number"])): e["active"]
            for e in before
            if "active" in e
        }
        deduped: dict[tuple[str, str], dict] = {}
        for entry in entries:
            deduped[(str(entry.get("facility_code")), str(entry.get("card_number")))] = entry

        db = get_db()
        db.execute(
            "DELETE FROM pacs_cards WHERE pacs_person_id = ? AND pacs_credential_id = ?",
            (person_id, credential_id),
        )
        _log_changes(person_id, credential_id, before, list(deduped.values()))
        for (facility_code, card_number), entry in deduped.items():
            active = entry["active"] if "active" in entry else known.get((facility_code, card_number))
            db.execute(
                "INSERT INTO pacs_cards (pacs_person_id, pacs_credential_id, "
                "slot, facility_code, card_number, active, pending, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id, credential_id, entry.get("slot"),
                    encrypt(facility_code), encrypt(card_number),
                    int(active) if isinstance(active, bool) else None,
                    entry.get("pending"),
                    entry.get("status") or "live",
                ),
            )

    @staticmethod
    def entries_for_person(person_id: str) -> dict[str, list[dict]]:
        """Every credential recorded for a cardholder, keyed by credential id."""
        return {
            key.partition(":")[2]: entries
            for key, entries in PacsCards._entries(
                "WHERE pacs_person_id = ?", (person_id,)
            ).items()
        }

    @staticmethod
    def cards_for_person(person_id: str) -> set[tuple[str, str]]:
        """Every card recorded for a cardholder, across all their credentials."""
        return {
            (str(e["facility_code"]), str(e["card_number"]))
            for entries in PacsCards.entries_for_person(person_id).values()
            for e in entries
        }

    @staticmethod
    def forget(person_id: str, credential_id: str) -> None:
        from ....db.connection import get_db

        _log_changes(person_id, credential_id, PacsCards.get(person_id, credential_id), [])
        get_db().execute(
            "DELETE FROM pacs_cards WHERE pacs_person_id = ? AND pacs_credential_id = ?",
            (person_id, credential_id),
        )
