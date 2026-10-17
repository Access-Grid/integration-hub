"""The CSV an import is made of, built without touching the network.

This is the module that replaces the full-form round-trip, and it is worth
being clear about what changes. A cardholder save re-posts the record's
entire state, so every field we fail to echo is erased and the parser has to
reproduce browser submit semantics byte for byte to be accepted at all. An
import names the columns it carries, and the server applies those and leaves
the rest of the record alone — so the safest possible write is the narrowest
one, and that is what :func:`to_csv` emits: the three identity columns, plus
columns for exactly the card slots being changed.

A status change therefore sends six cells, not a 90 KB form. The dates, the
access levels, the photo, the custom user fields and the two slots we are
not touching are not in the file, so they cannot be mangled by us.

Everything here is a pure function of its arguments. No HTTP, no clock, no
settings — which means the logic that has caused every write bug on this
adapter is now testable as "these rows in, this CSV out".

**Identity.** The import matches a row to a cardholder on First Name, Last
Name and Card Type, and nothing else — there is no column for the record id
that the rest of the adapter identifies people by. So the values in those
three columns are copied verbatim out of the export rather than rebuilt from
the detail page: the export is a dump of the same table the import matches
against, and round-tripping its own spelling is the only way to be sure the
row lands on the record we read. Where a name is not unique in the export,
:func:`unique_identities` withholds it and the caller declines to write.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass

# The per-slot columns, spelled as both the export and the import label
# them. Order matters only for readability of the file; the mapping is by
# name.
COLUMN_CARD_NUMBER = "Encoded Card No."
COLUMN_FACILITY_CODE = "Facility Code"
COLUMN_CARD_FORMAT = "Card Format"
COLUMN_ACTIVATION = "Activation Date"
COLUMN_EXPIRATION = "Expiration Date"
COLUMN_ACTIVE = "Active"

_SLOT_COLUMN_ORDER = (
    COLUMN_CARD_NUMBER,
    COLUMN_FACILITY_CODE,
    COLUMN_CARD_FORMAT,
    COLUMN_ACTIVATION,
    COLUMN_EXPIRATION,
    COLUMN_ACTIVE,
)


def slot_column(index: int, column: str) -> str:
    """"Card 2 Facility Code" — the label both halves of the round-trip use."""
    return f"Card {index} {column}"


@dataclass(frozen=True)
class Identity:
    """How the import finds the cardholder, copied out of the export.

    Card Type is blank for every row the export has produced on this
    install, which is why it is carried rather than relied on: it is part
    of the match key whether or not it holds anything.
    """

    first_name: str
    last_name: str
    card_type: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.first_name.strip().lower(),
            self.last_name.strip().lower(),
            self.card_type.strip().lower(),
        )

    def describe(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return f"{name} ({self.card_type})" if self.card_type else name


@dataclass(frozen=True)
class SlotWrite:
    """What to put in one card slot. Only the stated fields are sent.

    ``None`` means "do not carry a column for this at all", which is not the
    same as an empty string: an empty string is a value the import will
    apply. Writing an empty expiration date is how a card's expiry would be
    cleared, so the two have to stay distinguishable.
    """

    index: int
    card_number: str | None = None
    facility_code: str | None = None
    card_format: str | None = None
    activation: str | None = None
    expiration: str | None = None
    active: bool | None = None

    def cells(self) -> dict[str, str]:
        values = {
            COLUMN_CARD_NUMBER: self.card_number,
            COLUMN_FACILITY_CODE: self.facility_code,
            COLUMN_CARD_FORMAT: self.card_format,
            COLUMN_ACTIVATION: self.activation,
            COLUMN_EXPIRATION: self.expiration,
            # Millennium's own spelling in the export, and what its import
            # parses back.
            COLUMN_ACTIVE: None if self.active is None else str(self.active),
        }
        return {
            slot_column(self.index, column): value
            for column, value in values.items()
            if value is not None
        }

    def describe(self) -> str:
        parts = []
        if self.card_number is not None:
            parts.append(f"card {self.facility_code or '?'}/{self.card_number}")
        if self.active is not None:
            parts.append("active" if self.active else "inactive")
        if self.expiration is not None:
            parts.append(f"expires {self.expiration or 'never'}")
        return f"slot {self.index}: {', '.join(parts) or 'no change'}"


@dataclass(frozen=True)
class RowWrite:
    """One cardholder's changes."""

    identity: Identity
    slots: tuple[SlotWrite, ...]

    def describe(self) -> str:
        return f"{self.identity.describe()} — " + "; ".join(
            s.describe() for s in self.slots
        )


def unique_identities(
    exported: Iterable,
) -> dict[tuple[str, str, str], Identity]:
    """Identities that name exactly one cardholder in the export.

    The export is a dump of the table the import matches against, so a key
    appearing once there appears once there. A key appearing twice is
    withheld entirely — not resolved to the first row — because the import
    offers no way to say which of them was meant, and the failure mode of
    guessing is a working credential on the wrong person's record.

    Takes anything with ``first_name``, ``last_name`` and optionally
    ``card_type``, so it reads an :class:`~.export.ExportedCardholder`
    directly.
    """
    seen: dict[tuple[str, str, str], Identity] = {}
    collided: set[tuple[str, str, str]] = set()
    for row in exported:
        identity = Identity(
            first_name=getattr(row, "first_name", "") or "",
            last_name=getattr(row, "last_name", "") or "",
            card_type=getattr(row, "card_type", "") or "",
        )
        key = identity.key
        if key in seen:
            collided.add(key)
            continue
        seen[key] = identity
    for key in collided:
        seen.pop(key, None)
    return seen


def columns_for(rows: Iterable[RowWrite]) -> list[str]:
    """The header row: the identity columns, then only what is being written.

    Slot columns are emitted in slot then field order so a file read by a
    human lines up, and so two identical plans produce an identical file —
    which is what makes the golden tests worth having.
    """
    from .import_fields import IDENTITY_COLUMNS

    wanted: set[str] = set()
    for row in rows:
        for slot in row.slots:
            wanted.update(slot.cells())

    ordered = list(IDENTITY_COLUMNS)
    indexes = sorted({int(name.split()[1]) for name in wanted})
    for index in indexes:
        for column in _SLOT_COLUMN_ORDER:
            name = slot_column(index, column)
            if name in wanted:
                ordered.append(name)
    return ordered


def to_csv(rows: Iterable[RowWrite]) -> str:
    """The file step one uploads.

    CRLF and a header row, because that is what ``FieldHeaders=true`` and
    Millennium's own export produce, and the parser on the other side is the
    same one either way.
    """
    rows = list(rows)
    if not rows:
        raise ValueError("an import needs at least one row")
    headers = columns_for(rows)

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=headers, restval="", lineterminator="\r\n"
    )
    writer.writeheader()
    for row in rows:
        record = {
            "First Name": row.identity.first_name,
            "Last Name": row.identity.last_name,
            "Card Type": row.identity.card_type,
        }
        for slot in row.slots:
            record.update(slot.cells())
        writer.writerow(record)
    return buffer.getvalue()
