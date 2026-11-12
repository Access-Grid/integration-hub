"""A Unity card format's bits, as AccessGrid `file_data`.

AccessGrid takes a card in a format other than 26-bit as `file_data`: the
whole Wiegand credential, parity bits included, as one number, right-aligned
in 64 hex characters. That is the layout of the Rails app's
BitFormat::W26#to_file_data, for more than 26 bits.

Only the formats in STANDARDS are encoded: H10301, H10302 and H10304. The
site's format is recognised by Unity's own definition of it matching one of
them field for field, so a format we have not written down — or one whose
Unity definition reads differently than we expect — is refused, not guessed.
A card encoded wrong is a pass that opens nothing, or the wrong door.

Unity gives each field a 0-based start (`…loc`) and a length in bits
(`…len`), bit 0 being the first bit sent. A parity field covers its parity
bit too: the even parity bit is its first bit, over the rest; the odd parity
bit is its last, over the rest. That reading is the only one that gives
H10301 from the 26-bit definition a live box sent (even 0/13, odd 13/13).
The 37-bit definitions below are what that reading gives the standards; no
live box has shown us one yet.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

FILE_DATA_HEX_CHARS = 64


class UnencodableCard(ValueError):
    """This card cannot be put in this format; the message says why."""


@dataclass(frozen=True)
class Layout:
    total_bits: int
    fc_loc: int
    fc_len: int
    card_loc: int
    card_len: int
    even_loc: int
    even_len: int
    odd_loc: int
    odd_len: int
    issue_len: int = 0
    offset: int = 0
    flags: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Layout:
        """From card_format_row's fields (or a stored copy); UnencodableCard if one is not a number."""
        values = {}
        for f in fields(cls):
            text = str(row.get(f.name, "") or "0").strip()
            if not text.isdigit():
                raise UnencodableCard(f"card format field {f.name} is {text!r}")
            values[f.name] = int(text)
        return cls(**values)

    def as_params(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def _key(self) -> tuple:
        # A facility code of no bits sits nowhere, whatever its start says.
        return (self.total_bits, self.fc_loc if self.fc_len else 0, self.fc_len, self.card_loc, self.card_len,
                self.even_loc, self.even_len, self.odd_loc, self.odd_len, self.issue_len, self.offset, self.flags)


STANDARDS: dict[str, Layout] = {
    # 26-bit: FC 8, CN 16; even parity over bits 1-12, odd over 13-24.
    "H10301": Layout(26, 1, 8, 9, 16, 0, 13, 13, 13),
    # 37-bit, no facility code: CN 35; even over bits 1-18, odd over 18-35.
    "H10302": Layout(37, 0, 0, 1, 35, 0, 19, 18, 19),
    # 37-bit: FC 16, CN 19; even over bits 1-18, odd over 18-35.
    "H10304": Layout(37, 1, 16, 17, 19, 0, 19, 18, 19),
}


def standard(layout: Layout) -> str | None:
    """The name of the standard this layout is, or None."""
    return next((name for name, s in STANDARDS.items() if s._key() == layout._key()), None)


def encode(layout: Layout, facility_code: str | int, card_number: str | int) -> str:
    """The credential as `file_data`: lower-case hex, 64 characters.

    Raises UnencodableCard for a layout that is not a known standard, or a
    number that does not fit its field.
    """
    name = standard(layout)
    if name is None:
        raise UnencodableCard(
            f"the card format is not one this sync can encode ({', '.join(STANDARDS)}): {layout.as_params()}")
    bits = [0] * layout.total_bits
    for label, value, loc, length in (("facility code", facility_code, layout.fc_loc, layout.fc_len),
                                      ("card number", card_number, layout.card_loc, layout.card_len)):
        if not length:
            continue
        text = str(value).strip()
        if not text.isdigit():
            raise UnencodableCard(f"{name}: {label} {value!r} is not a number")
        number = int(text)
        if number >= 1 << length:
            raise UnencodableCard(f"{name}: {label} {number} does not fit its {length} bits "
                                  f"(at most {(1 << length) - 1})")
        bits[loc:loc + length] = map(int, format(number, f"0{length}b"))
    bits[layout.even_loc] = sum(bits[layout.even_loc + 1:layout.even_loc + layout.even_len]) % 2
    last = layout.odd_loc + layout.odd_len - 1
    bits[last] = 1 - sum(bits[layout.odd_loc:last]) % 2
    return format(int("".join(map(str, bits)), 2), f"0{FILE_DATA_HEX_CHARS}x")


def fits(layout: Layout, facility_code: str | int, card_number: str | int) -> None:
    """Raise UnencodableCard unless both numbers fit the layout's fields."""
    encode(layout, facility_code, card_number)
