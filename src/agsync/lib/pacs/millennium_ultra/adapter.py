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
from . import export
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

# The credential id for a cardholder's Seos pass.
SEOS_CREDENTIAL_ID = "seos"

# Seos needs this many *empty* slots before we will provision. A phone and a
# watch consume one each, and the marker's own slot is the other.
REQUIRED_EMPTY_SLOTS = 1


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
    """Which Millennium slots hold the AccessGrid-allocated cards.

    In Seos mode the card does not exist in Millennium until we write it, so
    there is nothing on the cardholder that says "this slot belongs to
    AccessGrid". Stamping a marker into a customer-visible field would be a
    side effect on their data, so we keep the mapping on our side instead.
    """

    KEY = "millennium_seos_slots"

    @staticmethod
    def _load_all() -> dict[str, list[dict]]:
        from ....settings_store import get_json

        return get_json(SeosLedger.KEY) or {}

    @staticmethod
    def _save_all(data: dict[str, list[dict]]) -> None:
        from ....settings_store import set_json

        set_json(SeosLedger.KEY, data)

    @staticmethod
    def _key(person_id: str, credential_id: str) -> str:
        return f"{person_id}:{credential_id}"

    @staticmethod
    def all() -> dict[str, list[dict]]:
        """The whole ledger, keyed "person_id:credential_id"."""
        return SeosLedger._load_all()

    @staticmethod
    def get(person_id: str, credential_id: str) -> list[dict]:
        return SeosLedger._load_all().get(SeosLedger._key(person_id, credential_id), [])

    @staticmethod
    def record(person_id: str, credential_id: str, entries: list[dict]) -> None:
        """Replace the recorded slots, keeping one entry per physical card.

        A card that was deleted in Millennium gets rewritten into a fresh
        slot, so the same facility code + card number can be recorded twice
        over a credential's life; the later slot is the live one.
        """
        deduped: dict[tuple[str, str], dict] = {}
        for entry in entries:
            deduped[(str(entry.get("facility_code")), str(entry.get("card_number")))] = entry

        data = SeosLedger._load_all()
        key = SeosLedger._key(person_id, credential_id)
        # What we last wrote to a card's Active box survives a re-record of
        # the same card. Recording happens whenever the slots are rewritten,
        # and dropping the flag there would quietly return the install gate
        # to overruling whoever unticked it.
        known = {
            (str(e.get("facility_code")), str(e.get("card_number"))): e["active"]
            for e in data.get(key, [])
            if "active" in e
        }
        for card, entry in deduped.items():
            if "active" not in entry and card in known:
                entry["active"] = known[card]
        data[key] = list(deduped.values())
        SeosLedger._save_all(data)

    @staticmethod
    def entries_for_person(person_id: str) -> dict[str, list[dict]]:
        """Every credential recorded for a cardholder, keyed by credential id.

        A cardholder can hold more than one Seos pass, and each has its own
        entry. Returned together because deciding what a cardholder has, and
        what is being asked for, needs all of them at once.
        """
        prefix = f"{person_id}:"
        return {
            key[len(prefix):]: entries
            for key, entries in SeosLedger._load_all().items()
            if key.startswith(prefix)
        }

    @staticmethod
    def cards_for_person(person_id: str) -> set[tuple[str, str]]:
        """Every card recorded for a cardholder, across all their credentials.

        One cardholder can hold more than one Seos credential, and a card
        belonging to any of them is still ours. Asked per credential — which
        is right for "has this one been revoked" — it is wrong for "is this
        card somebody's marker", because another credential's card answers no.
        """
        prefix = f"{person_id}:"
        return {
            (str(e.get("facility_code")), str(e.get("card_number")))
            for key, entries in SeosLedger._load_all().items()
            if key.startswith(prefix)
            for e in entries
        }

    @staticmethod
    def forget(person_id: str, credential_id: str) -> None:
        data = SeosLedger._load_all()
        if data.pop(SeosLedger._key(person_id, credential_id), None) is not None:
            SeosLedger._save_all(data)



class MillenniumUltraAdapter:
    def __init__(
        self,
        base_url: str = "",
        email_domain: str = "",
        notify_email: str = "",
        trigger_card_format: str = "",
        trigger_card_format_label: str = "",
        mode: str = MODE_DESFIRE,
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
        triggers = [s for s in slots if self._is_trigger(s)]
        if not triggers:
            if pid in self._ours:
                # Our own cards carry the trigger format, so a cardholder with
                # no trigger slot holds none of them; drop the record of them.
                self._forget_if_nothing_remains(pid, slots)
            return []

        if self.mode == MODE_DESFIRE:
            return [self._desfire_credential(pid, s) for s in triggers]
        return self._seos_credentials(pid, slots, triggers[0])

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

    def _desfire_credential(self, pid: str, slot: Slot) -> Credential:
        return Credential(
            id=f"slot{slot.index}",
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

    def _seos_credentials(
        self, pid: str, slots: list[Slot], trigger: Slot
    ) -> list[Credential]:
        """One credential per pass this cardholder holds or has asked for.

        A cardholder used to have exactly one. They need two for the case a
        watch creates: Wallet will not add a credential to a watch after the
        pass is installed, and the original link cannot be replayed, so the
        only way to reach the watch is a second pass that the holder installs
        on it alone — while the phone's keeps working untouched.

        Two kinds of group, and the ledger separates them. Every entry it
        holds for this cardholder is a pass we have already issued, keyed by
        whatever id it was issued under. A trigger-format card the ledger
        does *not* know about is an operator asking for one.
        """
        # Entries with nothing in them are not passes. `retire_credentials`
        # re-records the list after removing a card, so a credential whose
        # last card AccessGrid deleted leaves an empty entry behind. Counted
        # as a credential it would hold its id while contributing nothing,
        # and the cardholder's trigger card would then be read as asking for
        # a *second* pass — one card, two passes.
        existing = {
            credential_id: entries
            for credential_id, entries in SeosLedger.entries_for_person(pid).items()
            if entries
        }
        ours = {
            (str(e.get("facility_code")), str(e.get("card_number")))
            for entries in existing.values()
            for e in entries
        }

        out: list[Credential] = []
        for credential_id, ledger in sorted(existing.items()):
            credential = self._seos_credential(
                pid, credential_id, ledger, slots, trigger
            )
            if credential is not None:
                out.append(credential)

        # Then anything being asked for: a trigger card we never wrote. Only
        # the first gets an id. Further passes were keyed on the card's
        # CardID, which the export does not carry.
        for slot in slots:
            if not self._is_trigger(slot):
                continue
            if (slot.facility_code, slot.card_number) in ours:
                continue
            if SEOS_CREDENTIAL_ID in existing:
                logger.warning(
                    "Millennium: cardholder %s has another trigger card in slot %d "
                    "— no id to issue a second pass under",
                    pid, slot.index,
                )
                continue
            credential = self._seos_credential(
                pid, SEOS_CREDENTIAL_ID, [], slots, trigger
            )
            if credential is not None:
                out.append(credential)
            break
        return out

    def _seos_credential(
        self,
        pid: str,
        credential_id: str,
        ledger: list[dict],
        slots: list[Slot],
        trigger: Slot,
    ) -> Credential | None:
        empty = [s for s in slots if s.empty]

        if not ledger and len(empty) < REQUIRED_EMPTY_SLOTS:
            logger.info(
                "Millennium: skipping cardholder %s — Seos needs %d free card slot(s), "
                "found %d",
                pid, REQUIRED_EMPTY_SLOTS, len(empty),
            )
            return None

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
                "trigger_slot": trigger.index,
                "written": [
                    f"{e.get('facility_code')}/{e.get('card_number')}" for e in ledger
                ],
            },
        )


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
        "pacs.millennium_ultra.base_url": "Millennium Ultra login URL (e.g. https://hosted8.mgiaccess.com)",
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
        "pacs.millennium_ultra.base_url": "URL de inicio de sesión de Millennium Ultra (p. ej. https://hosted8.mgiaccess.com)",
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

