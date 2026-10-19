"""Building Millennium's cardholder import, and reading its log. No network.

An import is a CSV we upload plus a mapping naming, for each of its columns,
the Millennium field it fills. Rows are matched on Employee ID.

What live runs showed:
  * Employee ID must be mapped, or the whole import fails.
  * First and Last Name must be mapped, or every row is skipped.
  * An empty value in a mapped column can erase that field (Card Type does),
    so a row carries only its key, its names and the cells being changed.
  * A card with no number and facility code is skipped, not cleared — the
    import cannot delete a card.
  * A skipped row is only a Warning; the log still ends in success.
  * A header spelled exactly like a Millennium field ("Employee ID") is
    auto-mapped by the upload page; spelled without spaces ("EmployeeID") it
    matches nothing, and only our mapping decides.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

# Millennium's field ids. Card fields are repeated per slot (`Card` 1-3);
# cardholder fields use Card 0.
FIELD_IDS = {
    "First Name": 1,
    "Last Name": 2,
    "Employee ID": 4,
    "Encoded Card No.": 16,
    "Activation Date": 17,
    "Expiration Date": 18,
    "Active": 19,
    "Card Format": 20,
    "Facility Code": 21,
    **{
        label: first_id + position - 1
        for position in range(1, 11)
        for label, first_id in (
            (f"Access Level {position}", 200),
            (f"Access Level {position} Activation Date", 456),
            (f"Access Level {position} Expiration Date", 712),
        )
    },
}

# A card is found by its number and facility code; without both, the import
# skips the slot.
CARD_IDENTITY = ("Encoded Card No.", "Facility Code")


@dataclass
class Row:
    """One cardholder, and the cells to set on them.

    `cells` maps (card, label) to a value: card 0 for a cardholder field,
    1-3 for a card slot. Every card named must carry CARD_IDENTITY.
    """

    employee_id: str
    first_name: str
    last_name: str
    cells: dict[tuple[int, str], str] = field(default_factory=dict)

    def columns(self) -> tuple[tuple[int, str], ...]:
        return _KEY_COLUMNS + tuple(sorted(self.cells))

    def values(self) -> dict[tuple[int, str], str]:
        return {
            (0, "Employee ID"): self.employee_id,
            (0, "First Name"): self.first_name,
            (0, "Last Name"): self.last_name,
            **self.cells,
        }


_KEY_COLUMNS = ((0, "Employee ID"), (0, "First Name"), (0, "Last Name"))


def access_level_cells(card: int, levels) -> dict[tuple[int, str], str]:
    """Cells giving card `card` these levels, in the same positions.

    Only what is set: an empty date is left out rather than sent empty.
    """
    cells: dict[tuple[int, str], str] = {}
    for level in levels:
        cells[(card, f"Access Level {level.position}")] = level.name
        if level.activation:
            cells[(card, f"Access Level {level.position} Activation Date")] = level.activation
        if level.expiration:
            cells[(card, f"Access Level {level.position} Expiration Date")] = level.expiration
    return cells


@dataclass(frozen=True)
class ImportFile:
    csv: str
    fields: list[dict]  # ImportCardholdersNow's `ImportedFields`


def build(rows: list[Row]) -> list[ImportFile]:
    """One import per distinct set of columns.

    A column in the file is a column for every row in it, and an empty cell
    can erase a field — so rows that change different things never share a
    file.
    """
    groups: dict[tuple[tuple[int, str], ...], list[Row]] = {}
    for row in rows:
        for card in {card for card, _ in row.cells if card}:
            missing = [lbl for lbl in CARD_IDENTITY if not row.cells.get((card, lbl))]
            if missing:
                raise ValueError(
                    f"Employee ID {row.employee_id}, card {card}: no {', '.join(missing)}"
                )
        groups.setdefault(row.columns(), []).append(row)
    return [_file(columns, group) for columns, group in groups.items()]


def _file(columns: tuple[tuple[int, str], ...], rows: list[Row]) -> ImportFile:
    names = [label if not card else f"Card {card} {label}" for card, label in columns]
    headers = [name.replace(" ", "") for name in names]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        values = row.values()
        writer.writerow([values[column] for column in columns])
    fields = [
        {
            "Card": card,
            "ID": FIELD_IDS[label],
            "Name": name,
            "TenantID": 0,
            "UserColumnID": index,
            "UserColumnName": header,
        }
        for index, ((card, label), name, header) in enumerate(
            zip(columns, names, headers, strict=True)
        )
    ]
    return ImportFile(csv=out.getvalue(), fields=fields)


# --- the log ----------------------------------------------------------------


@dataclass(frozen=True)
class ImportLog:
    counts: dict[str, int]  # e.g. {"cardholders updated": 1, "cards skipped": 2}
    warnings: list[str]
    errors: list[str]

    def skipped(self) -> set[str]:
        """Employee IDs a warning names: rows, or cards on them, not applied."""
        return {
            m.group(1)
            for w in self.warnings
            if (m := re.search(r"'Employee ID:([^']*)'", w))
        }


_LINE = re.compile(r"^.+?\|\s*(Information|Warning|Error)\s*\|\s*(.*)$")
_COUNT = re.compile(r"^\s+(\d+) (.+?)(?: \(see reasons above\))?$")


def parse_log(text: str) -> ImportLog:
    counts: dict[str, int] = {}
    warnings: list[str] = []
    errors: list[str] = []
    for line in text.splitlines():
        if match := _LINE.match(line):
            level, message = match.groups()
            if level == "Warning":
                warnings.append(message.strip())
            elif level == "Error":
                errors.append(message.strip())
        elif match := _COUNT.match(line):
            counts[match.group(2)] = int(match.group(1))
    return ImportLog(counts=counts, warnings=warnings, errors=errors)
