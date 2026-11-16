#!/usr/bin/env python3
"""Decrypt and parse a DMP System Link "Export Accounts" XML file.

System Link's File > Import and Export > Export Accounts produces an encrypted
`.xml` file, protected by a key the OPERATOR chooses at export time. This is the
cleanest supported extraction path for the AccessGrid integration: no DMP
compiled-in secret, no DBISAM files, no memory dump -- the operator runs the
export with a key we agree on, and we decrypt it here.

Encryption (reverse-engineered from a real export, key "abc123"):
    * File is ASCII: one long UPPERCASE hex string = the ciphertext.
    * AES-128, key = MD5(password) (16 bytes), ECB, PKCS7-style tail padding.
Decrypts to XML: <Panels><Panel>...<Users>...</Users>...</Panel></Panels>.
Field values are typed: DataType="1" = base64 string, "3" = integer,
"11" = timestamp, etc.

Validated: USER_NUM 67 JANE DOE CODE 10001; USER_NUM 28 JOHN SMITH CODE
10002 PROFILE1 2 -- matching the DBISAM decrypt and customer ground truth.

Usage:
    python scripts/dmp/decrypt_xml_export.py link_export.xml --password abc123
    python scripts/dmp/decrypt_xml_export.py link_export.xml --password abc123 \\
        --users-csv users.xml.csv --xml-out decrypted.xml
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import re
import sys
import warnings

warnings.filterwarnings("ignore")
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def decrypt_xml(path: str, password: str) -> str:
    raw = bytes.fromhex(open(path).read().strip())
    if len(raw) % 16:
        raise ValueError("ciphertext not a multiple of 16 bytes -- not AES?")
    key = hashlib.md5(password.encode()).digest()
    pt = Cipher(algorithms.AES(key), modes.ECB()).decryptor().update(raw)
    pt = pt.rstrip(b"\x00")
    if pt and pt[-1] <= 16:            # strip PKCS7 padding if present
        pt = pt[: -pt[-1]] if all(b == pt[-1] for b in pt[-pt[-1]:]) else pt
    text = pt.decode("latin-1")
    if "<Panels>" not in text[:64] and "<" not in text[:4]:
        raise ValueError("decrypted output is not XML -- wrong password?")
    return text


def _decode(datatype: str, val: str):
    if datatype == "1":                # base64 string
        try:
            return base64.b64decode(val).rstrip(b"\x00").decode("latin-1")
        except Exception:
            return val
    return val


def parse_users(xml: str):
    """Yield each <Users> record as a dict of tag -> decoded value."""
    for block in re.findall(r"<Users>(.*?)</Users>", xml, re.S):
        rec = {}
        for tag, dt, val in re.findall(r"<(\w+)[^>]*DataType=\"(\d+)\"[^>]*>(.*?)</\1>", block):
            rec[tag] = _decode(dt, val)
        yield rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="the encrypted export .xml")
    ap.add_argument("--password", required=True, help="operator's export key")
    ap.add_argument("--xml-out", help="write the decrypted XML here")
    ap.add_argument("--users-csv", help="write parsed users to this CSV")
    args = ap.parse_args(argv)

    xml = decrypt_xml(args.file, args.password)
    print(f"decrypted OK: {len(xml)} chars of XML")
    if args.xml_out:
        open(args.xml_out, "w").write(xml)
        print(f"  wrote {args.xml_out}")

    users = list(parse_users(xml))
    print(f"parsed {len(users)} user records")
    if users:
        cols = sorted({k for u in users for k in u})
        # put the fields we care about first
        first = [c for c in ("USER_NUM", "NAME", "CODE", "PROFILE1", "PROFILE2",
                             "PROFILE3", "PROFILE4", "ACTIVE") if c in cols]
        cols = first + [c for c in cols if c not in first]
        if args.users_csv:
            with open(args.users_csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerows(users)
            print(f"  wrote {args.users_csv}")
        print("\n  sample:")
        for u in users[:5]:
            print(f"    USER_NUM={u.get('USER_NUM')} NAME={u.get('NAME')!r} "
                  f"CODE={u.get('CODE')!r} P1={u.get('PROFILE1')} ACTIVE={u.get('ACTIVE')}")


if __name__ == "__main__":
    sys.exit(main())
