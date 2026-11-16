#!/usr/bin/env python3
"""Extract the DMP user roster from a System Link DBISAM dump.

Context
-------
DMP System Link stores its data in DBISAM tables. Every table in a customer
dump we have seen is Blowfish-encrypted *except* ``DebugTable.dat``, which
System Link leaves in cleartext. That table logs the raw panel protocol
traffic, and those messages embed the user number + user name for every
access event and every user-programming record. So even without the DBISAM
table password we can reconstruct the roster from DebugTable alone.

This is throwaway-resistant reconnaissance tooling for the DMP Secure Link
integration work -- keep it around; we will likely need to re-run it against
other customer dumps. It has no dependency on the ag-sync-tool package other
than ``pydbisam`` (already installed in the dev venv).

Message formats we mine (all seen in real dumps, account 0001)
--------------------------------------------------------------
Monitoring / event records embed fields delimited by ``"`` and prefixed by a
single control byte identifying the field:

    ...\\u 00047"JANE DOE      \\      -> user  number=47   name="JANE DOE"
    ...\\v 008"MAIN DOOR 1      \\      -> device number=8    name="MAIN DOOR 1"
    ...\\a 025"2ND STAIR TO MEZ  \\      -> reader number=25   name="2ND STAIR TO MEZ"

User-programming records ("P=" messages) carry the credential too:

    P=0105 22291F371B0D...0000 YN DOM BLUM        (masked form uses '----')
      ^num ^user-code (hex)    ^^ flags ^name

Usage
-----
    python scripts/dmp/read_debugtable.py /path/to/Db/DebugTable.dat
    python scripts/dmp/read_debugtable.py /path/to/Db/DebugTable.dat --csv users.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict


# NOTE 1: DebugTable stores protocol control bytes as *literal* escape text --
#   a real 0x1e byte is rendered as the 5-char string "<x1e>", a CR as "<x0d>",
#   and field prefixes as "\u ", "\v ", "\a ". Parse against that literal text.
#
# NOTE 2: DebugTable is a rolling buffer. pydbisam only yields the ~1580 *live*
#   rows (which happen to hold the "P=" programming dumps). The older monitoring
#   traffic -- which carries clean user *numbers* -- sits in deleted/overwritten
#   record space. So we scan the raw file bytes (latin-1) to see the full
#   history, not just live rows.
#
# NOTE 3: DMP recycles user-number slots. The same number is reprogrammed to a
#   different person over time (e.g. slot 1010 held both JANE DOE and BILL
#   TELL), and a person may appear under several slots. The roster is therefore
#   keyed by NAME (the person); slot numbers are recorded as unstable metadata.

# Monitoring / event user field:  \u 00047"JANE DOE      \   -> (47, "JANE DOE")
USER_RE = re.compile(r'\\u\s*(\d{1,5})"([A-Z][A-Za-z0-9 ./#\-]+?)\s*\\')

# "P=" programming dumps pack many users, each ending in a literal "<x1e>":
#   <num><credential>...<mask:---- or digits><flags:YN><NAME><x1e>
# Anchor to the credential tail ("----" masked, or a digit) immediately before
# the "YN" active flag so we don't match the "NN"-prefixed reader-name lists
# that appear in other record types.
PROG_NAME_RE = re.compile(r"(?:----|\d)YN([A-Z0-9][A-Za-z0-9 .,#/\-]+?)<x1e>")


def _clean(name: str) -> str:
    return " ".join(name.split()).strip()


# Reader/door/zone names occasionally leak through the "P=" name pattern (they
# follow an arm/disarm flag pair). Drop anything that looks like a device name.
_READER_RE = re.compile(r"\bRDR\b|MAN DR|DOORS|DETECTOR|LOBBY|STAIR|GARAGE|"
                        r"CORRIDOR|MEZZ|SPRINKLER|BURG|HALL RDR|OFFICE R")


def _is_device_name(name: str) -> bool:
    return bool(_READER_RE.search(name)) or name.startswith(("NN", "NY", "YY"))


def extract_users(path: str):
    """Scan the raw DebugTable bytes and return a name-keyed roster.

    Returns dict[name -> {"slots": {num: count}, "seen": int, "event": bool}].
    ``event`` is True if the name was seen in access/event traffic (which gives
    a real user number); names seen only in "P=" dumps have no reliable number.
    """
    raw = open(path, "rb").read().decode("latin-1")
    roster: dict[str, dict] = defaultdict(
        lambda: {"slots": defaultdict(int), "seen": 0, "event": False}
    )

    for num, name in USER_RE.findall(raw):
        name = _clean(name)
        if not name:
            continue
        rec = roster[name]
        rec["slots"][int(num)] += 1
        rec["seen"] += 1
        rec["event"] = True

    for name in PROG_NAME_RE.findall(raw):
        name = _clean(name)
        if name and not _is_device_name(name):
            roster[name]["seen"] += 1

    return roster


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("debugtable", help="path to DebugTable.dat")
    ap.add_argument("--csv", help="write roster to this CSV file")
    args = ap.parse_args(argv)

    roster = extract_users(args.debugtable)
    names = sorted(roster)

    def slot_str(rec):
        slots = sorted(rec["slots"], key=lambda n: -rec["slots"][n])
        return ",".join(str(s) for s in slots) if slots else "(prog-only)"

    print(f"Recovered {len(names)} distinct users (people) from "
          f"{args.debugtable}\n")
    print(f"{'Name':<26} {'Seen':>5}  User#(slots, most-used first)")
    print(f"{'-'*26} {'-'*5}  {'-'*30}")
    for name in names:
        rec = roster[name]
        print(f"{name:<26} {rec['seen']:>5}  {slot_str(rec)}")

    reused = {n: r for n, r in roster.items() if len(r["slots"]) > 1}
    shared = defaultdict(list)
    for n, r in roster.items():
        for s in r["slots"]:
            shared[s].append(n)
    collisions = {s: ns for s, ns in shared.items() if len(ns) > 1}
    print(f"\n{sum(1 for r in roster.values() if r['event'])} seen in access "
          f"events (real numbers); {len(names)} total names.")
    if collisions:
        examples = "; ".join(
            "#{}={}".format(s, "/".join(ns[:2]))
            for s, ns in list(collisions.items())[:3]
        )
        print(f"{len(collisions)} user-number slots were reused across people "
              f"(slot recycling) -- e.g. {examples}")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["name", "user_number_slots", "times_seen", "in_access_events"])
            for name in names:
                rec = roster[name]
                w.writerow([name, slot_str(rec), rec["seen"], rec["event"]])
        print(f"\nWrote {len(names)} rows to {args.csv}")


if __name__ == "__main__":
    main()
