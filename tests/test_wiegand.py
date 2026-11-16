"""Unity card formats as AccessGrid file_data (lib/pacs/avigilon/wiegand.py).

Each standard is checked against a reference written here independently,
from the format's published layout, not from the encoder's own tables.
"""

from __future__ import annotations

import pytest

from agsync.lib.pacs.avigilon.wiegand import STANDARDS, Layout, UnencodableCard, encode, standard


def _hex(bits: str) -> str:
    return format(int(bits, 2), "064x")


def _even(bits: str) -> str:
    return str(bits.count("1") % 2)


def _odd(bits: str) -> str:
    return str(1 - bits.count("1") % 2)


def rails_w26(site: int, card: int) -> str:
    """BitFormat::W26#to_file_data from the Rails app, line for line."""
    payload = format(site, "08b") + format(card, "016b")
    return format(int(_even(payload[:12]) + payload + _odd(payload[12:]), 2), "064x")


def h10304(fc: int, cn: int) -> str:
    """Bit 0 even over 1-18; FC bits 1-16; CN bits 17-35; bit 36 odd over 18-35."""
    data = format(fc, "016b") + format(cn, "019b")  # bits 1-35
    return _hex(_even(data[:18]) + data + _odd(data[17:]))


def h10302(cn: int) -> str:
    """Bit 0 even over 1-18; CN bits 1-35; bit 36 odd over 18-35."""
    data = format(cn, "035b")
    return _hex(_even(data[:18]) + data + _odd(data[17:]))


def test_h10301_matches_a_hand_worked_card():
    # FC 1, CN 1: one 1 in each half, so even parity 1 and odd parity 0.
    assert encode(STANDARDS["H10301"], 1, 1) == format(0b10000000100000000000000010, "064x")


@pytest.mark.parametrize(("site", "card"), [(239, 42069), (0, 0), (255, 65535), (1, 65534), (128, 1)])
def test_h10301_matches_the_rails_encoder(site, card):
    assert encode(STANDARDS["H10301"], site, card) == rails_w26(site, card)


@pytest.mark.parametrize(("fc", "cn"), [(1234, 42069), (0, 0), (65535, 524287), (1, 1), (40000, 300000)])
def test_h10304_matches_the_standard(fc, cn):
    assert encode(STANDARDS["H10304"], fc, cn) == h10304(fc, cn)


@pytest.mark.parametrize("cn", [12345678901, 0, (1 << 35) - 1, 1, 1 << 17])
def test_h10302_matches_the_standard(cn):
    assert encode(STANDARDS["H10302"], "", cn) == h10302(cn)


def test_file_data_is_64_lower_case_hex():
    out = encode(STANDARDS["H10304"], 65535, 524287)
    assert len(out) == 64 and out == out.lower()


@pytest.mark.parametrize(("name", "fc", "cn", "why"), [
    ("H10301", 256, 1, "facility code 256 does not fit its 8 bits"),
    ("H10301", 1, 65536, "card number 65536 does not fit its 16 bits"),
    ("H10304", 1, 1 << 19, "does not fit its 19 bits"),
    ("H10302", "", 1 << 35, "does not fit its 35 bits"),
    ("H10301", 1, "12a", "is not a number"),
    ("H10301", "", 1, "is not a number"),
])
def test_a_number_that_does_not_fit_is_refused(name, fc, cn, why):
    with pytest.raises(UnencodableCard, match=why):
        encode(STANDARDS[name], fc, cn)


@pytest.mark.parametrize("change", [
    {"issue_len": 4}, {"flags": 1}, {"offset": 2}, {"odd_len": 18}, {"card_len": 18}, {"total_bits": 35},
])
def test_a_layout_that_is_not_a_known_standard_is_refused(change):
    layout = Layout(**{**STANDARDS["H10304"].as_params(), **change})
    assert standard(layout) is None
    with pytest.raises(UnencodableCard, match="not one this sync can encode"):
        encode(layout, 1, 1)


def test_the_26_bit_format_a_live_box_sent_is_h10301():
    live = {"total_bits": "26", "fc_loc": "1", "fc_len": "8", "card_loc": "9", "card_len": "16", "even_loc": "0",
            "even_len": "13", "odd_loc": "13", "odd_len": "13", "issue_len": "0", "offset": "0", "flags": "0"}
    assert standard(Layout.from_row(live)) == "H10301"


def test_a_format_with_no_facility_code_matches_wherever_unity_says_it_starts():
    assert standard(Layout(**{**STANDARDS["H10302"].as_params(), "fc_loc": 5})) == "H10302"


def test_a_layout_field_that_is_not_a_number_is_refused():
    with pytest.raises(UnencodableCard, match="fc_len"):
        Layout.from_row({"total_bits": "26", "fc_len": "eight"})
