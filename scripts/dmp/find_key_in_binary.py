#!/usr/bin/env python3
"""Recover the DBISAM table password from the System Link binary.

The System Link app must hold the DBISAM encryption password to open its own
tables, so the password (or a string it is built from) lives in the executable
or an accompanying DLL/BPL. We don't have to *identify* it up front: we extract
every candidate string from the binary and test each against a known-plaintext
crib from a real encrypted table (Users.dat). The correct password decrypts the
first field descriptor to ``01 00 <len> <ASCII field-name>`` -- an unambiguous
hit. Authorized use only (recovering a key to read a database you may access).

System Link is a Delphi/DBISAM app, so strings appear both as ASCII and as
UTF-16LE (Delphi's native string encoding); we harvest both. We also try common
transforms (as-is, MD5) and all plausible DBISAM Blowfish variants
(key length 16/8, big/little-endian words, ECB/CBC-IV0), so we don't need to
know DBISAM's exact mode in advance.

Usage:
    python scripts/dmp/find_key_in_binary.py SystemLink.exe [more.dll ...] \\
        --sample /path/to/Db/Users.dat
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import warnings

warnings.filterwarnings("ignore")
from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes

NAMECH = set(range(65, 91)) | set(range(97, 123)) | set(range(48, 58)) | {95}


def swap32(b: bytes) -> bytes:
    o = bytearray(len(b))
    for i in range(0, len(b) - 3, 4):
        o[i:i + 4] = b[i:i + 4][::-1]
    return bytes(o)


def load_crib(sample_path: str):
    """Return the first 16 encrypted bytes of the field-descriptor region."""
    d = open(sample_path, "rb").read()
    return d[0x200:0x210]


def decrypt_blocks(algo, ct, le, mode):
    out = bytearray()
    prev = b"\x00" * 8
    for i in range(0, 16, 8):
        blk = ct[i:i + 8]
        x = swap32(blk) if le else blk
        p = Cipher(algo, modes.ECB()).decryptor().update(x)
        if le:
            p = swap32(p)
        if mode == "cbc":
            p = bytes(a ^ b for a, b in zip(p, prev))
            prev = blk
        out += p
    return bytes(out)


def crib_ok(pt: bytes) -> bool:
    if pt[0:2] != b"\x01\x00":
        return False
    L = pt[2]
    return 1 <= L <= 13 and all(c in NAMECH for c in pt[3:3 + L])


def try_raw_key(key: bytes, ct: bytes):
    """Test a raw byte window as a DIRECT Blowfish key (no password/MD5 step).

    In a memory dump the MD5(password) key DBISAM loaded is resident even if the
    password string is obfuscated. So scan aligned 16- and 8-byte windows.
    """
    try:
        algo = Blowfish(key)
    except Exception:
        return None
    for le in (False, True):
        for mode in ("ecb", "cbc"):
            if crib_ok(decrypt_blocks(algo, ct, le, mode)):
                return {"raw_key": key.hex(), "keylen": len(key),
                        "little_endian": le, "mode": mode}
    return None


def scan_raw_keys(data: bytes, ct: bytes, align: int = 4):
    """Fallback: sweep the buffer for a resident Blowfish key. Slow -- aligned."""
    n = len(data)
    for klen in (16, 8):
        for off in range(0, n - klen, align):
            hit = try_raw_key(data[off:off + klen], ct)
            if hit:
                hit["offset"] = off
                return hit
    return None


def test_password(pw: str, ct: bytes):
    for material, kind in ((pw.encode("utf-8", "ignore"), "raw"),
                           (hashlib.md5(pw.encode("utf-8", "ignore")).digest(), "md5")):
        for kl in (16, 8):
            key = material[:kl]
            if not key:
                continue
            try:
                algo = Blowfish(key)
            except Exception:
                continue
            for le in (False, True):
                for mode in ("ecb", "cbc"):
                    if crib_ok(decrypt_blocks(algo, ct, le, mode)):
                        return {"password": pw, "key_from": kind, "keylen": kl,
                                "little_endian": le, "mode": mode}
    return None


def extract_strings(data: bytes, minlen=3, maxlen=64):
    seen = set()
    # ASCII
    for m in re.finditer(rb"[\x20-\x7e]{%d,%d}" % (minlen, maxlen), data):
        s = m.group().decode("latin-1")
        if s not in seen:
            seen.add(s); yield s
    # UTF-16LE (Delphi native)
    for m in re.finditer((rb"(?:[\x20-\x7e]\x00){%d,%d}" % (minlen, maxlen)), data):
        s = m.group().decode("utf-16-le", "ignore")
        if s not in seen:
            seen.add(s); yield s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binaries", nargs="+", help="SystemLink.exe and/or DBISAM DLLs/BPLs")
    ap.add_argument("--sample", required=True, help="an encrypted .dat (e.g. Users.dat)")
    ap.add_argument("--minlen", type=int, default=3)
    ap.add_argument("--maxlen", type=int, default=64)
    args = ap.parse_args(argv)

    ct = load_crib(args.sample)
    print(f"crib from {args.sample}: {ct.hex(' ')}")
    print("(target decrypt = 01 00 <len> <ASCII field name>)\n")

    tested = 0
    for path in args.binaries:
        data = open(path, "rb").read()
        cands = list(extract_strings(data, args.minlen, args.maxlen))
        print(f"{path}: {len(cands)} candidate strings")
        for s in cands:
            tested += 1
            hit = test_password(s, ct)
            if hit:
                print("\n*** PASSWORD FOUND ***")
                for k, v in hit.items():
                    print(f"    {k}: {v!r}")
                print("\nPlug this into decrypt_table.py / the ODBC connection string.")
                return 0
    print(f"\nTested {tested} strings. No password matched the crib.")
    print("If empty: the password may be runtime-constructed/obfuscated -> use "
          "dynamic analysis (debugger breakpoint on DBISAM decrypt, or a memory "
          "dump while a table is open) and re-run this against the dump.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
