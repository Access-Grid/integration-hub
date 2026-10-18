"""The import file, built without touching the network.

An import names the fields it carries and leaves the rest of the record
alone. Verified on the live install: **a column left out of the file is
untouched, and a column included with an empty value is erased.** Both
halves matter. The first is what makes a narrow write safe; the second is
why we must never include a column we do not mean to change — including
First Name with the wrong value renamed a cardholder, and including Card
Type empty wiped it.

So the file carries exactly two kinds of column:

  * **the key** — Employee ID, which is what the import matches a row on,
    and which is unique across cardholders on this install; and
  * **the cells being changed** — nothing else.

A suspend is therefore two columns and one row.

**Column names are deliberately not Millennium's own labels.** The field
screen places any header that exactly matches a system field, and a placed
field is *removed from the catalogue the same page carries* — so naming a
column "Card 2 Active" is precisely how we lose the ability to read back
which numeric field it means. Every column is named with a prefixed, lowered
alias instead, which matches nothing, stays loose, and leaves the catalogue
whole. The alias appears in the file; the real label travels beside it so the
mapping can be built from the page.

Everything here is a pure function of its arguments: no HTTP, no clock, no
settings. The logic that has caused every write bug on this adapter is now
"these rows in, this file out".
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass

# The row's key. Unique across cardholders on this install, operator-entered,
# and the only thing the import matches on.
KEY_LABEL = "Employee ID"

# Per-slot labels, exactly as Millennium's field screen spells them.
LABEL_CARD_NUMBER = "Encoded Card No."
LABEL_FACILITY_CODE = "Facility Code"
LABEL_CARD_FORMAT = "Card Format"
LABEL_ACTIVATION = "Activation Date"
LABEL_EXPIRATION = "Expiration Date"
LABEL_ACTIVE = "Active"

# Field order within a slot. Only affects how the file reads; the mapping is
# by name. Fixed so two identical plans produce an identical file.
_SLOT_FIELD_ORDER = (
    LABEL_CARD_NUMBER,
    LABEL_FACILITY_CODE,
    LABEL_CARD_FORMAT,
    LABEL_ACTIVATION,
    LABEL_EXPIRATION,
    LABEL_ACTIVE,
)

_ALIAS_PREFIX = "agsync_"


def slot_label(index: int, field: str) -> str:
    """"Card 2 Facility Code" — Millennium's own spelling of a slot field."""
    return f"Card {index} {field}"


def alias_for(label: str) -> str:
    """A header name for this field that Millennium will not recognise.

    Deliberately unrecognisable. The field screen places any header matching
    a system field and drops that field from its own catalogue, which is how
    we would lose the field's numeric id. A prefixed, lowered, underscored
    name collides with nothing Millennium calls anything.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return f"{_ALIAS_PREFIX}{slug}"


@dataclass(frozen=True)
class SlotWrite:
    """What to change in one card slot.

    ``None`` means "do not carry a column for this at all", which is not the
    same as an empty string. An empty string is a value the import applies —
    it is how a field gets cleared — so the two must stay distinguishable.
    """

    index: int
    card_number: str | None = None
    facility_code: str | None = None
    card_format: str | None = None
    activation: str | None = None
    # Millennium owns the expiry. Set only to clear a slot.
    expiration: str | None = None
    active: bool | None = None

    def cells(self) -> dict[str, str]:
        """Millennium label -> value, for the fields this write names."""
        values = {
            LABEL_CARD_NUMBER: self.card_number,
            LABEL_FACILITY_CODE: self.facility_code,
            LABEL_CARD_FORMAT: self.card_format,
            LABEL_ACTIVATION: self.activation,
            LABEL_EXPIRATION: self.expiration,
            # Millennium's own spelling, and the capital matters: verified
            # on the live install, "true" is not accepted where "True" is.
            LABEL_ACTIVE: None if self.active is None else str(self.active),
        }
        return {
            slot_label(self.index, field): value
            for field, value in values.items()
            if value is not None
        }

    def describe(self) -> str:
        parts = []
        if self.card_number is not None:
            parts.append(
                f"card {self.facility_code or '?'}/{self.card_number or 'cleared'}"
            )
        if self.active is not None:
            parts.append("active" if self.active else "inactive")
        return f"slot {self.index}: {', '.join(parts) or 'no change'}"


@dataclass(frozen=True)
class RowWrite:
    """One cardholder's changes, addressed by the only key the import has."""

    employee_id: str
    slots: tuple[SlotWrite, ...]

    def describe(self) -> str:
        return f"Employee ID {self.employee_id} — " + "; ".join(
            s.describe() for s in self.slots
        )


def columns_for(rows: Iterable[RowWrite]) -> list[tuple[str, str]]:
    """(header alias, Millennium label) for every column the file needs.

    The key first, then each slot's fields in slot and field order, so the
    same plan always produces the same file.
    """
    wanted: set[str] = set()
    for row in rows:
        for slot in row.slots:
            wanted.update(slot.cells())

    labels = [KEY_LABEL]
    for index in sorted({int(name.split()[1]) for name in wanted}):
        for field in _SLOT_FIELD_ORDER:
            label = slot_label(index, field)
            if label in wanted:
                labels.append(label)
    return [(alias_for(label), label) for label in labels]


def to_csv(rows: Iterable[RowWrite]) -> str:
    """The file step one uploads.

    A header row of aliases and CRLF line endings, matching what
    ``FieldHeaders=true`` promises and what Millennium's own export produces.
    """
    rows = list(rows)
    if not rows:
        raise ValueError("an import needs at least one row")
    columns = columns_for(rows)
    if len(columns) < 2:
        raise ValueError(
            "an import row that changes nothing would carry only the key"
        )
    by_label = {label: alias for alias, label in columns}

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=[alias for alias, _ in columns],
        restval="",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for row in rows:
        record = {by_label[KEY_LABEL]: row.employee_id}
        for slot in row.slots:
            for label, value in slot.cells().items():
                record[by_label[label]] = value
        writer.writerow(record)
    return buffer.getvalue()
