#!/usr/bin/env python3
"""Decrypt and read DMP System Link DBISAM tables.

The System Link database is DBISAM v4 with per-table Blowfish encryption. The
table password is a System Link secret (recover it with find_key_in_binary.py);
pass it in with --password. It is never stored in this file. Encryption scheme,
confirmed against a real customer database:

    * only the field-descriptor + row region (file offset 0x200 onward) is
      encrypted; the basic header (0x00-0x1FF) is plaintext.
    * Blowfish, key = MD5(password) (full 16 bytes), big-endian, CBC, IV = 0.

Validated end to end against a customer database: a user's CODE field is the
card number the customer confirmed, and PROFILE1-4 are the access levels.

Usage:
    python scripts/dmp/dmp_decrypt.py Users.dat --password KEY --structure
    python scripts/dmp/dmp_decrypt.py Users.dat --password KEY --csv users.csv
    python scripts/dmp/dmp_decrypt.py /path/to/Db --password KEY --all --outdir export/
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import os
import struct
import sys
import warnings

warnings.filterwarnings("ignore")
from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes
import pydbisam.field as _field
from pydbisam import PyDBISAM

ENC_OFFSET = 0x200


def _key(password: str) -> bytes:
    return hashlib.md5(password.encode()).digest()

# DBISAM BLOB subtypes pydbisam doesn't enumerate (memo/graphic/wide-blob). Map
# any unknown id to its base type (low byte) so the reader doesn't crash.
for _tid in (5635, 5379, 5507, 5891):
    _field.FieldType._value2member_map_.setdefault(_tid, _field.FieldType.BLOB)


def is_encrypted(path: str) -> bool:
    d = open(path, "rb").read()
    if len(d) < 0x220:
        return False
    idx = struct.unpack_from("<H", d, ENC_OFFSET)[0]
    return not (idx == 1 and 1 <= d[0x202] <= 32)


def decrypt_bytes(path: str, key: bytes) -> bytes:
    """Return the full table file with its encrypted region decrypted."""
    d = open(path, "rb").read()
    if not is_encrypted(path):
        return d  # already clear (DebugTable, GPRS* tables)
    body = d[ENC_OFFSET:]
    body = body[: len(body) - (len(body) % 8)]
    pt = Cipher(Blowfish(key), modes.CBC(b"\x00" * 8)).decryptor().update(body)
    return d[:ENC_OFFSET] + pt


def read_table(path: str, key: bytes):
    """Return (columns, rows). Reads all slots and filters obviously-empty ones.

    NOTE: pydbisam's per-row delete flag is unreliable on these v4 files (live
    rows carry a non-zero header byte it reads as 'deleted'), so we read every
    slot with extract_deleted=True and keep rows that have real content.
    """
    db = PyDBISAM(data=decrypt_bytes(path, key))
    cols = db.fields()
    total = db.total_rows + db._deleted_rows
    out = []
    for i in range(total):
        try:
            r = db.row(i, extract_deleted=True)
        except Exception:
            continue
        if r and any(v not in (None, "", 0, False) for v in r):
            out.append(r)
    return cols, out


def export_csv(path: str, out_csv: str, key: bytes):
    cols, rows = read_table(path, key)
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return len(cols), len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a .dat file, or a Db directory with --all")
    ap.add_argument("--password", required=True, help="DMP table encryption key")
    ap.add_argument("--structure", action="store_true", help="print columns only")
    ap.add_argument("--csv", help="export this table to CSV")
    ap.add_argument("--all", action="store_true", help="export every table in a directory")
    ap.add_argument("--outdir", default="dmp_export", help="output dir for --all")
    args = ap.parse_args(argv)
    key = _key(args.password)

    if args.all:
        os.makedirs(args.outdir, exist_ok=True)
        for p in sorted(glob.glob(os.path.join(args.path, "*.dat"))):
            name = os.path.basename(p)[:-4]
            try:
                nc, nr = export_csv(p, os.path.join(args.outdir, name + ".csv"), key)
                print(f"  {name:<22} {nr:>6} rows, {nc} cols")
            except Exception as e:
                print(f"  {name:<22} FAILED: {e}")
        return 0

    if args.structure:
        cols, rows = read_table(args.path, key)
        print(f"{os.path.basename(args.path)}: {len(rows)} rows, {len(cols)} cols")
        print("columns:", cols)
        return 0

    if args.csv:
        nc, nr = export_csv(args.path, args.csv, key)
        print(f"exported {nr} rows x {nc} cols -> {args.csv}")
        return 0

    cols, rows = read_table(args.path, key)
    print(f"{os.path.basename(args.path)}: {len(rows)} rows, {len(cols)} cols")
    print("columns:", cols)


if __name__ == "__main__":
    sys.exit(main())
