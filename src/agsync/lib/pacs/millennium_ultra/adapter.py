"""Millennium Ultra → vendor-agnostic adapter.

What makes Millennium different from every other adapter here:

  * **There is no API.** The adapter drives the operator's own web screens
    with a session cookie captured by the AG Connect side-car, because the
    login is captcha-gated and can never be replayed headlessly. When the
    cookie dies, MillenniumAuthError propagates and the engine asks a human
    to reconnect (see agsync.notifications) instead of guessing.

  * **The trigger is a card format, not a sentinel field.** The operator
    picks one format at setup time — "HID 37", say — and any card slot on
    any cardholder carrying that format is enrolled. There is nothing to
    type into a comment field. An *empty* slot cannot carry a format
    (verified live: Millennium discards a format posted for a slot with no
    card), so the trigger is always evaluated against a slot that holds a
    real card.

  * **It runs in one of two directions**, chosen at setup:

      DESFire — the card already exists in Millennium. We copy its facility
        code + card number out to AccessGrid. Millennium is the source of
        truth and we never write a card, only toggle State.

      Seos — AccessGrid mints the credential. The trigger card marks *who*
        should get a pass; AccessGrid allocates the facility code and card
        number, and we write them back into the cardholder's empty slots.
        A cardholder needs two slots we can write, because a person who
        installs on both a phone and a watch (or is issued as part of a
        pair) needs one each. One of them is the marker's own — it holds
        a placeholder number that opens nothing, so the first credential
        overwrites it — leaving one empty slot to find.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from ..base import (
    ConnectionResult,
    Credential,
    CredentialIdentity,
    CredentialStatus,
    PacsDescriptor,
    PacsRecordUnavailable,
    Person,
)
from . import export, migrate
from .client import (
    ANTIFORGERY_COOKIE_PREFIX,
    COMPANY_COOKIE,
    TIMEOFFSET_COOKIE,
    MillenniumError,
    MillenniumUltraClient,
)
from .export import Slot

logger = logging.getLogger(__name__)

MODE_DESFIRE = "desfire"
MODE_SEOS = "seos"

# The id a new pass is recorded under, then "card-2", "card-3"... Passes
# recorded before keep the id they were issued under ("seos", "slot2").
NEW_CREDENTIAL_ID = "card"

# The trigger card's slot takes AccessGrid's first card. A Seos pair has a
# second (Apple and Google each get one), which needs an empty slot.
PAIR_EMPTY_SLOTS = 1


def _first_valid_email(*candidates: str) -> str:
    """The first of these that could actually be delivered to.

    Millennium's email fields are free text and hold whatever was typed —
    a name, a note, a phone number. Issuing a pass to one of those fails at
    AccessGrid with an error about the address rather than about the field
    it came from, so anything without an @ and a dot after it is treated as
    absent and the synthesized address is used instead.
    """
    for candidate in candidates:
        value = (candidate or "").strip()
        if not value or " " in value:
            continue
        local, _, domain = value.partition("@")
        if local and "." in domain:
            return value
    return ""


def synthesize_email(first: str, last: str, employee_id: str, domain: str) -> str:
    """first.last.<employee id>@domain, for a cardholder with no address.

    The Employee ID keeps two people with the same name apart.
    """
    if not employee_id:
        logger.error(
            "Millennium: cannot synthesize an email for %s %s — no Employee ID",
            first, last,
        )
        return ""
    domain = (domain or "").strip().lstrip("@")
    if not domain:
        return ""
    parts = [re.sub(r"[^a-z0-9]", "", (p or "").lower()) for p in (first, last)]
    local = ".".join([p for p in parts if p] + [str(employee_id)])
    return f"{local}@{domain}"


class SeosLedger:
    """Which Millennium slots hold the AccessGrid-allocated cards: the
    `pacs_cards` table, one row per card.

    In Seos mode the card does not exist in Millennium until we write it, so
    there is nothing on the cardholder that says "this slot belongs to
    AccessGrid". Stamping a marker into a customer-visible field would be a
    side effect on their data, so we keep the mapping on our side instead.

    Entries are read back as {"slot", "card_number", "facility_code"}, plus
    "active" when the Active box we last knew is recorded and "pending" when
    a change awaits the next import. The card number and facility code are
    stored encrypted.
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
        return SeosLedger._entries()

    @staticmethod
    def get(person_id: str, credential_id: str) -> list[dict]:
        return SeosLedger._entries(
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
            for e in SeosLedger.get(person_id, credential_id)
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
            for key, entries in SeosLedger._entries(
                "WHERE pacs_person_id = ?", (person_id,)
            ).items()
        }

    @staticmethod
    def cards_for_person(person_id: str) -> set[tuple[str, str]]:
        """Every card recorded for a cardholder, across all their credentials."""
        return {
            (str(e["facility_code"]), str(e["card_number"]))
            for entries in SeosLedger.entries_for_person(person_id).values()
            for e in entries
        }

    @staticmethod
    def forget(person_id: str, credential_id: str) -> None:
        from ....db.connection import get_db

        get_db().execute(
            "DELETE FROM pacs_cards WHERE pacs_person_id = ? AND pacs_credential_id = ?",
            (person_id, credential_id),
        )


class MillenniumUltraAdapter:
    def __init__(
        self,
        base_url: str = "",
        email_domain: str = "",
        notify_email: str = "",
        trigger_card_format: str = "",
        trigger_card_format_label: str = "",
        mode: str = MODE_DESFIRE,
        pair: bool = False,
        auth_cookie: str = "",
        company_name: str = "",
        time_offset: str = "",
        request_token: str = "",
        request_token_cookie: str = "",
    ):
        session = _stored_session() if not auth_cookie else {}
        self.base_url = base_url or session.get("base_url", "")
        self.email_domain = email_domain
        self.notify_email = notify_email
        self.trigger_card_format = str(trigger_card_format or "")
        self.mode = MODE_SEOS if mode == MODE_SEOS else MODE_DESFIRE
        # Whether the AccessGrid template is a pair (Apple + Google). A Seos
        # pair is two card identities to write back; any other pair shares
        # the Millennium card's one.
        self.pair = bool(pair)
        self._offset_seconds = int(time_offset or session.get("time_offset") or 0)
        self._client = MillenniumUltraClient(
            base_url=self.base_url,
            request_token=request_token or session.get("request_token", ""),
            request_token_cookie=(
                request_token_cookie or session.get("request_token_cookie", "")
            ),
            auth_cookie=auth_cookie or session.get("auth_cookie", ""),
            company_name=company_name or session.get("company_name", ""),
            time_offset=str(time_offset or session.get("time_offset") or ""),
        )

        from . import DESCRIPTOR

        self._descriptor = DESCRIPTOR
        # This cycle's export, by Employee ID. Refilled by list_people.
        self._cardholders: dict[str, export.Cardholder] = {}
        # The trigger format's display name, which is what the export prints.
        self._trigger_label = str(trigger_card_format_label or "")
        # Employee IDs the ledger says we hold cards on, refreshed per cycle.
        self._ours: set[str] = set()

    # -- contract --------------------------------------------------------

    def descriptor(self) -> PacsDescriptor:
        return self._descriptor

    def test_connection(self) -> ConnectionResult:
        ok, message = self._client.test_connection()
        return ConnectionResult(ok=ok, message=message)

    def card_formats(self) -> list[tuple[str, str]]:
        """Every card format defined on this install, for the setup screen.

        Millennium has no endpoint that lists formats, so we read the
        <select> off the first cardholder in the roster.
        """
        cardholder_id = self._client.first_cardholder_id()
        if not cardholder_id:
            return []
        return self._client.card_formats(cardholder_id)

    def list_people(self) -> Iterable[Person]:
        """Everyone in this cycle's export, keyed by Employee ID."""
        migrate.ensure(self)
        if not self._trigger_label:
            # Without it no card reads as a trigger, which would look like
            # everybody's pass being withdrawn at once.
            raise MillenniumError(
                "the trigger card format has no stored name — pick it again in Settings"
            )

        rows = export.parse(self._client.export_cardholders(), self._offset_seconds)
        if not rows:
            raise MillenniumError("Millennium's export came back empty")

        by_id: dict[str, list[tuple[export.Cardholder, str]]] = {}
        for row in rows:
            email = _first_valid_email(row.email) or synthesize_email(
                row.first_name, row.last_name, row.employee_id, self.email_domain
            )
            if not row.employee_id:
                logger.error(
                    "Millennium: skipping %s %s — no Employee ID",
                    row.first_name, row.last_name,
                )
                continue
            by_id.setdefault(row.employee_id, []).append((row, email))

        self._cardholders = {}
        self._ours = {key.partition(":")[0] for key in SeosLedger.all()}
        for employee_id, matches in by_id.items():
            if len(matches) > 1:
                logger.error(
                    "Millennium: skipping Employee ID %s — it names %d cardholders (%s)",
                    employee_id, len(matches),
                    ", ".join(f"{r.first_name} {r.last_name}" for r, _ in matches),
                )
                continue
            row, email = matches[0]
            self._cardholders[employee_id] = row
            yield Person(
                id=employee_id,
                full_name=" ".join(p for p in (row.first_name, row.last_name) if p),
                first_name=row.first_name,
                last_name=row.last_name,
                email=email,
                phone=row.phone,
                # Millennium has no cardholder-level enable flag.
                active=True,
                raw={"employee_id": employee_id},
            )

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        pid = str(person_id)
        row = self._cardholders.get(pid)
        if row is None:
            # Unread is not the same as empty: answering [] here would let
            # phase 3 read a cardholder missing from this export as a deleted
            # credential.
            raise PacsRecordUnavailable(f"cardholder {pid} is not in this cycle's export")
        slots = list(row.slots)
        if self.mode == MODE_DESFIRE:
            self._adopt_slot_ids(pid, slots)
        if (
            self.mode == MODE_SEOS
            and pid in self._ours
            and not any(self._is_trigger(s) for s in slots)
        ):
            # Drop the record of any pass whose cards are all gone, which is
            # what lets phase 3 delete it. A card still there in another
            # format is dormant, not gone. A Seos pass with a trigger card
            # left is kept, and reads as suspended.
            self._forget_if_nothing_remains(pid, slots)
        return self._credentials(pid, slots)

    # -- writes: none until Millennium's import is wired in ----------------

    @property
    def supports_status_writeback(self) -> bool:
        return False

    @property
    def supports_credential_writeback(self) -> bool:
        return False

    @property
    def supports_credential_retirement(self) -> bool:
        return False

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        return False

    def write_back_credentials(
        self, person_id: str, credential_id: str, identities: list[CredentialIdentity]
    ) -> bool:
        return False

    def retire_credentials(
        self, person_id: str, credential_id: str, identities: list[CredentialIdentity]
    ) -> int:
        return 0

    def written_credentials(self) -> dict[tuple[str, str], list[str]]:
        """Card numbers this integration has written into Millennium.

        Read from the ledger rather than from the cardholder pages: this is
        for a page listing every issued pass, and fetching ~90 KB per holder
        to render a one-line summary would be absurd.
        """
        out: dict[tuple[str, str], list[str]] = {}
        for key, entries in SeosLedger.all().items():
            person_id, _, credential_id = key.partition(":")
            numbers = [str(e.get("card_number")) for e in entries if e.get("card_number")]
            if numbers:
                out[(person_id, credential_id)] = numbers
        return out


    def session_from_cookies(self, cookies: dict[str, str]) -> dict:
        """The cookie jar reduced to what this adapter is constructed from.

        `timeoffset` travels with the auth cookie because Millennium's date
        fields are rendered and parsed against it — a session without it
        writes card activation times in the wrong timezone.
        """
        from . import DESCRIPTOR

        spec = DESCRIPTOR.browser_login
        # The anti-forgery cookie's name is suffixed per application, so
        # it is found by prefix and its name stored beside its value —
        # nothing downstream can reconstruct it.
        token_name = next(
            (n for n in cookies if n.startswith(ANTIFORGERY_COOKIE_PREFIX)), "",
        )
        return {
            "auth_cookie": cookies.get(spec.required_cookie, ""),
            "base_url": self.base_url,
            "request_token": cookies.get(token_name, "") if token_name else "",
            "request_token_cookie": token_name,
            "company_name": cookies.get(COMPANY_COOKIE, ""),
            "time_offset": cookies.get(TIMEOFFSET_COOKIE, ""),
        }

    def validate_session(self, session: dict) -> tuple[bool, str]:
        client = None
        try:
            client = MillenniumUltraClient(
                base_url=session.get("base_url") or self.base_url,
                auth_cookie=session.get("auth_cookie", ""),
                company_name=session.get("company_name", ""),
                time_offset=session.get("time_offset", ""),
                request_token=session.get("request_token", ""),
                request_token_cookie=session.get("request_token_cookie", ""),
            )
            if not client.first_cardholder_id():
                return False, "signed in, but no cardholders were readable"
        except Exception as e:  # noqa: BLE001 — reported to the operator verbatim
            return False, f"{type(e).__name__}: {e}"
        finally:
            if client is not None:
                client.close()
        return True, ""


    # -- internals -------------------------------------------------------

    def _forget_if_nothing_remains(self, pid: str, slots: list[Slot]) -> None:
        """Drop each ledger entry whose cards are all gone from the cardholder.

        Every credential of theirs, not just the first. A cardholder can hold
        more than one Seos pass, and clearing their cards clears all of them —
        an entry left behind would outlive its pass and suspend whatever the
        next trigger card earns.
        """
        live = {(s.facility_code, s.card_number) for s in slots if not s.empty}
        for credential_id, entries in SeosLedger.entries_for_person(pid).items():
            if any(
                (str(e.get("facility_code")), str(e.get("card_number"))) in live
                for e in entries
            ):
                continue
            logger.info(
                "Millennium: forgetting the cards recorded for cardholder %s "
                "under %s (%s) — none of them are on the cardholder any more",
                pid, credential_id,
                ", ".join(
                    f"{e.get('facility_code')}/{e.get('card_number')}"
                    for e in entries
                ) or "nothing",
            )
            SeosLedger.forget(pid, credential_id)


    def _is_trigger(self, slot: Slot) -> bool:
        # A slot only carries a format when it holds a real card, so this is
        # never true for an empty slot.
        return (
            bool(self._trigger_label)
            and slot.card_format == self._trigger_label
            and not slot.empty
        )

    def _adopt_slot_ids(self, pid: str, slots: list[Slot]) -> None:
        """Record the cards behind DESFire passes tracked from before pacs_cards.

        Those passes were keyed by slot ("slot2"). Each is recorded once,
        under that id, against the card now in that slot. A slot that is
        empty by now held a card that was deleted, and its pass is revoked.
        """
        from ....db.connection import execute

        if SeosLedger.entries_for_person(pid):
            return
        for row in execute(
            "SELECT pacs_credential_id FROM ag_credentials WHERE pacs_person_id = ? "
            "AND status != 'deleted' AND pacs_credential_id LIKE 'slot%'",
            (pid,),
        ):
            slot = next(
                (s for s in slots
                 if f"slot{s.index}" == row["pacs_credential_id"] and self._is_trigger(s)),
                None,
            )
            if slot is not None:
                SeosLedger.record(pid, row["pacs_credential_id"], [{
                    "slot": slot.index,
                    "facility_code": slot.facility_code,
                    "card_number": slot.card_number,
                }])

    def _desfire_credential(
        self, pid: str, credential_id: str, entries: list[dict], slots: list[Slot]
    ) -> Credential | None:
        """A pass carrying the Millennium card's own identity and state."""
        cards = {(e["facility_code"], e["card_number"]) for e in entries}
        slot = next(
            (s for s in slots if (s.facility_code, s.card_number) in cards), None
        )
        if slot is None:
            return None
        return Credential(
            id=credential_id,
            person_id=pid,
            card_number=slot.card_number,
            site_code=slot.facility_code,
            status=(
                CredentialStatus.ACTIVE if slot.active else CredentialStatus.SUSPENDED
            ),
            # The card's own validity, so the pass matches the badge rather
            # than starting whenever the sync happened to notice it.
            activate_date=slot.activation,
            deactivate_date=slot.expiration,
            trigger_active=True,
            raw={"slot": slot.index},
        )

    def _credentials(self, pid: str, slots: list[Slot]) -> list[Credential]:
        """One credential per pass this cardholder holds or has just asked for.

        Each trigger-format slot is checked against `pacs_cards`:

          * a card of theirs we recorded → that pass; if it moved slot, the
            record follows it
          * the slot is recorded with a different card → error, no pass
          * the card is recorded on somebody else, or we track a pass for a
            cardholder we hold no card of → error, no pass
          * otherwise a new pass. Its trigger card is recorded now (in Seos,
            awaiting the write of AccessGrid's card); phase 1 issues it, and
            keeps trying until it has.

        A pass is *held* — left exactly as it is by every phase — while one
        of its cards sits on the cardholder in another format (dormant), or
        while its slot holds a card we did not record.
        """
        mine = SeosLedger.entries_for_person(pid)
        held: set[str] = set()
        present = {(s.facility_code, s.card_number): s for s in slots if not s.empty}
        for credential_id, entries in mine.items():
            changed = False
            for e in entries:
                there = present.get((e["facility_code"], e["card_number"]))
                dormant = there is not None and not self._is_trigger(there)
                if dormant != (e.get("status") == "dormant"):
                    e["status"] = "dormant" if dormant else "live"
                    changed = True
                    logger.info(
                        "Millennium: cardholder %s card %s/%s %s the trigger format — %s",
                        pid, e["facility_code"], e["card_number"],
                        "left" if dormant else "is back in",
                        "holding its pass" if dormant else "tracking it again",
                    )
                if dormant:
                    held.add(credential_id)
            if changed:
                SeosLedger.record(pid, credential_id, entries)
        for slot in slots:
            if not self._is_trigger(slot):
                continue
            card = (slot.facility_code, slot.card_number)
            owner = next(
                (cid for cid, entries in mine.items()
                 if any((e["facility_code"], e["card_number"]) == card for e in entries)),
                None,
            )
            if owner is not None:
                entries = mine[owner]
                if any(e["slot"] != slot.index for e in entries
                       if (e["facility_code"], e["card_number"]) == card):
                    for e in entries:
                        if (e["facility_code"], e["card_number"]) == card:
                            e["slot"] = slot.index
                    SeosLedger.record(pid, owner, entries)
                continue
            recorded = next(
                (e for entries in mine.values() for e in entries if e["slot"] == slot.index),
                None,
            )
            if recorded is not None:
                held.update(cid for cid, entries in mine.items() if recorded in entries)
                _card_problem(
                    pid, card,
                    f"Millennium: cardholder {pid} slot {slot.index} holds card "
                    f"{card[0]}/{card[1]}, but we recorded {recorded['facility_code']}/"
                    f"{recorded['card_number']} there — not issuing",
                )
                continue
            if not mine and (self._card_recorded_elsewhere(pid, card) or _tracked_pass(pid)):
                _card_problem(
                    pid, card,
                    f"Millennium: cardholder {pid}'s trigger card {card[0]}/{card[1]} "
                    "does not match what we recorded — not issuing",
                )
                continue
            seos = self.mode == MODE_SEOS
            empty = [s for s in slots if s.empty]
            needed = PAIR_EMPTY_SLOTS if seos and self.pair else 0
            if len(empty) < needed:
                logger.info(
                    "Millennium: skipping cardholder %s — a pass needs %d free card "
                    "slot(s), found %d",
                    pid, needed, len(empty),
                )
                continue
            credential_id, n = NEW_CREDENTIAL_ID, 2
            while credential_id in mine:
                credential_id, n = f"{NEW_CREDENTIAL_ID}-{n}", n + 1
            mine[credential_id] = [{
                "slot": slot.index, "facility_code": card[0], "card_number": card[1],
                **({"pending": "write"} if seos else {}),
            }]
            SeosLedger.record(pid, credential_id, mine[credential_id])

        build = self._seos_credential if self.mode == MODE_SEOS else self._desfire_credential
        out: list[Credential] = []
        for credential_id, entries in sorted(mine.items()):
            credential = build(pid, credential_id, entries, slots)
            if credential is None and credential_id in held:
                # Its slot holds a card we did not record: reported as it
                # was, so the hold keeps its pass alive.
                credential = Credential(
                    id=credential_id, person_id=pid, trigger_active=True,
                    card_number=entries[0]["card_number"],
                    site_code=entries[0]["facility_code"],
                )
            elif credential is None:
                # A DESFire card that is gone: phase 3 revokes its pass.
                SeosLedger.forget(pid, credential_id)
            if credential is not None:
                out.append(replace(credential, hold=credential_id in held))
        return out

    @staticmethod
    def _card_recorded_elsewhere(pid: str, card: tuple[str, str]) -> bool:
        return any(
            (e["facility_code"], e["card_number"]) == card
            for key, entries in SeosLedger.all().items()
            if key.partition(":")[0] != pid
            for e in entries
        )

    def _seos_credential(
        self,
        pid: str,
        credential_id: str,
        entries: list[dict],
        slots: list[Slot],
    ) -> Credential | None:
        trigger_slot = entries[0]["slot"] if entries else None
        # A trigger card still awaiting AccessGrid's card says nothing about
        # the pass: its Active box is the operator's placeholder.
        ledger = [e for e in entries if not e.get("pending")]

        # Once written, the state of the cards we wrote drives the pass.
        #
        # Matched by card identity rather than slot: a slot is just a
        # position, and if our card were replaced by a different one, reading
        # that slot's Active box would report on somebody else's card.
        #
        # And every written credential has to be present and active, not just
        # one of them. AccessGrid can suspend a pass but not a single device
        # on it, so a holder whose watch credential was deleted cannot be
        # half-revoked; the honest options are "leave it fully working" or
        # "suspend it". Access control fails closed, so a credential removed
        # or deactivated in Millennium suspends the pass, and the log says
        # which one caused it.
        live = {
            (s.facility_code, s.card_number): s for s in slots if not s.empty
        }
        # The credential is only as valid as its shortest-lived card, and only
        # live once its latest-starting one is. Both read off the cards we
        # wrote rather than the pass, because an operator editing the dates in
        # Millennium is the case this exists for.
        recorded = [
            live[(str(e.get("facility_code")), str(e.get("card_number")))]
            for e in ledger
            if (str(e.get("facility_code")), str(e.get("card_number"))) in live
        ]
        expires = [s.expiration for s in recorded if s.expiration]
        starts = [s.activation for s in recorded if s.activation]
        deactivate_date = min(expires) if expires else None
        activate_date = max(starts) if starts else None

        status = CredentialStatus.ACTIVE
        for entry in ledger:
            key = (str(entry.get("facility_code")), str(entry.get("card_number")))
            slot = live.get(key)
            if slot is None:
                logger.info(
                    "Millennium: card %s/%s is gone from cardholder %s — "
                    "suspending the pass",
                    key[0], key[1], pid,
                )
                status = CredentialStatus.SUSPENDED
                break
            if not slot.active:
                logger.info(
                    "Millennium: card %s/%s is inactive on cardholder %s — "
                    "suspending the pass",
                    key[0], key[1], pid,
                )
                status = CredentialStatus.SUSPENDED
                break
        now = datetime.now(UTC)
        if status is CredentialStatus.ACTIVE and activate_date and activate_date > now:
            # Dated to start later. The card should not open a door before
            # the day it was issued for, and neither should the pass.
            logger.info(
                "Millennium: cardholder %s is not valid until %s — holding the "
                "pass until then",
                pid, activate_date.strftime("%Y-%m-%d"),
            )
            status = CredentialStatus.SUSPENDED

        logger.info(
            "Millennium: cardholder %s → %s %s credential (%s); slots %s",
            pid, status.value, credential_id,
            ", ".join(
                f"{e.get('facility_code')}/{e.get('card_number')}" for e in ledger
            ) or "nothing written yet",
            _describe(slots),
        )
        return Credential(
            id=credential_id,
            person_id=pid,
            # Empty on purpose: AccessGrid allocates the facility code and
            # card number, and phase 1 must not send one of its own.
            card_number="",
            site_code="",
            status=status,
            # Carried so phase 3 can retire a credential Millennium has
            # dated out, whatever its Active box still says.
            activate_date=activate_date,
            deactivate_date=deactivate_date,
            trigger_active=True,
            allocate_identity=True,
            raw={
                "trigger_slot": trigger_slot,
                "written": [
                    f"{e.get('facility_code')}/{e.get('card_number')}" for e in ledger
                ],
            },
        )


def _card_problem(pid: str, card: tuple[str, str], problem: str) -> None:
    """A card we will not act on: logged here, and mailed to the operator."""
    from .... import notifications

    logger.error("%s", problem)
    notifications.notify_card_problem(f"{pid}:{card[0]}/{card[1]}", problem)


def _tracked_pass(pid: str) -> bool:
    """Whether we track a live AccessGrid pass for this cardholder."""
    from ....db.connection import execute_one

    return execute_one(
        "SELECT 1 FROM ag_credentials WHERE pacs_person_id = ? AND status != 'deleted'",
        (pid,),
    ) is not None


def _describe(slots: list[Slot]) -> str:
    """One-line slot summary, for diagnostics."""
    return ", ".join(
        f"{s.index}:empty" if s.empty
        else f"{s.index}:{s.facility_code}/{s.card_number} "
             f"fmt={s.card_format or '-'} {'on' if s.active else 'off'}"
        for s in slots
    ) or "none"

def _stored_session() -> dict:
    """The cookie AG Connect captured, if the operator has connected."""
    try:
        from ....settings_store import PacsSession

        return PacsSession.load("millennium_ultra") or {}
    except Exception:  # noqa: BLE001 — settings/db may not exist yet in tests
        return {}


HELP_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "pacs.millennium_ultra.base_url": "Millennium Ultra login URL (e.g. https://hosted105.mgiaccess.com)",
        "pacs.millennium_ultra.email_domain": "Domain for synthesized email addresses (e.g. cards.example.com)",
        "pacs.millennium_ultra.notify_email": "Notification email (used when the Millennium session expires)",
        "pacs.millennium_ultra.mode_desfire": "Your AccessGrid card template issues DESFire, so cards read from Millennium: the facility code and card number on each trigger card are copied out to AccessGrid. Nothing is written back.",
        "pacs.millennium_ultra.mode_seos": "Your AccessGrid card template issues HID Seos, so cards are written into Millennium: AccessGrid allocates the facility code and card number, and the sync engine writes them into each trigger cardholder's empty card slots.",
        "pacs.millennium_ultra.trigger_help": (
            "Millennium Ultra has no API and its login is protected by a "
            "captcha, so this integration works through a session you open "
            "yourself: click Connect to Millennium, sign in, and the browser "
            "hands the session back encrypted. Then choose the card format "
            "that acts as the enrollment trigger — any cardholder holding a "
            "card in that format gets an AccessGrid pass on the next cycle. "
            "In DESFire mode the card's facility code and number are copied "
            "out to AccessGrid. In Seos mode AccessGrid mints them and the "
            "sync engine writes them into the cardholder's empty card slots, "
            "which is why a cardholder needs two free slots before Seos will "
            "provision — a phone and a watch install use one each. "
            "Cardholders here store no email address, so one is synthesized "
            "from the name and Employee ID against the domain above; hand the "
            "pass out with the QR code on the Credentials page. Suspending or "
            "resuming a pass toggles the card's Active box; access levels, "
            "photos and every other field are left exactly as they are."
        ),
    },
    "es": {
        "pacs.millennium_ultra.base_url": "URL de inicio de sesión de Millennium Ultra (p. ej. https://hosted105.mgiaccess.com)",
        "pacs.millennium_ultra.email_domain": "Dominio para las direcciones de correo sintetizadas (p. ej. cards.example.com)",
        "pacs.millennium_ultra.notify_email": "Correo de notificación (se usa cuando caduca la sesión de Millennium)",
        "pacs.millennium_ultra.mode_desfire": "Su plantilla de AccessGrid emite DESFire, así que las tarjetas se leen desde Millennium: el código de instalación y el número de cada tarjeta disparadora se copian a AccessGrid. No se escribe nada de vuelta.",
        "pacs.millennium_ultra.mode_seos": "Su plantilla de AccessGrid emite HID Seos, así que las tarjetas se escriben en Millennium: AccessGrid asigna el código de instalación y el número de tarjeta, y el motor de sincronización los escribe en las ranuras libres de cada titular disparador.",
        "pacs.millennium_ultra.trigger_help": (
            "Millennium Ultra no tiene API y su inicio de sesión está "
            "protegido por un captcha, por lo que esta integración funciona "
            "mediante una sesión que usted mismo abre: pulse Conectar con "
            "Millennium, inicie sesión y el navegador devuelve la sesión "
            "cifrada. Después elija el formato de tarjeta que actúa como "
            "disparador de inscripción: cualquier titular con una tarjeta de "
            "ese formato recibirá un pase de AccessGrid en el siguiente "
            "ciclo. En modo DESFire se copian a AccessGrid el código de "
            "instalación y el número de la tarjeta. En modo Seos, AccessGrid "
            "los genera y el motor de sincronización los escribe en las "
            "ranuras de tarjeta libres del titular; por eso Seos exige dos "
            "ranuras libres, ya que una instalación en teléfono y otra en "
            "reloj usan una cada una. Aquí los titulares no guardan correo "
            "electrónico, así que se sintetiza uno a partir del nombre y del "
            "identificador con el dominio indicado arriba; entregue el pase "
            "con el código QR de la página Credenciales. Suspender o "
            "reanudar un pase cambia la casilla Activa de la tarjeta; los "
            "niveles de acceso, las fotos y todos los demás campos quedan "
            "intactos."
        ),
    },
}

