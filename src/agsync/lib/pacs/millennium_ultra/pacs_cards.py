"""Which Millennium cards each pass rests on, kept in the `pacs_cards` table."""

from __future__ import annotations


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

        known = {
            (str(e["facility_code"]), str(e["card_number"])): e["active"]
            for e in PacsCards.get(person_id, credential_id)
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

        get_db().execute(
            "DELETE FROM pacs_cards WHERE pacs_person_id = ? AND pacs_credential_id = ?",
            (person_id, credential_id),
        )
