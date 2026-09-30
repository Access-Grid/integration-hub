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
    every 30 minutes. See _profile_for.

This install stores no email addresses or phone numbers, so phase 1 would
skip every cardholder for want of a delivery channel. Instead we synthesize
a deterministic address per cardholder from their name and primary key
against an operator-supplied domain; distribution happens through the
/credentials page's QR code and install URL.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
    CARD_SLOTS,
    MillenniumAuthError,
    MillenniumUltraClient,
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

# Seos needs this many *empty* slots before we will provision. A phone and a
# watch consume one each, and the marker's own slot is the other — we
# overwrite it, since it holds a placeholder rather than a working card. Of
# Millennium's three slots per cardholder that leaves one to find free.
REQUIRED_EMPTY_SLOTS = 1

# Cardholders whose detail page we re-read per cycle beyond the enrolled
# ones. Enrolled cardholders are always refreshed; this budget is what
# discovers newly-enrolled cards, so it trades discovery latency for load.
DEFAULT_SWEEP_BUDGET = 400

# A cardholder page that fails to load is retried on its own schedule rather
# than waiting for the sweep cursor to come round again, which with a large
# roster can be many cycles. The backoff is what keeps a cardholder that
# always fails — a corrupt record, say — from being read every cycle
# forever, while a one-off timeout is retried almost immediately.
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600

# How often to say something while sweeping. A first sweep of a large
# install reads for five minutes and used to emit nothing at all between
# "reading all N pages" and the finished snapshot, which is indistinguishable
# from a hang — long enough that an operator reasonably concludes the sync
# has stopped and starts looking for what broke.
PROGRESS_EVERY = 200

# Millennium's date/time fields, e.g. "08/17/2028 12:00 AM".
DATE_FORMAT = "%m/%d/%Y %I:%M %p"

# Roster names arrive as "Last, Middle. First" — this install repurposes the
# middle-name field as a card-type label ("HID", "RFID"), so a cardholder
# with none is simply "Last, First".
_ROSTER_NAME = re.compile(r"^(?P<last>[^,]+),\s*(?:(?P<middle>\S+)\.\s+)?(?P<first>.+)$")


@dataclass(frozen=True)
class Slot:
    """One of a cardholder's three card slots."""

    index: int
    card_id: str
    card_number: str
    facility_code: str
    card_format: str
    active: bool
    activation: datetime | None = None
    expiration: datetime | None = None

    @property
    def empty(self) -> bool:
        return not self.card_id and not self.card_number


def parse_roster_name(name: str) -> tuple[str, str]:
    """"Abayan, HID. Tanyabella" -> ("Tanyabella", "Abayan").

    Used only for the initial listing; once a cardholder's detail page is
    read, its FirstName/LastName fields win.
    """
    match = _ROSTER_NAME.match((name or "").strip())
    if not match:
        return "", (name or "").strip()
    return match.group("first").strip(), match.group("last").strip()


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


def parse_datetime(value: str, offset_seconds: int) -> datetime | None:
    """Read one of Millennium's date fields back into a UTC instant.

    The stored text is local to the install, per the `timeoffset` the browser
    reported at sign-in, so the offset has to come back off. Unparseable or
    empty values give None, and the caller falls back to its own default
    rather than provisioning a card with a date that means nothing.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        naive = datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        logger.debug("Millennium: unparseable date %r", value)
        return None
    return (naive - timedelta(seconds=offset_seconds)).replace(tzinfo=UTC)


def format_datetime(value: datetime, offset_seconds: int) -> str:
    """Render a UTC instant the way Millennium's date fields expect it.

    The server parses these fields against the browser's `timeoffset`
    cookie, which AG Connect captured alongside the session, so a UTC string
    would land hours away from the intended local time.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return (value + timedelta(seconds=offset_seconds)).strftime(DATE_FORMAT)


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
        data[SeosLedger._key(person_id, credential_id)] = list(deduped.values())
        SeosLedger._save_all(data)

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
            auth_cookie=auth_cookie or session.get("auth_cookie", ""),
            company_name=company_name or session.get("company_name", ""),
            time_offset=str(time_offset or session.get("time_offset") or ""),
        )
        self._sweep_budget = int(sweep_budget)

        from . import DESCRIPTOR

        self._descriptor = DESCRIPTOR
        # cardholder id -> {"slots": [Slot], "first": str, "last": str}
        self._profiles: dict[str, dict] = {}
        # Roster ids seen in the current list_people() pass.
        self._roster: dict[str, dict] = {}
        # Un-enrolled cardholders whose detail page this cycle will re-read.
        self._sweep: set[str] = set()
        # Detail pages read this cycle, for progress reporting.
        self._read_this_cycle = 0
        self._reading_total = 0
        # Where the next cycle's slice starts, so coverage rotates rather
        # than re-reading the same head of the roster forever.
        self._sweep_cursor = 0
        self._cold = True
        # Cardholders whose page failed to load: id -> (attempts, next try).
        self._retry: dict[str, tuple[int, float]] = {}
        # Contact details the bulk export supplied this cycle, id -> (email,
        # phone). The detail page is authoritative once it has been read;
        # this covers the cycle before that, which is the one that matters.
        self._exported_contact: dict[str, tuple[str, str]] = {}
        # The trigger format's display name, resolved once. "" means looked
        # for and not found; None means not looked for yet.
        self._trigger_format_label: str | None = None
        # Candidates who share a name with another roster entry. The export
        # has no cardholder id, so their contact details cannot be told
        # apart and their detail page has to be read before they are yielded.
        self._ambiguous: set[str] = set()

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
        self._roster = {}
        self._read_this_cycle = 0
        roster = self._client.list_cardholders()
        self._plan_sweep([str(r.get("ID")) for r in roster], roster)
        self._reading_total = len(self._sweep)

        for row in roster:
            pid = str(row.get("ID"))
            self._roster[pid] = row
            first, last = parse_roster_name(row.get("Name", ""))
            # A namesake's contact details cannot come from the export, so
            # read the page now rather than yield a Person without them.
            # This is the only chance: the pass is issued on the same cycle
            # a cardholder is first seen, and AccessGrid will not change the
            # address on a pass it has already issued.
            #
            # Not a spare request. `list_credentials` reads the page moments
            # later anyway; this only moves the first read earlier, and the
            # cache means later cycles do not pay for it at all.
            if pid in self._ambiguous and pid not in self._profiles:
                self._profile_for(pid)
            profile = self._profiles.get(pid)
            if profile:
                first = profile.get("first") or first
                last = profile.get("last") or last
            full_name = " ".join(p for p in (first, last) if p)
            exported_email, exported_phone = self._exported_contact.get(pid, ("", ""))
            # The detail page wins where we have read one, the same
            # precedence the name uses. The export covers the cycle before
            # that, which is the cycle a cardholder is provisioned on.
            stored_email = (profile or {}).get("email") or _first_valid_email(
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
        self._cold = False

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

    def _enrolled_from_export(self, roster: list[dict]) -> set[str] | None:
        """Cardholder ids carrying the trigger format, via the bulk export.

        None when the export is unavailable, which is the caller's signal to
        fall back to reading detail pages. It is an optimisation: on this
        install it finds 6 slots out of 4,797 in three requests, where the
        rotating sweep spends 400 requests a cycle looking for them.

        The export has no cardholder id, so rows are matched to the roster
        by name — and names collide here badly enough (42% of this roster,
        one of them five ways) that a match is a shortlist. Every candidate
        is still read and confirmed; this only decides *which* pages to
        read, never what is true about them.
        """
        label = self._trigger_format_name()
        if not label:
            return None
        try:
            rows = export.parse(self._client.export_cardholders())
        except Exception as e:  # noqa: BLE001 — never fail a cycle over this
            logger.info(
                "Millennium: bulk export unavailable (%s) — sweeping instead", e
            )
            return None
        if not rows:
            logger.info("Millennium: bulk export came back empty — sweeping instead")
            return None

        by_name: dict[tuple[str, str], list[str]] = {}
        for row in roster:
            first, last = parse_roster_name(row.get("Name", ""))
            by_name.setdefault(export.name_key(first, last), []).append(
                str(row.get("ID"))
            )

        # Contact details for everyone the export named, not only the
        # enrolled: cheap to keep, and it means a cardholder enrolled later
        # already has them before their detail page is ever read.
        #
        # Only where the name identifies one cardholder. A shared name maps
        # to every roster entry carrying it, so each row's details were
        # written against all of them and the last row won — one namesake's
        # email and phone standing in for the other's, on the cycle the pass
        # is issued. The export cannot say whose they are, so it says nothing
        # and `list_people` reads the page instead.
        self._exported_contact = {}
        for person in rows:
            ids = by_name.get(
                export.name_key(person.first_name, person.last_name)
            ) or []
            if len(ids) != 1:
                continue
            if person.email or person.phone:
                self._exported_contact[ids[0]] = (person.email, person.phone)

        candidates: set[str] = set()
        self._ambiguous = set()
        carrying = unmatched = 0
        ambiguous: list[tuple[str, int]] = []
        for person in rows:
            if not person.carries_format(label):
                continue
            carrying += 1
            ids = by_name.get(export.name_key(person.first_name, person.last_name))
            if not ids:
                unmatched += 1
                continue
            if len(ids) > 1:
                ambiguous.append((f"{person.first_name} {person.last_name}", len(ids)))
                self._ambiguous.update(ids)
            candidates.update(ids)

        if unmatched:
            # A trigger card we cannot place is the one case where the export
            # is worse than the sweep, so say so rather than quietly missing
            # somebody who is meant to be enrolled.
            logger.warning(
                "Millennium: %d cardholder(s) carry the trigger format but no "
                "roster entry matches their name — they will not be enrolled",
                unmatched,
            )
        for name, count in ambiguous:
            # Says why more pages are being read than there are trigger
            # cards. All of them are read and confirmed; the extras drop out
            # for holding no trigger card, but the reads are real.
            logger.info(
                "Millennium: %s matches %d cardholders by name — reading all of "
                "them to find which one holds the trigger card",
                name, count,
            )
        # Counted separately because they differ exactly when a name is
        # ambiguous, and reporting candidates as though they were enrolled
        # cardholders would overstate how many people are actually involved.
        if carrying == len(candidates):
            logger.info(
                "Millennium: export found %d cardholder(s) carrying the trigger format",
                carrying,
            )
        else:
            logger.info(
                "Millennium: export found %d cardholder(s) carrying the trigger "
                "format, across %d possible roster entries",
                carrying, len(candidates),
            )
        return candidates

    def _plan_sweep(self, ids: list[str], roster: list[dict] | None = None) -> None:
        """Pick which un-enrolled cardholders to re-read this cycle.

        Enrolled cardholders are always refreshed, so this slice exists only
        to notice cards that became enrolled since we last looked. The bulk
        export answers that directly when it is available; failing that, the
        roster is walked a window at a time so every cardholder is revisited
        within a bounded number of cycles rather than the same head being
        re-read forever.
        """
        if roster is not None:
            candidates = self._enrolled_from_export(roster)
            if candidates is not None:
                # Nothing else needs reading: a cardholder absent from this
                # set has no trigger card, and a cardholder we never read is
                # explicitly not evidence of anything downstream.
                self._sweep = candidates
                self._cold = False
                return

        if self._cold:
            # Nothing is cached yet, so a partial read would look like a
            # population with no credentials — which phase 3 reads as mass
            # deletion. Take the whole thing once.
            self._sweep = set(ids)
            logger.info(
                "Millennium: first sweep — reading all %d cardholder detail pages; "
                "later cycles refresh enrolled cardholders plus %d others",
                len(ids), self._sweep_budget,
            )
            return

        if not ids or self._sweep_budget <= 0:
            self._sweep = set()
            return

        start = self._sweep_cursor % len(ids)
        window = min(self._sweep_budget, len(ids))
        # Wrap around the end of the roster so the slice stays contiguous.
        self._sweep = set((ids + ids)[start : start + window])
        self._sweep_cursor = (start + window) % len(ids)
        logger.debug(
            "Millennium: re-reading %d of %d cardholders this cycle (from index %d)",
            window, len(ids), start,
        )

    def list_credentials(self, person_id: str) -> Iterable[Credential]:
        pid = str(person_id)
        profile = self._profile_for(pid)
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
        current = _read_slots(form, self._offset_seconds)
        slots = self._slots_for_credential(pid, credential_id, current)
        if not slots:
            logger.warning(
                "Millennium: no card backs credential %s/%s — not writing",
                pid, credential_id,
            )
            return False

        by_index = {s.index: s for s in current}
        writable = 0
        changed = False
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
            if form.is_checked(field) != want_active:
                form.set_checked(field, want_active)
                changed = True
        if not writable:
            return False
        if not changed:
            return True
        ok = self._client.save_cardholder(pid, form)
        if ok:
            self._profiles.pop(pid, None)
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
        slots = _read_slots(form, self._offset_seconds)
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
        marker = self._marker_slot(slots, already_written)
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
            self._fill_slot(form, slot, identity, card_id=replacing)
            written.append({
                "slot": slot,
                "card_number": str(identity.card_number),
                "facility_code": str(identity.site_code),
            })

        if not self._client.save_cardholder(pid, form):
            return False
        SeosLedger.record(pid, credential_id, written)
        self._profiles.pop(pid, None)
        logger.info(
            "Millennium: wrote %d credential(s) into cardholder %s", len(pending), pid,
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
        return {
            "auth_cookie": cookies.get(spec.required_cookie, ""),
            "base_url": self.base_url,
            "request_token": cookies.get("__RequestVerificationToken", ""),
            "company_name": cookies.get("UltraCompanyName", ""),
            "time_offset": cookies.get("timeoffset", ""),
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
            for s in _read_slots(form, self._offset_seconds)
            if not s.empty
        }
        token = form.value("__RequestVerificationToken")
        # Read from the cardholder rather than the credential id, which no
        # longer carries a position.
        marker = self._marker_slot(
            _read_slots(form, self._offset_seconds), set(recorded)
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
            self._profiles.pop(pid, None)
        return retired

    # -- internals -------------------------------------------------------

    def _is_trigger(self, slot: Slot) -> bool:
        # A slot only carries a format when it holds a real card, so this is
        # never true for an empty slot.
        return (
            bool(self.trigger_card_format)
            and slot.card_format == self.trigger_card_format
            and not slot.empty
        )

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
        credential_id = SEOS_CREDENTIAL_ID
        ledger = SeosLedger.get(pid, credential_id)
        empty = [s for s in slots if s.empty]

        if not ledger and len(empty) < REQUIRED_EMPTY_SLOTS:
            logger.info(
                "Millennium: skipping cardholder %s — Seos needs %d free card slot(s), "
                "found %d",
                pid, REQUIRED_EMPTY_SLOTS, len(empty),
            )
            return []

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

        return [
            Credential(
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
        ]

    def _slots_for_credential(
        self, pid: str, credential_id: str, slots: list[Slot] | None = None
    ) -> list[int]:
        """Which Millennium slot(s) a tracked credential currently occupies.

        For a Seos credential the ledger's slot number is only where the card
        was first written; operators move cards between slots. Resolve by
        card identity against the current cardholder when we have it, so a
        suspend never lands on whatever happens to sit in the old position.
        """
        if credential_id == SEOS_CREDENTIAL_ID:
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
    ) -> None:
        """Turn a slot into a live card.

        Leaving Card_N_CardID empty is what tells Millennium to create a card
        rather than update one; it assigns the id itself on save. Pass the
        existing id to overwrite a card in place instead — replacing the
        marker mutates the one card rather than leaving the old one adrift.
        """
        form.set_value(f"Card_{slot}_CardID", card_id)
        form.set_value(f"Card_{slot}_EncodedCardNumber", str(identity.card_number))
        form.set_value(f"Card_{slot}_FaciltyCode", str(identity.site_code))
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
        form.set_checked(f"Card_{slot}_Active", True)

    def _backing_off(self, pid: str) -> bool:
        """Has this cardholder failed recently, with its retry not yet due?"""
        pending = self._retry.get(pid)
        return pending is not None and time.monotonic() < pending[1]

    def _record_failure(self, pid: str, error: Exception) -> None:
        attempts = self._retry.get(pid, (0, 0.0))[0] + 1
        delay = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
        self._retry[pid] = (attempts, time.monotonic() + delay)
        logger.warning(
            "Millennium: failed to read cardholder %s: %s — retrying in %ds "
            "(attempt %d)",
            pid, error, delay, attempts,
        )

    def _profile_for(self, pid: str) -> dict | None:
        """Current slot state for a cardholder, refetched when it matters.

        Enrolled cardholders are always refreshed — their state drives phases
        2 through 6, and a stale "no credentials" answer would look like a
        deletion. Everyone else is refreshed within the per-cycle budget so
        newly-enrolled cards are still found without re-reading every page.
        """
        cached = self._profiles.get(pid)
        enrolled = bool(cached and cached.get("enrolled"))
        # Enrolled cardholders are never held back: their slots drive phases
        # 2 through 6, and a stale reading there is worse than a wasted read.
        if not enrolled:
            if self._backing_off(pid):
                return cached
            retrying = pid in self._retry
            if not retrying and pid not in self._sweep:
                # Including when nothing is cached. It used to read anyway in
                # that case, which was invisible while the first sweep read
                # everybody — but once the export narrowed the sweep to the
                # handful who carry a trigger card, every other cardholder
                # fell through here and was fetched regardless.
                #
                # Answering None is safe: it reaches the snapshot as "not
                # read this cycle", which no phase treats as evidence.
                return cached

        try:
            form = self._client.get_cardholder_form(pid)
        except MillenniumAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            self._record_failure(pid, e)
            return cached

        self._retry.pop(pid, None)

        self._read_this_cycle += 1
        if self._read_this_cycle % PROGRESS_EVERY == 0:
            logger.info(
                "Millennium: read %d of %d cardholder pages",
                self._read_this_cycle, max(self._reading_total, self._read_this_cycle),
            )

        slots = _read_slots(form, self._offset_seconds)
        profile = {
            "slots": slots,
            "first": form.value("FirstName"),
            "last": form.value("LastName"),
            # Personal before company: the pass is delivered to a person, and
            # a shared company address would send several people's passes to
            # one inbox. Both are commonly blank — this install has neither
            # for any of its 1,599 cardholders — which is what the synthesized
            # address exists for.
            "email": _first_valid_email(form.value("EMail"), form.value("CompanyEMail")),
            "phone": (form.value("Phone") or form.value("InternalPhone") or "").strip(),
            "enrolled": any(self._is_trigger(s) for s in slots),
        }
        self._profiles[pid] = profile
        return profile


def _read_slots(form: CardholderForm, offset_seconds: int = 0) -> list[Slot]:
    slots: list[Slot] = []
    for index in CARD_SLOTS:
        prefix = f"Card_{index}_"
        # A slot with no selected format submits the <select>'s first option,
        # which would masquerade as a real format on an empty slot.
        format_control = form.find(f"{prefix}CardFormat")
        selected = ""
        if format_control is not None:
            selected = next(
                (o.value for o in format_control.options if o.selected), ""
            )
        slots.append(
            Slot(
                index=index,
                card_id=form.value(f"{prefix}CardID"),
                card_number=form.value(f"{prefix}EncodedCardNumber"),
                facility_code=form.value(f"{prefix}FaciltyCode"),
                card_format=selected,
                active=form.is_checked(f"{prefix}Active"),
                activation=parse_datetime(
                    form.value(f"{prefix}ActivationDate"), offset_seconds
                ),
                expiration=parse_datetime(
                    form.value(f"{prefix}ExpirationDate"), offset_seconds
                ),
            )
        )
    return slots


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
