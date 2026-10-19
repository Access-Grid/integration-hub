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
        installs on both a phone and a watch needs one each. One of them is
        the marker's own — it holds a placeholder number that opens nothing,
        so the first credential overwrites it — leaving one empty slot to
        find.

  * **Every write is a full-form round-trip.** Millennium's save replaces
    the whole cardholder record, so html_form re-posts every field the page
    served and mutates only the slot in question. Access levels, photos,
    user fields and vehicle details are echoed back untouched — this
    adapter never edits a cardholder's access levels.

  * **Cardholders are people and cards at once.** This install has ~1800 of
    them, one detail page each at ~90 KB, so a full sweep costs ~5 minutes.
    We pay that once to build a profile cache, then each cycle refetch only
    the cardholders that are enrolled plus a rotating slice of the rest, so
    a newly-enrolled card is still discovered without re-reading 158 MB
    every 30 minutes. See Exporter.profile_for in export.py.

This install stores no email addresses or phone numbers, so phase 1 would
skip every cardholder for want of a delivery channel. Instead we synthesize
a deterministic address per cardholder from their name and primary key
against an operator-supplied domain; distribution happens through the
/credentials page's QR code and install URL.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime

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
from .client import (
    ANTIFORGERY_COOKIE_PREFIX,
    COMPANY_COOKIE,
    TIMEOFFSET_COOKIE,
    MillenniumUltraClient,
)
from .export import (
    DEFAULT_SWEEP_BUDGET,
    Exporter,
    Slot,
    describe_slots,
    first_valid_email,
    format_datetime,
    parse_roster_name,
    read_slots,
)
from .html_form import CardholderForm

logger = logging.getLogger(__name__)

MODE_DESFIRE = "desfire"
MODE_SEOS = "seos"

# The credential id for a cardholder's Seos pass. Deliberately carries no
# slot number: cards we write take the trigger format, so one landing below
# the operator's marker used to move the id — and a moved id reads as a new
# credential, which phase 1 provisions a second pass for. There is one Seos
# credential per cardholder, so there is nothing for a position to identify.
SEOS_CREDENTIAL_ID = "seos"

# The id for a pass beyond a cardholder's first, suffixed with the Millennium
# CardID of the card that asked for it. The slot cannot serve: cards move
# between positions, and a moved id reads as a new credential, which phase 1
# provisions a second pass for — the bug migration 005 was written to stop.
# The card number cannot either, because provisioning overwrites it. The
# CardID survives both: it is preserved deliberately when a marker is
# overwritten, so that Millennium updates the card rather than creating a
# second one beside it.
#
# Cardholders already tracked under the bare "seos" keep it. Nothing is
# migrated, so nothing has to read the cardholder page to learn an id it
# never had.
_SEOS_CREDENTIAL_PREFIX = "seos-card"


def _credential_id_for(slot: Slot) -> str:
    return f"{_SEOS_CREDENTIAL_PREFIX}{slot.card_id}"


def _is_seos_credential(credential_id: str) -> bool:
    return credential_id == SEOS_CREDENTIAL_ID or credential_id.startswith(
        _SEOS_CREDENTIAL_PREFIX
    )


# Millennium gives three card slots. A pass can hold two cards — a phone and
# a watch — so two passes is already more than fits comfortably, and a third
# could not be installed. The cap is what stops an id that moves, or a
# trigger that matches more cards than it should, issuing a pass per cycle.
MAX_SEOS_CREDENTIALS = 2

# Seos needs this many *empty* slots before we will provision. A phone and a
# watch consume one each, and the marker's own slot is the other — we
# overwrite it, since it holds a placeholder rather than a working card. Of
# Millennium's three slots per cardholder that leaves one to find free.
REQUIRED_EMPTY_SLOTS = 1


def synthesize_email(first: str, last: str, cardholder_id: str, domain: str) -> str:
    """Deterministic address for an install that stores none.

    The cardholder's primary key is part of the local part because this
    install carries genuine duplicates — the same human appears once per
    card technology — and each is a separate pass.
    """
    domain = (domain or "").strip().lstrip("@")
    if not domain:
        return ""
    parts = [re.sub(r"[^a-z0-9]", "", (p or "").lower()) for p in (first, last)]
    local = ".".join([p for p in parts if p] + [str(cardholder_id)])
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
    def note_active(person_id: str, credential_id: str, states: dict[int, bool]) -> None:
        """Remember the Active box we last wrote, per card.

        Millennium's checkbox carries no reason. A card we switched off
        because nobody had installed the pass yet looks exactly like one an
        operator switched off to revoke somebody, and the install gate
        reads the second as the first and switches it back on. What we
        wrote is the only thing that tells them apart.
        """
        data = SeosLedger._load_all()
        key = SeosLedger._key(person_id, credential_id)
        entries = data.get(key)
        if not entries:
            return
        for entry in entries:
            try:
                slot = int(entry.get("slot"))
            except (TypeError, ValueError):
                continue
            if slot in states:
                entry["active"] = bool(states[slot])
        data[key] = entries
        SeosLedger._save_all(data)

    @staticmethod
    def last_written_active(person_id: str, credential_id: str) -> dict[int, bool]:
        """Slot -> the Active box we last wrote there.

        A slot is absent whenever we do not actually know: no entry, an
        entry from before this was recorded, a value that is not a
        true/false, a slot number that will not parse, or a ledger we
        could not read at all. Absence means "no opinion", never "off" —
        reading a missing record as "we set it off" would make the install
        hand-off refuse itself.

        Only real booleans count. A string "false" is not a False, and
        coercing one with bool() would read it as True and then refuse to
        switch a card on that nobody had switched off. The slot number is
        read the same way: int(1.5) is 1, and a record we had to round to
        reach is not a record of anything.
        """
        out: dict[int, bool] = {}
        try:
            entries = SeosLedger.get(person_id, credential_id)
        except Exception as e:  # noqa: BLE001 — an unreadable ledger is "unknown"
            logger.warning(
                "Millennium: could not read what we last set active for %s/%s "
                "(%s) — treating it as unknown",
                person_id, credential_id, e,
            )
            return out
        for entry in entries:
            active = entry.get("active") if isinstance(entry, dict) else None
            if not isinstance(active, bool):
                continue
            slot = entry.get("slot")
            # bool is an int in Python, and True would read as slot 1.
            if isinstance(slot, bool) or not isinstance(slot, int | str):
                continue
            try:
                out[int(slot)] = active
            except (TypeError, ValueError):
                continue
        return out

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
        mode: str = MODE_DESFIRE,
        auth_cookie: str = "",
        company_name: str = "",
        time_offset: str = "",
        request_token: str = "",
        request_token_cookie: str = "",
        sweep_budget: int = DEFAULT_SWEEP_BUDGET,
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
        # Reading Millennium: the roster, the bulk export and the detail
        # pages, with the caches that keep a cycle from reading them all.
        # The client is looked up rather than handed over, so a client
        # swapped onto the adapter is the one the export reads through.
        self._export = Exporter(
            client=lambda: self._client,
            is_trigger=self._is_trigger,
            trigger_label=self._trigger_format_name,
            offset_seconds=self._offset_seconds,
            sweep_budget=sweep_budget,
        )
        # The trigger format's display name, resolved once. "" means looked
        # for and not found; None means not looked for yet.
        self._trigger_format_label: str | None = None
        # Cardholders the ledger says we hold cards on, refreshed once per
        # cycle. Diagnostics use it to tell "a stranger we skipped" from
        # "somebody we have written a credential to and then lost sight of",
        # which are the same silence without it.
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
        self._ours = {key.partition(":")[0] for key in SeosLedger.all()}
        roster = self._export.read_roster(self._ours)

        for row in roster:
            pid = str(row.get("ID"))
            first, last = parse_roster_name(row.get("Name", ""))
            profile = self._export.profiles.get(pid)
            if profile:
                first = profile.get("first") or first
                last = profile.get("last") or last
            full_name = " ".join(p for p in (first, last) if p)
            exported_email, exported_phone = self._export.contacts.get(pid, ("", ""))
            # The detail page wins where we have read one, the same
            # precedence the name uses. The export covers the cycle before
            # that, which is the cycle a cardholder is provisioned on.
            stored_email = (profile or {}).get("email") or first_valid_email(
                exported_email
            )
            yield Person(
                id=pid,
                full_name=full_name,
                first_name=first,
                last_name=last,
                # Only invent one when the install holds none. A real address
                # is the holder's, and the synthesized one is a stand-in that
                # cannot receive anything.
                email=stored_email or synthesize_email(
                    first, last, pid, self.email_domain
                ),
                phone=(profile or {}).get("phone") or exported_phone,
                # Millennium has no cardholder-level enable flag; the
                # roster's IsActive only marks the row the UI has selected.
                active=True,
                raw=row,
            )
        self._export.finish_roster()

    def _trigger_format_name(self) -> str:
        """The display name the export prints for the configured trigger.

        The trigger is stored as Millennium's numeric format id; the export
        prints the name. The <select> on any cardholder page carries both,
        so the pair is read once and kept for the life of the adapter —
        formats are install-level configuration, not per-cycle state.
        """
        if self._trigger_format_label is None:
            self._trigger_format_label = ""
            try:
                for fid, label in self.card_formats():
                    if str(fid) == self.trigger_card_format:
                        self._trigger_format_label = label
                        break
            except Exception as e:  # noqa: BLE001 — falls back to the sweep
                logger.debug("Millennium: could not read the format list: %s", e)
        return self._trigger_format_label

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        pid = str(person_id)
        profile = self._export.profile_for(pid)
        if profile is None:
            # Unreadable is not the same as empty: answering [] here would
            # let phase 3 read a dropped connection — or a cardholder we
            # deliberately skipped — as a deleted credential.
            raise PacsRecordUnavailable(
                f"cardholder {pid} was not read this cycle"
            )
        slots: list[Slot] = profile["slots"]
        triggers = [s for s in slots if self._is_trigger(s)]
        if not triggers:
            if pid in self._ours:
                # Expected for a namesake the export's name join swept in;
                # not expected for somebody we have written a card to, whose
                # card carries the trigger format itself.
                logger.warning(
                    "Millennium: cardholder %s holds cards we wrote but no slot "
                    "carries format %s — reporting no credentials. Slots: %s",
                    pid, self.trigger_card_format, describe_slots(slots),
                )
                # And the record of those cards is now false, so drop it.
                # Phase 3 deletes the pass off the back of this same empty
                # answer; left recorded, the cards would suspend whatever
                # pass a new trigger card earned next, because a recorded
                # card missing from the cardholder is how an operator's
                # revocation is detected.
                #
                # Safe precisely here and nowhere else: our own cards carry
                # the trigger format, so a cardholder with no trigger slot
                # cannot be holding one. Confirmed against the slots anyway,
                # since it is the ledger that makes a card ours to release.
                self._forget_if_nothing_remains(pid, slots)
            return []

        if self.mode == MODE_DESFIRE:
            return [self._desfire_credential(pid, s) for s in triggers]
        return self._seos_credentials(pid, slots, triggers[0])

    def update_credential_status(
        self, person_id: str, credential_id: str, status: CredentialStatus
    ) -> bool:
        """Suspend or resume by toggling the card's Active checkbox.

        Millennium has no other switch: an unchecked Card_N_Active is an
        inactive card. The card keeps its number, dates and access levels.

        A credential still awaiting its install unticks the same box — the
        two are one state here, however different their reasons.
        """
        if status not in (
            CredentialStatus.ACTIVE,
            CredentialStatus.SUSPENDED,
            CredentialStatus.AWAITING_INSTALL,
        ):
            return False
        want_active = status == CredentialStatus.ACTIVE
        pid = str(person_id)
        form = self._client.get_cardholder_form(pid)
        current = read_slots(form, self._offset_seconds)
        slots = self._slots_for_credential(pid, credential_id, current)
        if not slots:
            logger.warning(
                "Millennium: no card backs credential %s/%s — not writing",
                pid, credential_id,
            )
            return False

        by_index = {s.index: s for s in current}
        if want_active:
            # An operator unticking a card is how a pass is revoked here.
            # Switching it back on because AccessGrid still shows the pass
            # installed overrules the person holding the console, and does
            # it silently, every cycle. Only refuse where we have a record
            # of having set that card on ourselves: without one we cannot
            # tell a revocation from a card that has simply always been
            # off, and refusing then would stop the install hand-off.
            recorded = SeosLedger.last_written_active(pid, credential_id)
            switched_off = [
                index for index in slots
                if recorded.get(index) is True
                and form.find(f"Card_{index}_Active") is not None
                and not form.is_checked(f"Card_{index}_Active")
            ]
            if switched_off:
                logger.warning(
                    "Millennium: cardholder %s credential %s was switched off "
                    "in Millennium after we last switched it on (slot(s) %s) — "
                    "leaving it off rather than overruling the operator",
                    pid, credential_id,
                    ", ".join(str(i) for i in switched_off),
                )
                return False

        writable = 0
        changed = False
        confirmed: list[int] = []
        for index in slots:
            field = f"Card_{index}_Active"
            slot = by_index.get(index)
            if form.find(field) is None or slot is None or slot.empty:
                # The card behind this credential is gone. Activating an
                # empty slot would be rejected by Millennium's own
                # validation anyway; report the failure and let phase 3
                # reconcile the disappearance.
                logger.warning(
                    "Millennium: slot %s on cardholder %s holds no card — "
                    "not writing status",
                    index, pid,
                )
                continue
            writable += 1
            confirmed.append(index)
            if form.is_checked(field) != want_active:
                form.set_checked(field, want_active)
                changed = True
        if not writable:
            return False
        if not changed:
            # Already as asked. Recorded anyway: this is how a card that
            # nobody has touched since the upgrade acquires a record, and
            # until it has one an operator's untick cannot be recognised.
            if not dryrun.active():
                SeosLedger.note_active(
                    pid, credential_id, dict.fromkeys(confirmed, want_active)
                )
            return True
        if dryrun.active():
            dryrun.would(
                "set cardholder %s credential %s to %s",
                pid, credential_id, "active" if want_active else "inactive",
            )
            return True
        ok = self._client.save_cardholder(pid, form)
        if ok:
            SeosLedger.note_active(
                pid, credential_id, dict.fromkeys(confirmed, want_active)
            )
            self._export.drop_profile(pid, "changing a card's Active box")
        return ok

    @property
    def supports_status_writeback(self) -> bool:
        return True

    @property
    def supports_credential_writeback(self) -> bool:
        """Seos mints credentials in AccessGrid and writes them into the PACS."""
        return self.mode == MODE_SEOS

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

    def write_back_credentials(
        self,
        person_id: str,
        credential_id: str,
        identities: list[CredentialIdentity],
    ) -> bool:
        """Write AccessGrid-allocated cards into the cardholder's empty slots.

        Called once per newly-issued identity — a pass installed on both a
        phone and a watch yields two, which is why provisioning demands two
        free slots up front.

        A credential is written **once and only once**. If it is later gone
        from the cardholder, somebody removed it in Millennium, and that
        decision is theirs: re-creating it would mean this integration
        overruling an operator working in their own system, and a card they
        deliberately revoked would come back by itself. So the ledger of what
        we have already written is authoritative, and only identities missing
        from *both* the cardholder and the ledger are ever written.
        """
        if self.mode != MODE_SEOS or not identities:
            return False
        pid = str(person_id)
        form = self._client.get_cardholder_form(pid)
        slots = read_slots(form, self._offset_seconds)
        existing = {(s.facility_code, s.card_number) for s in slots if not s.empty}

        written = list(SeosLedger.get(pid, credential_id))
        already_written = {
            (str(e.get("facility_code")), str(e.get("card_number"))) for e in written
        }

        # The marker goes first. It is a placeholder the operator created to
        # ask for a pass — a trigger-format card holding a number that opens
        # nothing — so leaving it in place would spend one of three slots on
        # a card that will never be used, and a cardholder needs two real
        # ones for a phone and a watch.
        # Every card of ours on this cardholder, not just this credential's.
        # A card belonging to another of their credentials is still ours, and
        # calling it a marker would overwrite a working credential in
        # preference to an empty slot.
        marker = self._marker_slot(slots, SeosLedger.cards_for_person(pid))
        # Read before anything is written, so a slot we fill cannot become
        # the source for the next one.
        levels = self._access_levels(form, slots)
        free = [s.index for s in slots if s.empty]
        if marker is not None:
            free.insert(0, marker.index)
        pending = []
        for identity in identities:
            key = (str(identity.site_code), str(identity.card_number))
            if key in existing:
                continue
            if key in already_written:
                logger.warning(
                    "Millennium: card %s/%s was written for cardholder %s and has "
                    "since been removed there — leaving it removed",
                    identity.site_code, identity.card_number, pid,
                )
                continue
            pending.append(identity)
        if not pending:
            # The ordinary case every cycle after the last card landed, and
            # also the case where a watch's credential never arrives. They
            # are indistinguishable without saying what was on offer.
            logger.info(
                "Millennium: nothing new to write for cardholder %s — offered %s; "
                "on the cardholder %s; already written %s",
                pid,
                ", ".join(f"{i.site_code}/{i.card_number}" for i in identities),
                ", ".join(f"{fc}/{n}" for fc, n in sorted(existing)) or "nothing",
                ", ".join(f"{fc}/{n}" for fc, n in sorted(already_written)) or "nothing",
            )
            return False

        if len(pending) > len(free):
            # Recoverable rather than broken: the abandoned half of a pair
            # may still be occupying a slot, and phase 3 releases it once
            # AccessGrid reports it deleted. Written next cycle, then.
            logger.warning(
                "Millennium: cardholder %s has %d free slot(s) but %d credential(s) "
                "to write — writing none this cycle",
                pid, len(free), len(pending),
            )
            return False

        for identity in pending:
            slot = free.pop(0)
            # Overwriting keeps the existing CardID so Millennium updates
            # that card rather than creating a second one in the same slot.
            replacing = marker.card_id if marker and slot == marker.index else ""
            if not self._client.card_number_is_free(
                slot, str(identity.card_number), str(identity.site_code),
                self.trigger_card_format,
            ):
                logger.error(
                    "Millennium: card %s/%s is already in use — not writing for %s",
                    identity.site_code, identity.card_number, pid,
                )
                return False
            self._fill_slot(
                form, slot, identity, card_id=replacing, access_levels=levels,
            )
            written.append({
                "slot": slot,
                "card_number": str(identity.card_number),
                "facility_code": str(identity.site_code),
            })

        if dryrun.active():
            dryrun.would(
                "write %d credential(s) into cardholder %s: %s",
                len(pending), pid,
                ", ".join(f"{i.site_code}/{i.card_number}" for i in pending),
            )
            # The ledger is not recorded either: it would then name cards
            # that are not on the cardholder, which is how a later cycle
            # decides a pass has been revoked.
            return True
        if not self._client.save_cardholder(pid, form):
            return False
        SeosLedger.record(pid, credential_id, written)
        self._export.drop_profile(pid, "writing credentials")
        logger.info(
            "Millennium: wrote %d credential(s) into cardholder %s: %s",
            len(pending), pid,
            ", ".join(f"{i.site_code}/{i.card_number}" for i in pending),
        )
        return True

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

    @property
    def supports_credential_retirement(self) -> bool:
        """Only Seos cards are ours to remove; DESFire cards are the customer's."""
        return self.mode == MODE_SEOS

    def retire_credentials(
        self,
        person_id: str,
        credential_id: str,
        identities: list[CredentialIdentity],
    ) -> int:
        """Release the slots holding credentials AccessGrid has deleted.

        Issuing against a card template pair allocates one credential per
        platform and the holder installs exactly one; AccessGrid deletes the
        other. Without this the abandoned card occupies a slot forever, and
        Millennium gives each cardholder only three — so the holder's watch
        would later have nowhere to go.

        The ledger entry is dropped in the same operation as the card. Those
        two facts are read together by `_seos_credentials`, which suspends a
        pass when a card it recorded is missing from the cardholder: leaving
        the entry behind would suspend a working pass on the next cycle,
        because nothing downstream can tell our deletion from an operator's.
        """
        if self.mode != MODE_SEOS or not identities:
            return 0
        pid = str(person_id)
        written = list(SeosLedger.get(pid, credential_id))
        if not written:
            return 0

        recorded = {
            (str(e.get("facility_code")), str(e.get("card_number"))): e for e in written
        }
        wanted = [
            recorded[key]
            for key in (
                (str(i.site_code), str(i.card_number)) for i in identities
            )
            if key in recorded
        ]
        if not wanted:
            return 0

        form = self._client.get_cardholder_form(pid)
        slots = {
            (s.facility_code, s.card_number): s
            for s in read_slots(form, self._offset_seconds)
            if not s.empty
        }
        token = form.value("__RequestVerificationToken")
        # Read from the cardholder rather than the credential id, which no
        # longer carries a position.
        marker = self._marker_slot(
            read_slots(form, self._offset_seconds), set(recorded)
        )
        trigger_slot = marker.index if marker else None

        retired = 0
        for entry in wanted:
            key = (str(entry.get("facility_code")), str(entry.get("card_number")))
            slot = slots.get(key)
            if slot is None:
                # Already gone from Millennium. Still drop the ledger entry:
                # the card is not coming back, and leaving it recorded would
                # suspend the pass for a card AccessGrid deleted anyway.
                logger.info(
                    "Millennium: card %s/%s already absent from cardholder %s — "
                    "dropping it from the ledger",
                    key[0], key[1], pid,
                )
                written.remove(entry)
                retired += 1
                continue
            if slot.index == trigger_slot:
                # Cannot happen — the trigger card is the customer's own and
                # is never recorded — but this is a destructive operation.
                #
                # Note the test is the slot, not the format: cards we write
                # carry the trigger format themselves, so `_is_trigger` is
                # true of our own cards and would refuse every retirement.
                logger.error(
                    "Millennium: refusing to delete the trigger card in slot %d "
                    "of cardholder %s",
                    slot.index, pid,
                )
                continue
            if not slot.card_id:
                logger.warning(
                    "Millennium: slot %d of cardholder %s has no CardID — "
                    "not deleting card %s/%s",
                    slot.index, pid, key[0], key[1],
                )
                continue

            logger.info(
                "Millennium: deleting card %s/%s from slot %d of cardholder %s — "
                "AccessGrid deleted it",
                key[0], key[1], slot.index, pid,
            )
            if dryrun.active():
                dryrun.would(
                    "delete card %s/%s from slot %d of cardholder %s",
                    key[0], key[1], slot.index, pid,
                )
                continue
            if not self._client.delete_card(pid, slot.card_id, token):
                logger.error(
                    "Millennium: delete refused for card %s/%s on cardholder %s",
                    key[0], key[1], pid,
                )
                continue
            written.remove(entry)
            retired += 1

        if retired:
            SeosLedger.record(pid, credential_id, written)
            self._export.drop_profile(pid, "retiring credentials")
        return retired

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
            bool(self.trigger_card_format)
            and slot.card_format == self.trigger_card_format
            and not slot.empty
        )

    def _access_levels(self, form: CardholderForm, slots: list[Slot]) -> str:
        """The access levels a card we are about to write should carry.

        Taken from the trigger-format cards already on the cardholder: the
        marker an operator created to ask for a pass, or — once that has been
        overwritten — the card we wrote in its place, which inherited them.
        The second is what a watch credential arriving cycles later needs,
        by which time no marker is left to read.

        Copied verbatim rather than parsed:

            {"0":{"200":{"ALID":2,"AD":null,"ED":null},
                  "201":{"ALID":1,"AD":null,"ED":null}}}

        Tenant, then the id of the "Access Level N" column the level sits in
        — 200 to 209 for the ten of them, the same ids the bulk export takes
        — then the level itself and its own dates. Those keys mean the same
        thing on every card, which is what makes moving the blob between
        slots sound. Re-encoding a structure we do not own is how a working
        access level becomes a subtly broken one.
        """
        for slot in slots:
            if not self._is_trigger(slot):
                continue
            levels = form.value(f"Card_{slot.index}_AccessLevels").strip()
            # An unset field is "{}" here rather than empty.
            if levels and levels not in ("{}", "[]"):
                return levels
        return ""

    def _marker_slot(
        self, slots: list[Slot], already_written: set[tuple[str, str]]
    ) -> Slot | None:
        """The placeholder an operator created to ask for a pass.

        A trigger-format card that is not one of ours. Cards we write take
        the trigger format too, so the ledger is what tells the two apart —
        without it we would overwrite a credential we had just issued.
        """
        return next(
            (
                s
                for s in slots
                if self._is_trigger(s)
                and (str(s.facility_code), str(s.card_number)) not in already_written
            ),
            None,
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
            raw={"slot": slot.index, "card_id": slot.card_id},
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
        whatever id it was issued under. Every trigger-format card the ledger
        does *not* know about is an operator asking for one, keyed by the
        card's own id in Millennium.
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

        # Then anything being asked for. A card we never wrote cannot be one
        # of ours — ours carry the trigger format, which is exactly why the
        # ledger has to answer this and not the format.
        #
        # A cardholder's first pass keeps the bare "seos" id, as it always
        # has. Only a second one is keyed on a CardID, so nothing about the
        # ordinary case starts depending on that id staying put.
        used = set(existing)
        for slot in slots:
            if not self._is_trigger(slot):
                continue
            if (slot.facility_code, slot.card_number) in ours:
                continue
            if SEOS_CREDENTIAL_ID in used and not slot.card_id:
                # Nothing to key a second credential on. Millennium assigns a
                # CardID when a card is saved, so this means a card we are
                # seeing before it exists properly.
                logger.warning(
                    "Millennium: cardholder %s has a trigger card in slot %d "
                    "with no CardID — cannot issue a second pass against it",
                    pid, slot.index,
                )
                continue
            if len(out) >= MAX_SEOS_CREDENTIALS:
                # Rationed for the same reason provisioning is: a trigger
                # that suddenly matches more cards than it should would
                # otherwise issue a pass for each, every cycle.
                logger.warning(
                    "Millennium: cardholder %s already has %d Seos pass(es) — "
                    "not issuing another for the card in slot %d (%s/%s)",
                    pid, len(out), slot.index,
                    slot.facility_code, slot.card_number,
                )
                break
            credential_id = (
                SEOS_CREDENTIAL_ID
                if SEOS_CREDENTIAL_ID not in used
                else _credential_id_for(slot)
            )
            used.add(credential_id)
            credential = self._seos_credential(
                pid, credential_id, [], slots, trigger
            )
            if credential is not None:
                out.append(credential)
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
            describe_slots(slots),
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

    def _slots_for_credential(
        self, pid: str, credential_id: str, slots: list[Slot] | None = None
    ) -> list[int]:
        """Which Millennium slot(s) a tracked credential currently occupies.

        For a Seos credential the ledger's slot number is only where the card
        was first written; operators move cards between slots. Resolve by
        card identity against the current cardholder when we have it, so a
        suspend never lands on whatever happens to sit in the old position.
        """
        if _is_seos_credential(credential_id):
            ledger = SeosLedger.get(pid, credential_id)
            if slots is None:
                return [int(e["slot"]) for e in ledger]
            by_identity = {
                (s.facility_code, s.card_number): s.index
                for s in slots if not s.empty
            }
            found = []
            for entry in ledger:
                key = (str(entry.get("facility_code")), str(entry.get("card_number")))
                if key in by_identity:
                    found.append(by_identity[key])
            return found
        match = re.fullmatch(r"slot(\d+)", credential_id)
        return [int(match.group(1))] if match else []

    def _fill_slot(
        self,
        form: CardholderForm,
        slot: int,
        identity: CredentialIdentity,
        card_id: str = "",
        access_levels: str = "",
    ) -> None:
        """Turn a slot into a live card.

        Leaving Card_N_CardID empty is what tells Millennium to create a card
        rather than update one; it assigns the id itself on save. Pass the
        existing id to overwrite a card in place instead — replacing the
        marker mutates the one card rather than leaving the old one adrift.
        """
        form.set_value(f"Card_{slot}_CardID", card_id)
        form.set_value(f"Card_{slot}_EncodedCardNumber", str(identity.card_number))
        form.set_value(f"Card_{slot}_FacilityCode", str(identity.site_code))
        form.set_value(f"Card_{slot}_CardFormat", self.trigger_card_format)
        if identity.activate_date:
            form.set_value(
                f"Card_{slot}_ActivationDate",
                format_datetime(identity.activate_date, self._offset_seconds),
            )
        # The expiry is deliberately not written. Millennium owns that field,
        # and AccessGrid applies a default of its own when issuing — so
        # writing it back stamped a date nobody chose onto the card, and
        # phase 3 would later delete the pass for reaching it.
        #
        # Not clearing it either: the form already carries whatever the slot
        # held, so an empty slot stays empty and the marker we overwrite
        # keeps the date an operator gave it.
        if access_levels:
            # Which doors the card opens. Every other per-slot field rides
            # along in the form untouched, and that is enough for the card
            # that replaces the marker — it inherits the marker's levels by
            # sitting in its slot. A card going into an empty slot inherits
            # that slot's nothing, so a holder's phone opened every door and
            # their watch opened none.
            form.set_value(f"Card_{slot}_AccessLevels", access_levels)

        # Millennium requires both dates on an active card — the page marks
        # them conditionalrequired on Card_N_Active — and ticking Active
        # without them is answered with its generic "System is temporarily
        # unavailable" page rather than a validation message. Every cycle,
        # for as long as the marker exists, read on this side as a bad
        # field nobody could name.
        #
        # An activation date we can supply: it is ours to choose and the
        # identity usually carries one. An expiry we cannot. Phase 3
        # deletes a pass whose card has expired, so inventing one would
        # revoke a working pass on a date nobody set — see
        # test_no_expiry_is_invented_for_a_card_millennium_left_blank.
        #
        # So a slot with no expiry is written, and left inactive, and said
        # out loud. One line an operator can act on beats a save refused
        # forever for a reason nothing reports.
        activation = f"Card_{slot}_ActivationDate"
        if not form.value(activation):
            form.set_value(
                activation,
                format_datetime(
                    identity.activate_date or datetime.now(UTC),
                    self._offset_seconds,
                ),
            )
        if not form.value(f"Card_{slot}_ExpirationDate"):
            logger.warning(
                "Millennium: slot %d holds no expiration date, so card %s/%s "
                "is written inactive — Millennium refuses an active card "
                "without one. Give that card an expiry and it activates on "
                "the next cycle.",
                slot, identity.site_code, identity.card_number,
            )
            # Unticked, not merely left alone: the marker we are
            # overwriting may already be active, and echoing that back
            # beside a changed card number is the same invalid save.
            form.set_checked(f"Card_{slot}_Active", False)
            return
        form.set_checked(f"Card_{slot}_Active", True)


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
            "from the name and record id against the domain above; hand the "
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
