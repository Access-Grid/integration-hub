"""Reading Millennium: the first of the adapter's three stages.

A cycle exports what Millennium holds, works out what changed, and imports
the result back. This module is the export: the roster, the bulk cardholder
export and the per-cardholder detail pages, read into Slots and profiles,
with the caches and the sweep that keep a cycle from reading every page.
It decides nothing about what it read — Persons, Credentials and every
write belong to the adapter.

The bulk export
---------------

The detail screen is the only place a cardholder's slots can be read one at
a time, and reading every one of them is what the sweep does: ~1,600
requests to discover the handful of people who actually carry a trigger
card. On this install that is 6 slots out of 4,797.

The export answers the same question in three requests. It is a queued job
rather than an endpoint: start it, poll until the status says done, then
fetch the file it names. What comes back is a zip of one CSV, a row per
cardholder, with fifteen columns repeated per card slot:

    First Name, Last Name, Card Type, Employee ID, Phone, E-Mail,
    Current Status,
    Card 1 Printed Card No., Card 1 Encoded Card No., Card 1 Activation
    Date, Card 1 Expiration Date, Card 1 Active, Card 1 Card Format,
    Card 1 Facility Code, Card 1 Badge Type,       ... and again for 2, 3

It does carry the contact details, and they matter here more than they
look: the detail page has them too, but it is read after `list_people` has
already built the Person, so on the cycle a cardholder is first seen — the
same cycle they are provisioned on — there was nothing to read and the pass
went out to a synthesized address with no phone number.

Two things it does **not** carry, which is why it supplements the detail
page rather than replacing it:

  * **No cardholder id.** `Employee ID` is a separate, operator-entered
    field — 33137 for the cardholder whose record id is 11591 — so rows are
    matched back to the roster by name. Names are not unique here (42% of
    this roster collides, one name five ways), so a match is a *shortlist*
    to confirm against the detail page, never an identification.
  * **No CardID per slot**, which every write needs.

`Card Format` arrives as a display name — "AccessGrid / HID Wallet Format"
— where the trigger is configured as a numeric id, so the caller maps one
to the other through the format list the detail page already yields.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# A module rather than its names: client.py imports this module for the
# CSV helpers, so names looked up at call time are what survive the cycle.
from . import client as client_mod
from .html_form import CardholderForm

logger = logging.getLogger(__name__)

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

# The job's name in Millennium's long-operation registry.
OPERATION = "CardholdersExport"

# Slots per cardholder, matching CARD_SLOTS. The column prefix is 1-based
# and spelled out per slot rather than nested.
_SLOT_FORMAT = "Card {n} Card Format"
_SLOT_NUMBER = "Card {n} Encoded Card No."
_SLOT_FACILITY = "Card {n} Facility Code"
_SLOT_ACTIVE = "Card {n} Active"


@dataclass(frozen=True)
class ExportedSlot:
    index: int
    card_number: str
    facility_code: str
    card_format: str  # display name, not the id
    active: bool


@dataclass(frozen=True)
class ExportedCardholder:
    first_name: str
    last_name: str
    employee_id: str
    email: str
    phone: str
    slots: tuple[ExportedSlot, ...]

    def carries_format(self, display_name: str) -> bool:
        return any(s.card_format == display_name for s in self.slots)


def unpack(archive: bytes) -> str:
    """The CSV out of the zip Millennium hands back.

    One member, but named for the export rather than fixed, so it is found
    by extension instead of by name.
    """
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [n for n in bundle.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"No CSV in the export archive: {bundle.namelist()}")
        if len(names) > 1:
            logger.debug("Export archive holds %d CSVs; reading %s", len(names), names[0])
        return bundle.read(names[0]).decode("utf-8-sig")


def parse(csv_text: str, slots: tuple[int, ...] = (1, 2, 3)) -> list[ExportedCardholder]:
    """Rows from the export CSV, with the per-slot columns folded up."""
    out: list[ExportedCardholder] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        found: list[ExportedSlot] = []
        for n in slots:
            card_format = (row.get(_SLOT_FORMAT.format(n=n)) or "").strip()
            number = (row.get(_SLOT_NUMBER.format(n=n)) or "").strip()
            # An empty slot still occupies its columns; a slot with neither a
            # format nor a number holds no card.
            if not card_format and not number:
                continue
            found.append(
                ExportedSlot(
                    index=n,
                    card_number=number,
                    facility_code=(row.get(_SLOT_FACILITY.format(n=n)) or "").strip(),
                    card_format=card_format,
                    # Millennium writes True/False here rather than 1/0.
                    active=(row.get(_SLOT_ACTIVE.format(n=n)) or "").strip().lower()
                    == "true",
                )
            )
        out.append(
            ExportedCardholder(
                first_name=(row.get("First Name") or "").strip(),
                last_name=(row.get("Last Name") or "").strip(),
                employee_id=(row.get("Employee ID") or "").strip(),
                email=(row.get("E-Mail") or "").strip(),
                phone=(row.get("Phone") or "").strip(),
                slots=tuple(found),
            )
        )
    return out


def name_key(first: str, last: str) -> tuple[str, str]:
    """The join key between an export row and a roster entry.

    Deliberately crude — case-folded first and last — because it is only
    ever used to narrow, never to decide. The roster's own names carry
    middle initials the export omits, so anything stricter would miss.
    """
    return (first.strip().lower(), last.strip().lower())


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


def first_valid_email(*candidates: str) -> str:
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


# -- the export stage --------------------------------------------------------


class Exporter:
    """What the current cycle has read from Millennium, and what to read next.

    One per adapter, kept across cycles: the profile cache, the sweep cursor
    and the retry schedule are what let a cycle read a few hundred pages
    rather than every cardholder.

    The client, the trigger test and the trigger's display name are taken
    as callables rather than values because they belong to the adapter and
    are read from it when needed: a client or trigger set on the adapter
    after construction is the one this reads with.
    """

    def __init__(
        self,
        client: Callable[[], client_mod.MillenniumUltraClient],
        is_trigger: Callable[[Slot], bool],
        trigger_label: Callable[[], str],
        offset_seconds: int = 0,
        sweep_budget: int = DEFAULT_SWEEP_BUDGET,
    ):
        self._client = client
        self._is_trigger = is_trigger
        self._trigger_label = trigger_label
        self._offset_seconds = offset_seconds
        self._budget = int(sweep_budget)
        # cardholder id -> {"slots": [Slot], "first": str, "last": str}
        self.profiles: dict[str, dict] = {}
        # Un-enrolled cardholders whose detail page this cycle will re-read.
        self.sweep: set[str] = set()
        # Detail pages read this cycle, for progress reporting.
        self._read = 0
        self._total = 0
        # Where the next cycle's slice starts, so coverage rotates rather
        # than re-reading the same head of the roster forever.
        self._cursor = 0
        self.cold = True
        # Cardholders whose page failed to load: id -> (attempts, next try).
        self.retry: dict[str, tuple[int, float]] = {}
        # Contact details the bulk export supplied this cycle, id -> (email,
        # phone). The detail page is authoritative once it has been read;
        # this covers the cycle before that, which is the one that matters.
        self.contacts: dict[str, tuple[str, str]] = {}
        # Cardholders the ledger says we hold cards on, as of this cycle.
        self.ours: set[str] = set()

    def read_roster(self, ours: set[str]) -> list[dict]:
        """Start a cycle: read the roster and decide whose pages to read."""
        self._read = 0
        self.ours = ours
        roster = self._client().list_cardholders()
        self._plan_sweep([str(r.get("ID")) for r in roster], roster)
        self._total = len(self.sweep)
        return roster

    def finish_roster(self) -> None:
        """Every roster entry has been handed on; the cache is no longer cold."""
        self.cold = False

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
        label = self._trigger_label()
        if not label:
            return None
        try:
            rows = parse(self._client().export_cardholders())
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
            by_name.setdefault(name_key(first, last), []).append(
                str(row.get("ID"))
            )

        # Contact details for everyone the export named, not only the
        # enrolled: cheap to keep, and it means a cardholder enrolled later
        # already has them before their detail page is ever read.
        self.contacts = {}
        for person in rows:
            for pid in by_name.get(
                name_key(person.first_name, person.last_name)
            ) or []:
                if person.email or person.phone:
                    self.contacts[pid] = (person.email, person.phone)

        candidates: set[str] = set()
        carrying = unmatched = 0
        ambiguous: list[tuple[str, int]] = []
        for person in rows:
            if not person.carries_format(label):
                continue
            carrying += 1
            ids = by_name.get(name_key(person.first_name, person.last_name))
            if not ids:
                unmatched += 1
                continue
            if len(ids) > 1:
                ambiguous.append((f"{person.first_name} {person.last_name}", len(ids)))
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
                # Plus everyone we hold a card on, whom the export cannot
                # be relied on to name.
                #
                # It names cardholders by the format they carry, so somebody
                # loses their place in it the moment their last trigger card
                # goes — which is exactly the event we most need to see, and
                # the only evidence that authorises deleting their pass. It
                # also joins by name, having no cardholder id to join on, so
                # a roster entry spelled differently never lands at all.
                #
                # Either way the cardholder was carried only by their cached
                # page, and a restart or any write of ours drops that. Then
                # they are unread, and phase 3 declines to act on a record it
                # did not read — correctly, which is what makes it silent.
                # The pass stays live, the slot stays held, and nothing says
                # so. The ledger is the reliable statement of who matters:
                # a card we wrote is a card we are answerable for.
                self.sweep = candidates | self.ours
                # Everybody else in the roster has no trigger card and no
                # card of ours, so reading them would tell us nothing.
                self.cold = False
                return

        if self.cold:
            # Nothing is cached yet, so a partial read would look like a
            # population with no credentials — which phase 3 reads as mass
            # deletion. Take the whole thing once.
            self.sweep = set(ids)
            logger.info(
                "Millennium: first sweep — reading all %d cardholder detail pages; "
                "later cycles refresh enrolled cardholders plus %d others",
                len(ids), self._budget,
            )
            return

        if not ids or self._budget <= 0:
            self.sweep = set()
            return

        start = self._cursor % len(ids)
        window = min(self._budget, len(ids))
        # Wrap around the end of the roster so the slice stays contiguous.
        self.sweep = set((ids + ids)[start : start + window])
        self._cursor = (start + window) % len(ids)
        logger.debug(
            "Millennium: re-reading %d of %d cardholders this cycle (from index %d)",
            window, len(ids), start,
        )


    def _backing_off(self, pid: str) -> bool:
        """Has this cardholder failed recently, with its retry not yet due?"""
        pending = self.retry.get(pid)
        return pending is not None and time.monotonic() < pending[1]

    def _record_failure(self, pid: str, error: Exception) -> None:
        attempts = self.retry.get(pid, (0, 0.0))[0] + 1
        delay = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
        self.retry[pid] = (attempts, time.monotonic() + delay)
        logger.warning(
            "Millennium: failed to read cardholder %s: %s — retrying in %ds "
            "(attempt %d)",
            pid, error, delay, attempts,
        )

    def drop_profile(self, pid: str, why: str) -> None:
        """Forget a cardholder's cached page after writing to it.

        Named rather than inlined because it is load-bearing: a cardholder
        the sweep does not name is read *only* when their cached profile
        says they are enrolled, so dropping the cache is what decides
        whether they are ever read again.
        """
        if self.profiles.pop(pid, None) is None:
            return
        logger.info(
            "Millennium: dropped the cached page for cardholder %s after %s%s",
            pid, why,
            "" if pid in self.sweep
            else " — they are not in this cycle's sweep, so this is the last "
                 "cycle they are visible unless the next sweep names them",
        )

    def profile_for(self, pid: str) -> dict | None:
        """Current slot state for a cardholder, refetched when it matters.

        Enrolled cardholders are always refreshed — their state drives phases
        2 through 6, and a stale "no credentials" answer would look like a
        deletion. Everyone else is refreshed within the per-cycle budget so
        newly-enrolled cards are still found without re-reading every page.
        """
        cached = self.profiles.get(pid)
        enrolled = bool(cached and cached.get("enrolled"))
        # Enrolled cardholders are never held back: their slots drive phases
        # 2 through 6, and a stale reading there is worse than a wasted read.
        if not enrolled:
            if self._backing_off(pid):
                return cached
            retrying = pid in self.retry
            if not retrying and pid not in self.sweep:
                # Including when nothing is cached. It used to read anyway in
                # that case, which was invisible while the first sweep read
                # everybody — but once the export narrowed the sweep to the
                # handful who carry a trigger card, every other cardholder
                # fell through here and was fetched regardless.
                #
                # Answering None is safe: it reaches the snapshot as "not
                # read this cycle", which no phase treats as evidence.
                if pid in self.ours:
                    logger.warning(
                        "Millennium: not reading cardholder %s this cycle — not in "
                        "the sweep and %s. We hold cards on them, so every phase "
                        "will see them as having no credential",
                        pid,
                        "serving a profile cached from an earlier cycle"
                        if cached else "nothing is cached",
                    )
                return cached

        try:
            form = self._client().get_cardholder_form(pid)
        except client_mod.MillenniumAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            self._record_failure(pid, e)
            return cached

        self.retry.pop(pid, None)

        self._read += 1
        if self._read % PROGRESS_EVERY == 0:
            logger.info(
                "Millennium: read %d of %d cardholder pages",
                self._read, max(self._total, self._read),
            )

        slots = read_slots(form, self._offset_seconds)
        profile = {
            "slots": slots,
            "first": form.value("FirstName"),
            "last": form.value("LastName"),
            # Personal before company: the pass is delivered to a person, and
            # a shared company address would send several people's passes to
            # one inbox. Both are commonly blank — this install has neither
            # for any of its 1,599 cardholders — which is what the synthesized
            # address exists for.
            "email": first_valid_email(form.value("EMail"), form.value("CompanyEMail")),
            "phone": (form.value("Phone") or form.value("InternalPhone") or "").strip(),
            "enrolled": any(self._is_trigger(s) for s in slots),
        }
        self.profiles[pid] = profile
        return profile


def describe_slots(slots: list[Slot]) -> str:
    """One-line slot summary, for diagnostics."""
    return ", ".join(
        f"{s.index}:empty" if s.empty
        else f"{s.index}:{s.facility_code}/{s.card_number} "
             f"fmt={s.card_format or '-'} {'on' if s.active else 'off'} "
             f"id={s.card_id or '-'}"
        for s in slots
    ) or "none"


# Every per-slot field this adapter reads. Checked rather than assumed,
# because `form.value()` answers "" for a field that is not on the page and
# a silently-empty value is indistinguishable from a real one.
#
# That is not hypothetical. The move to ASP.NET Core corrected the spelling
# of FaciltyCode, so every card read with an empty facility code, the
# ledger's 99/165 stopped matching the slot's ("", "165"), and every pass
# was read as revoked. The write path already refused to guess — set_value
# raises for an unknown field — and only the read path was credulous.
_REQUIRED_SLOT_FIELDS = (
    "CardID",
    "EncodedCardNumber",
    "FacilityCode",
    "CardFormat",
    "ActivationDate",
    "ExpirationDate",
)


def _check_slot_shape(form: CardholderForm, prefix: str) -> None:
    """Refuse to read a card whose page does not look the way we expect.

    A slot either has its fields or is not rendered at all; a slot with
    some of them has been renamed under us. Stopping the cycle is the
    point — the alternative is reading a card as though a field were blank
    and deciding, downstream, that somebody's pass has been revoked.
    """
    present = [n for n in _REQUIRED_SLOT_FIELDS if form.find(prefix + n) is not None]
    if not present or len(present) == len(_REQUIRED_SLOT_FIELDS):
        return
    missing = ", ".join(
        prefix + n for n in _REQUIRED_SLOT_FIELDS if form.find(prefix + n) is None
    )
    raise client_mod.MillenniumError(
        f"Millennium's cardholder page is missing {missing} — the page shape "
        "has changed, and reading it would misreport every card on this "
        "cardholder"
    )


def read_slots(form: CardholderForm, offset_seconds: int = 0) -> list[Slot]:
    slots: list[Slot] = []
    for index in client_mod.CARD_SLOTS:
        prefix = f"Card_{index}_"
        _check_slot_shape(form, prefix)
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
                facility_code=form.value(f"{prefix}FacilityCode"),
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
