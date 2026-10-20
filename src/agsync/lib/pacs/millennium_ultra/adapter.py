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
from datetime import UTC, datetime, timedelta

from .... import dryrun
from ..base import (
    ConnectionResult,
    Credential,
    CredentialIdentity,
    CredentialStatus,
    PacsDescriptor,
    PacsRecordUnavailable,
    Person,
)
from . import export, import_file, migrate
from .client import (
    ANTIFORGERY_COOKIE_PREFIX,
    COMPANY_COOKIE,
    TIMEOFFSET_COOKIE,
    MillenniumError,
    MillenniumUltraClient,
)
from .export import Slot
from .pacs_cards import PacsCards

logger = logging.getLogger(__name__)

MODE_DESFIRE = "desfire"
MODE_SEOS = "seos"

# The id a new pass is recorded under, then "card-2", "card-3"... Passes
# recorded before keep the id they were issued under ("seos", "slot2").
NEW_CREDENTIAL_ID = "card"

# The trigger card's slot takes AccessGrid's first card. A Seos pair has a
# second (Apple and Google each get one), which needs an empty slot.
PAIR_EMPTY_SLOTS = 1

# A recorded card not yet in Millennium's final state: the trigger card a new
# pass is waiting to overwrite ("write"), and AccessGrid's cards waiting for
# the import to put them there ("insert").
UNWRITTEN = ("write", "insert")


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
        self._ours = {key.partition(":")[0] for key in PacsCards.all()}
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

    # -- writes: recorded in pacs_cards, sent by finish_cycle -------------

    @property
    def supports_status_writeback(self) -> bool:
        return True

    @property
    def supports_credential_writeback(self) -> bool:
        return self.mode == MODE_SEOS

    @property
    def supports_credential_retirement(self) -> bool:
        """Only Seos cards are ours to remove; DESFire cards are the customer's."""
        return self.mode == MODE_SEOS

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        """Switch the pass's cards' Active boxes, at the end of this cycle.

        Recorded as pending in pacs_cards; finish_cycle sends it. A card we
        switched on that is now off was switched off by an operator, and is
        not switched back on.
        """
        pid = str(person_id)
        holder = self._cardholders.get(pid)
        entries = PacsCards.get(pid, credential_id)
        if holder is None or not entries:
            return False
        want = status is CredentialStatus.ACTIVE
        boxes = {(s.facility_code, s.card_number): s.active for s in holder.slots if not s.empty}
        cards = [
            e for e in entries
            if (e["facility_code"], e["card_number"]) in boxes
            and e.get("pending") not in (*UNWRITTEN, "delete")
        ]
        if not cards:
            return False
        if want and any(
            e.get("active") is True and not boxes[(e["facility_code"], e["card_number"])]
            for e in cards
        ):
            logger.warning(
                "Millennium: cardholder %s credential %s was switched off in Millennium "
                "after we switched it on — leaving it off",
                pid, credential_id,
            )
            return False
        for e in cards:
            if boxes[(e["facility_code"], e["card_number"])] == want:
                e["active"] = want
                e.pop("pending", None)
            else:
                e["pending"] = "activate" if want else "deactivate"
        PacsCards.record(pid, credential_id, entries)
        return True

    def finish_cycle(self) -> None:
        """Send this cycle's pending card changes as one import.

        A cardholder the import log skips keeps its changes pending, and is
        tried again next cycle.
        """
        # Deletes first: the import below records its cards from the ledger
        # as read here, and read before a delete it put the deleted card back.
        self._delete_cards()
        rows: dict[str, import_file.Row] = {}
        pending: list[tuple[str, str, list[dict]]] = []
        for key, entries in PacsCards.all().items():
            pid, _, credential_id = key.partition(":")
            changes = [
                e for e in entries if e.get("pending") in ("activate", "deactivate", "insert")
            ]
            holder = self._cardholders.get(pid)
            if not changes or holder is None:
                continue
            row = rows.setdefault(
                pid, import_file.Row(pid, holder.first_name, holder.last_name)
            )
            for e in changes:
                row.cells[(e["slot"], "Encoded Card No.")] = e["card_number"]
                row.cells[(e["slot"], "Facility Code")] = e["facility_code"]
                if e["pending"] == "insert":
                    row.cells.update(self._new_card_cells(e["slot"], entries, holder))
                else:
                    row.cells[(e["slot"], "Active")] = str(e["pending"] == "activate")
            pending.append((pid, credential_id, entries))
        if not rows:
            return
        if dryrun.active():
            dryrun.would("switch Active on %d Millennium cardholder(s): %s",
                          len(rows), ", ".join(sorted(rows)))
            return

        skipped: set[str] = set()
        skipped_cards: dict[tuple[str, str], str] = {}
        for imported in import_file.build(list(rows.values())):
            log = self._client.import_cardholders(imported)
            skipped |= log.skipped()
            skipped_cards |= log.skipped_cards()
        for pid, credential_id, entries in pending:
            if pid in skipped:
                continue
            taken = False
            for e in entries:
                if e.get("pending") not in ("activate", "deactivate", "insert"):
                    continue
                card = (e["facility_code"], e["card_number"])
                if card in skipped_cards:
                    logger.error(
                        "Millennium did not take card %s/%s for %s — keeping it to try "
                        "again: %s",
                        *card, pid, skipped_cards[card],
                    )
                    continue
                taken = taken or e["pending"] == "insert"
                e["active"] = e.pop("pending") == "activate"
            if taken:
                # AccessGrid's cards are in: the trigger card they replaced
                # is no longer on the cardholder.
                entries = [e for e in entries if e.get("pending") != "write"]
            PacsCards.record(pid, credential_id, entries)

    def _delete_cards(self) -> None:
        """Delete the cards marked for deletion, one request each.

        A card already gone from the cardholder is simply forgotten. One that
        Millennium will not delete stays marked, and is tried again.
        """
        for key, entries in PacsCards.all().items():
            pid, _, credential_id = key.partition(":")
            doomed = [e for e in entries if e.get("pending") == "delete"]
            holder = self._cardholders.get(pid)
            if not doomed or holder is None:
                continue
            present = {(s.facility_code, s.card_number) for s in holder.slots if not s.empty}
            for e in doomed:
                card = (e["facility_code"], e["card_number"])
                if card in present:
                    if dryrun.active():
                        dryrun.would("delete card %s/%s from cardholder %s", *card, pid)
                        continue
                    try:
                        deleted = self._client.delete_card_by_number(pid, *card)
                    except MillenniumError as err:
                        logger.warning(
                            "Millennium: could not delete card %s/%s from %s: %s",
                            *card, pid, err,
                        )
                        continue
                    if not deleted:
                        logger.warning(
                            "Millennium refused to delete card %s/%s from %s", *card, pid,
                        )
                        continue
                    logger.info("Millennium: deleted card %s/%s from %s", *card, pid)
                entries.remove(e)
            if entries:
                PacsCards.record(pid, credential_id, entries)
            else:
                PacsCards.forget(pid, credential_id)

    def _new_card_cells(
        self, slot: int, entries: list[dict], holder: export.Cardholder
    ) -> dict[tuple[int, str], str]:
        """A card we write: the trigger format, the trigger card's access
        levels, activated now, and inactive until the pass is installed.

        The levels come from the trigger card while it is still there. Once
        it has been replaced, they come from the card of this pass that took
        its place, which kept them: a second card that failed and is retried
        later must not go in with none.
        """
        on_holder = {(s.facility_code, s.card_number): s for s in holder.slots if not s.empty}
        ours = [
            on_holder[(e["facility_code"], e["card_number"])]
            for e in sorted(entries, key=lambda e: e.get("pending") != "write")
            if e.get("pending") != "insert"
            and (e["facility_code"], e["card_number"]) in on_holder
        ]
        trigger = next((c for c in ours if c.access_levels), None)
        now = datetime.now(UTC) + timedelta(seconds=self._offset_seconds)
        hour = now.hour % 12 or 12
        return {
            (slot, "Card Format"): self._trigger_label,
            (slot, "Activation Date"):
                f"{now.month}/{now.day}/{now.year} {hour}:{now.minute:02d} "
                f"{'AM' if now.hour < 12 else 'PM'}",
            (slot, "Active"): "False",
            **import_file.access_level_cells(slot, trigger.access_levels if trigger else ()),
        }

    def write_back_credentials(
        self, person_id: str, credential_id: str, identities: list[CredentialIdentity]
    ) -> bool:
        """Record AccessGrid's cards for a pass that has just been issued.

        Only a pass whose trigger card is still waiting to be overwritten is
        written to; once its cards are in, later offers change nothing. The
        first card goes into the trigger card's slot, a pair's second into
        an empty one. finish_cycle sends them.
        """
        pid = str(person_id)
        holder = self._cardholders.get(pid)
        entries = PacsCards.get(pid, credential_id)
        trigger = next((e for e in entries if e.get("pending") == "write"), None)
        if holder is None or trigger is None:
            return False
        recorded = {(e["facility_code"], e["card_number"]) for e in entries}
        new = [
            i for i in identities
            if (str(i.site_code), str(i.card_number)) not in recorded
        ]
        if not new:
            return True
        taken = {
            e["slot"] for cards in PacsCards.entries_for_person(pid).values() for e in cards
        }
        empty = [s.index for s in holder.slots if s.empty and s.index not in taken]
        slots = [trigger["slot"]] + empty
        if any(e.get("pending") == "insert" for e in entries) or len(new) > len(slots):
            logger.warning(
                "Millennium: no room on cardholder %s for %d card(s) of %s — not writing",
                pid, len(new), credential_id,
            )
            return False
        for identity, slot in zip(new, slots, strict=False):
            entries.append({
                "slot": slot, "facility_code": str(identity.site_code),
                "card_number": str(identity.card_number), "pending": "insert",
            })
        PacsCards.record(pid, credential_id, entries)
        return True

    def retire_credentials(
        self, person_id: str, credential_id: str, identities: list[CredentialIdentity]
    ) -> int:
        """Mark the cards AccessGrid deleted or unlinked for deletion.

        Recorded only; finish_cycle deletes them. A dormant card is left
        alone, like the rest of a held pass.
        """
        pid = str(person_id)
        gone = {(str(i.site_code), str(i.card_number)) for i in identities}
        entries = PacsCards.get(pid, credential_id)
        marked = 0
        for e in entries:
            if (
                (e["facility_code"], e["card_number"]) in gone
                and e.get("pending") not in (*UNWRITTEN, "delete")
                and e.get("status") != "dormant"
            ):
                e["pending"] = "delete"
                marked += 1
        if marked:
            PacsCards.record(pid, credential_id, entries)
        return marked

    def written_credentials(self) -> dict[tuple[str, str], list[str]]:
        """Card numbers this integration has written into Millennium.

        Read from the ledger rather than from the cardholder pages: this is
        for a page listing every issued pass, and fetching ~90 KB per holder
        to render a one-line summary would be absurd.
        """
        out: dict[tuple[str, str], list[str]] = {}
        for key, entries in PacsCards.all().items():
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
        for credential_id, entries in PacsCards.entries_for_person(pid).items():
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
            PacsCards.forget(pid, credential_id)


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

        if PacsCards.entries_for_person(pid):
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
                PacsCards.record(pid, row["pacs_credential_id"], [{
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
        mine = PacsCards.entries_for_person(pid)
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
                PacsCards.record(pid, credential_id, entries)
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
                    PacsCards.record(pid, owner, entries)
                continue
            recorded = next(
                (e for entries in mine.values() for e in entries
                 if e["slot"] == slot.index and e.get("pending") != "insert"),
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
            if not self._has_room(pid, slots):
                continue
            credential_id, n = NEW_CREDENTIAL_ID, 2
            while credential_id in mine:
                credential_id, n = f"{NEW_CREDENTIAL_ID}-{n}", n + 1
            mine[credential_id] = [{
                "slot": slot.index, "facility_code": card[0], "card_number": card[1],
                **({"pending": "write"} if seos else {}),
            }]
            PacsCards.record(pid, credential_id, mine[credential_id])

        build = self._seos_credential if self.mode == MODE_SEOS else self._desfire_credential
        out: list[Credential] = []
        for credential_id, entries in sorted(mine.items()):
            if (
                all(e.get("pending") == "write" for e in entries)
                and not _tracked_pass(pid, credential_id)
                and not self._has_room(pid, slots)
            ):
                # Recorded but not issued yet, and the room it was recorded
                # with has since been taken. It waits until there is room.
                continue
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
                PacsCards.forget(pid, credential_id)
            if credential is not None:
                out.append(replace(credential, hold=credential_id in held))
        return out

    def _has_room(self, pid: str, slots: list[Slot]) -> bool:
        """Whether a new Seos pair pass has an empty slot for its second card."""
        needed = PAIR_EMPTY_SLOTS if self.mode == MODE_SEOS and self.pair else 0
        empty = len([s for s in slots if s.empty])
        if empty < needed:
            logger.info(
                "Millennium: skipping cardholder %s — a pass needs %d free card "
                "slot(s), found %d",
                pid, needed, empty,
            )
            return False
        return True

    @staticmethod
    def _card_recorded_elsewhere(pid: str, card: tuple[str, str]) -> bool:
        return any(
            (e["facility_code"], e["card_number"]) == card
            for key, entries in PacsCards.all().items()
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
        ledger = [e for e in entries if e.get("pending") not in UNWRITTEN]

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
        held_for_install = False
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
                if entry.get("active") is False:
                    # Off because we wrote it off, to wait for the install,
                    # and nobody has switched it on since: our own hold.
                    held_for_install = True
                    continue
                logger.info(
                    "Millennium: card %s/%s is inactive on cardholder %s — "
                    "suspending the pass",
                    key[0], key[1], pid,
                )
                status = CredentialStatus.SUSPENDED
                break
        if status is CredentialStatus.ACTIVE and held_for_install:
            logger.info(
                "Millennium: cardholder %s %s is off until the pass is installed",
                pid, credential_id,
            )
            status = CredentialStatus.AWAITING_INSTALL
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


def _tracked_pass(pid: str, credential_id: str | None = None) -> bool:
    """Whether we track a live AccessGrid pass for this cardholder (or one pass)."""
    from ....db.connection import execute_one

    sql = "SELECT 1 FROM ag_credentials WHERE pacs_person_id = ? AND status != 'deleted'"
    params: tuple = (pid,)
    if credential_id is not None:
        sql += " AND pacs_credential_id = ?"
        params += (credential_id,)
    return execute_one(sql, params) is not None


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
        "pacs.millennium_ultra.notify_email": "Notification email (used when the Millennium session expires, and for cards the sync will not act on)",
        "pacs.millennium_ultra.mode_desfire": "Your AccessGrid card template issues DESFire, so cards are read from Millennium: the facility code and card number on each trigger card are copied out to AccessGrid. The only thing changed in Millennium is the card's Active box.",
        "pacs.millennium_ultra.mode_seos": "Your AccessGrid card template issues HID Seos, so cards are written into Millennium: AccessGrid allocates the facility code and card number, and the sync engine writes them over the trigger card, and a pair's second card into an empty slot.",
        "pacs.millennium_ultra.trigger_help": (
            "Millennium Ultra has no API and its login is protected by a "
            "captcha, so this integration works through a session you open "
            "yourself: click Connect to Millennium, sign in, and the browser "
            "hands the session back encrypted. Then choose the card format "
            "that acts as the enrollment trigger — any cardholder holding a "
            "card in that format gets an AccessGrid pass on the next cycle. "
            "Cardholders are matched by Employee ID, so each needs a unique "
            "one. In DESFire mode the card's facility code and number are "
            "copied out to AccessGrid. In Seos mode AccessGrid mints them and "
            "the sync engine writes them over the trigger card, with the "
            "trigger card's access levels; a pair's second card needs one "
            "empty slot. A cardholder with no valid email address gets one "
            "synthesized from their name and Employee ID against the domain "
            "above. Suspending or resuming a pass switches the card's Active "
            "box."
        ),
    },
    "es": {
        "pacs.millennium_ultra.base_url": "URL de inicio de sesión de Millennium Ultra (p. ej. https://hosted105.mgiaccess.com)",
        "pacs.millennium_ultra.email_domain": "Dominio para las direcciones de correo sintetizadas (p. ej. cards.example.com)",
        "pacs.millennium_ultra.notify_email": "Correo de notificación (se usa cuando caduca la sesión de Millennium y para las tarjetas sobre las que la sincronización no actúa)",
        "pacs.millennium_ultra.mode_desfire": "Su plantilla de AccessGrid emite DESFire, así que las tarjetas se leen desde Millennium: el código de instalación y el número de cada tarjeta disparadora se copian a AccessGrid. Lo único que se cambia en Millennium es la casilla Activa de la tarjeta.",
        "pacs.millennium_ultra.mode_seos": "Su plantilla de AccessGrid emite HID Seos, así que las tarjetas se escriben en Millennium: AccessGrid asigna el código de instalación y el número de tarjeta, y el motor de sincronización los escribe sobre la tarjeta disparadora, y la segunda tarjeta de un par en una ranura libre.",
        "pacs.millennium_ultra.trigger_help": (
            "Millennium Ultra no tiene API y su inicio de sesión está "
            "protegido por un captcha, por lo que esta integración funciona "
            "mediante una sesión que usted mismo abre: pulse Conectar con "
            "Millennium, inicie sesión y el navegador devuelve la sesión "
            "cifrada. Después elija el formato de tarjeta que actúa como "
            "disparador de inscripción: cualquier titular con una tarjeta de "
            "ese formato recibirá un pase de AccessGrid en el siguiente "
            "ciclo. Los titulares se identifican por su Employee ID, así que "
            "cada uno necesita uno único. En modo DESFire se copian a "
            "AccessGrid el código de instalación y el número de la tarjeta. "
            "En modo Seos, AccessGrid los genera y el motor de sincronización "
            "los escribe sobre la tarjeta disparadora, con los niveles de "
            "acceso de esa tarjeta; la segunda tarjeta de un par necesita una "
            "ranura libre. Un titular sin un correo electrónico válido recibe "
            "uno sintetizado a partir de su nombre y su Employee ID con el "
            "dominio indicado arriba. Suspender o reanudar un pase cambia la "
            "casilla Activa de la tarjeta."
        ),
    },
}
