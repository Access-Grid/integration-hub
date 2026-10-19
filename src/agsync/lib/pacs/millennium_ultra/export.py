"""Reading Millennium's cardholder export — the adapter's only data source.

The export is a queued job rather than an endpoint: start it, poll until the
status says done, then fetch the file (see client.export_cardholders). What
comes back is a zip of one CSV, a row per cardholder, with the card columns
repeated per slot:

    First Name, Last Name, Card Type, Employee ID, Phone, E-Mail,
    Current Status,
    Card 1 Printed Card No., Card 1 Encoded Card No., Card 1 Activation
    Date, Card 1 Expiration Date, Card 1 Active, Card 1 Card Format,
    Card 1 Facility Code, Card 1 Badge Type,       ... and again for 2, 3

Cardholders are keyed by **Employee ID**. The export carries no record id
and no per-card CardID, so nothing here can name a cardholder any other way.

`Card Format` is a display name — "AccessGrid / HID Wallet Format" — not the
numeric id the trigger is configured as.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# The job's name in Millennium's long-operation registry.
OPERATION = "CardholdersExport"

SLOTS = (1, 2, 3)

# How the export prints a date. Installs differ: "04/30/2024 00:00:00" on
# one, "9/11/2026 12:00:00 AM" on another.
DATE_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p")


@dataclass(frozen=True)
class Slot:
    """One of a cardholder's three card slots."""

    index: int
    card_number: str
    facility_code: str
    card_format: str  # display name, not the id
    active: bool
    activation: datetime | None = None
    expiration: datetime | None = None

    @property
    def empty(self) -> bool:
        return not self.card_number


@dataclass(frozen=True)
class Cardholder:
    employee_id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    slots: tuple[Slot, ...]


def unpack(archive: bytes) -> str:
    """The CSV out of the zip Millennium hands back, found by extension."""
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = [n for n in bundle.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"No CSV in the export archive: {bundle.namelist()}")
        return bundle.read(names[0]).decode("utf-8-sig")


def parse_datetime(value: str, offset_seconds: int) -> datetime | None:
    """An export date as a UTC instant.

    The text is local to the install, per the `timeoffset` the browser
    reported at sign-in, so the offset comes back off. Empty or unparseable
    gives None.
    """
    value = (value or "").strip()
    if not value:
        return None
    for shape in DATE_FORMATS:
        try:
            naive = datetime.strptime(value, shape)
        except ValueError:
            continue
        return (naive - timedelta(seconds=offset_seconds)).replace(tzinfo=UTC)
    logger.debug("Millennium: unparseable date %r", value)
    return None


def parse(csv_text: str, offset_seconds: int = 0) -> list[Cardholder]:
    """Rows from the export CSV, with the per-slot columns folded into Slots.

    Every cardholder gets all three slots, empty ones included, because how
    many are free is part of what the adapter reads.
    """
    out: list[Cardholder] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        def cell(name: str, row: dict = row) -> str:
            return (row.get(name) or "").strip()

        slots = tuple(
            Slot(
                index=n,
                card_number=cell(f"Card {n} Encoded Card No."),
                facility_code=cell(f"Card {n} Facility Code"),
                card_format=cell(f"Card {n} Card Format"),
                # Millennium writes True/False here rather than 1/0.
                active=cell(f"Card {n} Active").lower() == "true",
                activation=parse_datetime(cell(f"Card {n} Activation Date"), offset_seconds),
                expiration=parse_datetime(cell(f"Card {n} Expiration Date"), offset_seconds),
            )
            for n in SLOTS
        )
        out.append(
            Cardholder(
                employee_id=cell("Employee ID"),
                first_name=cell("First Name"),
                last_name=cell("Last Name"),
                email=cell("E-Mail"),
                phone=cell("Phone"),
                slots=slots,
            )
        )
    return out
