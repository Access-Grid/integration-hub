"""A small encrypted DBISAM v4 Users table, laid out the way pydbisam reads one.

Built so the DMP tests run the real decrypt-and-parse path: Blowfish-CBC with
key MD5(password) and IV 0, the field definitions one run and each record its
own (as System Link's files are), a 25-byte record header (status byte — 0
while live — two 32-bit values, a 16-byte checksum), then each field as a null
marker and its value.
"""

from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timedelta

from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes

_STRING, _BOOLEAN, _INTEGER, _TIMESTAMP = 1, 4, 6, 11
FIELDS = [("USER_NUM", _INTEGER, 0), ("CODE", _STRING, 10), ("NAME", _STRING, 16), ("ACTIVE", _BOOLEAN, 0),
          ("U_FIELD1", _STRING, 40), ("LAST_PNL_CMP", _TIMESTAMP, 0), ("LAST_CHANGE", _TIMESTAMP, 0)]
_SIZE = {_INTEGER: 4, _BOOLEAN: 1, _TIMESTAMP: 8}
HEADER = 25


def _layout():
    offset, out = HEADER, []
    for name, kind, size in FIELDS:
        width = size + 1 if kind == _STRING else _SIZE[kind]
        out.append((name, kind, size, offset + 1, width))  # value after its null marker
        offset += 1 + width
    return out, offset + (-offset % 8)  # rows a whole number of cipher blocks


def _value(kind, width, value) -> bytes:
    if kind == _STRING:
        return str(value).encode("cp1252").ljust(width, b"\0")
    if kind == _BOOLEAN:
        return bytes([1 if value else 0])
    if kind == _INTEGER:
        return struct.pack("<i", value)
    ms = (value - datetime(1, 1, 1) + timedelta(days=1)).total_seconds() * 1000
    return struct.pack("<d", ms)


def users_dat(records: list[tuple[int, dict]], live: int, password: str = "k") -> bytes:
    """`records` as (status byte, row) in slot order; `live` is the header's count."""
    fields, row_size = _layout()
    head = bytearray(0x200)
    struct.pack_into("<IHH", head, 0x29, live, row_size, len(fields))
    defs = bytearray()
    for i, (name, kind, size, offset, _) in enumerate(fields, 1):
        d = bytearray(768)
        struct.pack_into("<HB", d, 0, i, len(name))
        d[3:3 + len(name)] = name.encode()
        struct.pack_into("<HH", d, 0xA4, kind, size if kind == _STRING else 0)
        struct.pack_into("<H", d, 0xAC, offset - 1)
        defs += d
    rows = bytearray()
    for n, (status, row) in enumerate(records):
        r = bytearray(row_size)
        r[0] = status
        struct.pack_into("<II", r, 1, n + 1, n + 1)
        for name, kind, _, offset, width in fields:
            if row.get(name) is not None:
                r[offset - 1] = 1
                r[offset:offset + width] = _value(kind, width, row[name])
        rows += r
    key = hashlib.md5(password.encode()).digest()

    def run(chunk: bytes) -> bytes:
        enc = Cipher(Blowfish(key), modes.CBC(b"\0" * 8)).encryptor()
        return enc.update(chunk) + enc.finalize()

    return bytes(head) + run(bytes(defs)) + b"".join(
        run(bytes(rows[i:i + row_size])) for i in range(0, len(rows), row_size))


def user(num: int, code: str, name: str, compared: datetime, email: str = "", active: bool = True) -> dict:
    return {"USER_NUM": num, "CODE": code, "NAME": name, "ACTIVE": active, "U_FIELD1": email,
            "LAST_PNL_CMP": compared, "LAST_CHANGE": compared - timedelta(days=30)}
