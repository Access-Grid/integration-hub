"""Reading Millennium's cardholder export.

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
import zipfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)

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
