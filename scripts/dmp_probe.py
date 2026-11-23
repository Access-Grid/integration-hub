"""Probe of a DMP System Link database — strictly read-only.

Collects what we need to know before trusting the DMP adapter against a
database we have never seen: whether the encryption key is right, whether the
tables decrypt and parse, whether our live-vs-deleted-row filtering reproduces
DBISAM's own record count, and what the adapter makes of the rows — how many
people it would sync, which ones carry the email that triggers enrollment, and
whether any card numbers collide. It also checks that this machine can reach
AccessGrid, which the hub needs as much as it needs the database.

Unlike the HTTP probes there is no `write` mode: DMP System Link is
store-and-forward and the adapter never writes back, so neither does the probe.
Every mode opens the files read-only and changes nothing.

Three rules, because the output is meant to be pasted into a chat or ticket:

  * **Read-only.** The files are opened for reading. Nothing is created,
    updated or deleted — there is no mode that does.
  * **No secrets.** The encryption key never reaches the report. It is prompted
    for (or read from DMP_PROBE_KEY for an unattended run) and used, not logged.
  * **No names or card numbers.** Values under a column that looks like a name,
    email, PIN, department or card code are replaced by their shape
    (`<str len=5 digits>`), so the report still shows what kind of value sits
    there. Counts, dates, flags and column names are kept: those are the point.

It runs the hub's own `DmpClient` and `DmpAdapter` against the database — the
point is to exercise the production read path, not a copy of it — and checks
what they produce against an independent oracle: the live-record count DBISAM
stores in each table's plaintext header, which the client never consults. So
the report can say whether our panel-compare heuristic agrees with DBISAM
itself.

Usage:
    python scripts/dmp_probe.py [all|tables|fields] <db_folder> [--email-field U_FIELD1]

`all` (the default) runs every check: environment, AccessGrid reachability, the
key and decrypt, the live-row oracle, the adapter's people/credentials, and a
scan for which User Field holds the enrollment email. `tables` inventories the
folder — which DBISAM tables are present, whether each is encrypted, its size
and the live/deleted counts in its header — the read to send about a box we
have never seen. `fields` finds the email: it reports, per candidate User
Field, how many live users hold an email-shaped value there, and recommends
which to configure. `slots` lists every record slot in Users.dat: its
DBISAM record header bytes (bar the 16-byte checksum), its user number, dates
and Active flag, and whether the hub reads it as current — run it, delete a
test user in System Link, and run it again to see which bytes mark a deleted
record. The report is printed and saved as JSON.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Column names whose values are a person's identity or a card number. CODE and
# CODE_2 are the card numbers; U_FIELD* hold the enrollment email. Matched
# against DMP column names to decide what to replace with a shape.
_PII_KEY = re.compile(r"name|email|code$|code_2|pin$|phone|u_field|department|descrip", re.I)
# User Fields the operator might have put the email in. The adapter defaults to
# U_FIELD1; `fields` mode reports all three so a misconfigured box is obvious.
_EMAIL_FIELD_CANDIDATES = ("U_FIELD1", "U_FIELD2", "U_FIELD3")
# A value that looks like an email: something, an @, a dot after it. Loose on
# purpose — the point is "did the operator put emails here", not validation.
_EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
ACCESSGRID_HOST = "api.accessgrid.com"
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "NO_PROXY", "no_proxy")
# Tables the adapter and its neighbours care about, for `tables` to call out
# whether each is present. Users is the only one the adapter reads today; the
# rest are listed because a box missing them is worth knowing about.
_KNOWN_TABLES = ("Users", "Profiles", "AreaInfo", "Device734", "DeviceInfo", "Account", "CustomFields")
MODES = ("all", "tables", "fields", "slots")


def _load_agsync():
    """Import the hub's DMP code, or explain how to make it importable.

    The probe runs the production client and adapter rather than a copy, so it
    has to find `agsync`. Run inside the repo's virtualenv (or with the project
    installed) and this just works; otherwise say so instead of a bare
    ImportError pasted into a ticket.
    """
    try:
        from agsync.lib.pacs.base import CredentialStatus
        from agsync.lib.pacs.dmp.adapter import DEFAULT_EMAIL_FIELD, DmpAdapter
        from agsync.lib.pacs.dmp.client import DmpClient, DmpError
    except ImportError as e:
        print(
            f"Could not import the hub's DMP code ({e}).\n"
            "Run this with the project's virtualenv active, from the repo root:\n"
            "    .venv/bin/python scripts/dmp_probe.py ...",
            file=sys.stderr,
        )
        raise SystemExit(2) from e
    return DmpClient, DmpError, DmpAdapter, DEFAULT_EMAIL_FIELD, CredentialStatus


def redact(value: Any, key: str = "") -> Any:
    """The value with anything personal swapped for a description of it."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        out = [redact(v, key) for v in value[:3]]
        return out + [f"<{len(value) - 3} more>"] if len(value) > 3 else out
    if not (isinstance(value, str) and value and _PII_KEY.search(key)):
        return value
    kind = "digits" if value.isdigit() else ("email" if _EMAIL_SHAPE.search(value) else "text")
    return f"<str len={len(value)} {kind}>"


def _pydbisam_header(client, path: Path) -> dict[str, Any]:
    """DBISAM's own counts for a table: live records, deleted slots, fields.

    Read straight from the decrypted header via pydbisam. The client never
    looks at `total_rows`; it is the independent oracle the live-row check
    compares against.
    """
    from pydbisam import PyDBISAM

    db = PyDBISAM(data=client._decrypt(path))
    return {
        "header_live_records": db.total_rows,
        "header_deleted_slots": getattr(db, "_deleted_rows", None),
        "fields": db.total_fields,
        "columns": list(db.fields()),
    }


class Probe:
    def __init__(self, db_path: str, key: str, mode: str, email_field: str):
        self.db = Path(db_path)
        self.key = key
        self.mode = mode
        self.email_field = (email_field or "").strip().upper()
        DmpClient, DmpError, DmpAdapter, default_field, status = _load_agsync()
        self._DmpError = DmpError
        self._status = status
        self.email_field = self.email_field or default_field
        self.client = DmpClient(db_path=str(self.db), encryption_key=key)
        self.adapter = DmpAdapter(db_path=str(self.db), encryption_key=key, email_field=self.email_field)
        self.report: dict[str, Any] = {
            "db_path": str(self.db),
            "mode": mode,
            "email_field": self.email_field,
            "started": datetime.now().isoformat(),
            "accessgrid": {},
            "environment": {},
            "database": {},
        }

    # -- checks ---------------------------------------------------------------

    def accessgrid(self) -> None:
        """Can this machine reach AccessGrid, as the hub will need to?

        Checked in layers so a failure names the layer — name lookup, a TCP
        connection, then HTTPS with the certificate verified, where a proxy
        that re-signs traffic shows up. Every layer runs even if one before it
        failed. Not an ICMP ping: that is often blocked where HTTPS works.
        """
        import httpx

        result = self.report["accessgrid"]
        result["host"] = ACCESSGRID_HOST
        # Validate against the OS trust store, as the hub does, so `reachable`
        # reflects what the hub will see — not a certifi-only verdict that
        # fails on a network whose inspection CA only Windows trusts.
        try:
            import truststore

            truststore.inject_into_ssl()
            result["trust_store"] = "os"
        except Exception as e:
            result["trust_store"] = f"certifi (truststore unavailable: {e})"
        # Names only: a proxy URL can carry a username and password.
        result["proxy_env_vars"] = [k for k in _PROXY_VARS if os.environ.get(k)]
        try:
            result["dns"] = sorted({i[4][0] for i in socket.getaddrinfo(ACCESSGRID_HOST, 443, type=socket.SOCK_STREAM)})
        except OSError as e:
            result["dns"] = f"failed: {e}"
        t = time.monotonic()
        try:
            with socket.create_connection((ACCESSGRID_HOST, 443), timeout=10):
                result["tcp_443"] = f"ok in {time.monotonic() - t:.2f}s"
        except OSError as e:
            result["tcp_443"] = f"failed: {type(e).__name__}: {e}"
        t = time.monotonic()
        try:
            resp = httpx.get(f"https://{ACCESSGRID_HOST}/", timeout=15, headers={"User-Agent": "AGSyncProbe/1"})
            result["https"] = f"ok: HTTP {resp.status_code} in {time.monotonic() - t:.2f}s"
        except httpx.HTTPError as e:
            result["https"] = f"failed: {type(e).__name__}: {e}"
        result["reachable"] = str(result.get("https", "")).startswith("ok")

    def environment(self) -> None:
        """The libraries the adapter needs to decrypt and parse DBISAM."""
        env = self.report["environment"]
        for name in ("pydbisam", "cryptography"):
            try:
                mod = importlib.import_module(name)
                env[name] = getattr(mod, "__version__", "present")
            except ImportError as e:
                env[name] = f"MISSING: {e}"
        env["python"] = sys.version.split()[0]

    def database(self) -> None:
        """What is in the folder: which known tables, each one's size."""
        db = self.report["database"]
        db["folder_exists"] = self.db.is_dir()
        if not self.db.is_dir():
            db["error"] = f"{self.db} is not a directory"
            return
        db["users_dat_present"] = (self.db / "Users.dat").exists()
        tables: dict[str, Any] = {}
        for name in _KNOWN_TABLES:
            path = self.db / f"{name}.dat"
            tables[name] = {"present": path.exists(), "bytes": path.stat().st_size if path.exists() else 0}
        db["known_tables"] = tables
        db["total_dat_files"] = len(list(self.db.glob("*.dat")))

    def decrypt(self) -> dict[str, Any]:
        """Does the key decrypt Users.dat into something that parses?

        The client raises DmpError when the key is wrong (the garbage it
        decrypts fails to parse as DBISAM fields), so a clean read is the
        signal the key is right.
        """
        out = self.report["decrypt"] = {}
        users = self.db / "Users.dat"
        if not users.exists():
            out["key_accepted"] = False
            out["error"] = "Users.dat not found"
            return out
        try:
            header = _pydbisam_header(self.client, users)
        except Exception as e:
            out["key_accepted"] = False
            out["error"] = f"{type(e).__name__}: {e}"
            return out
        out["key_accepted"] = True
        out["columns"] = header["columns"]
        out["column_count"] = header["fields"]
        return out

    def live_rows(self) -> None:
        """Does our live-row filtering reproduce DBISAM's own record count?

        header_live is what DBISAM stored; client_live is what read_users()
        returns after filtering ghosts and de-duplicating reused slots. They
        matched exactly on the reference database — a mismatch here is the
        first thing to look at.
        """
        out = self.report["live_rows"] = {}
        users = self.db / "Users.dat"
        try:
            header = _pydbisam_header(self.client, users)
            raw = self.client._read_table("Users")
            live = self.client.read_users()
        except self._DmpError as e:
            out["error"] = str(e)
            return
        out["header_live_records"] = header["header_live_records"]
        out["header_deleted_slots"] = header["header_deleted_slots"]
        out["raw_parseable_slots"] = len(raw)
        out["client_live_users"] = len(live)
        out["ghosts_filtered"] = len(raw) - len(live)
        out["matches_header"] = header["header_live_records"] == len(live)
        # The fallback path: with no panel compare we can't tell live from
        # deleted and keep everything. Say whether that path is in play.
        has_compare = any(r.get("LAST_PNL_CMP") for r in raw)
        out["panel_compare_populated"] = has_compare
        if not has_compare:
            out["note"] = "no LAST_PNL_CMP in any row — client falls back to every parseable row"

    def mapping(self) -> None:
        """What the adapter makes of the rows: people, triggers, collisions."""
        out = self.report["mapping"] = {}
        try:
            people = list(self.adapter.list_people())
        except self._DmpError as e:
            out["error"] = str(e)
            return
        out["people"] = len(people)
        out["active"] = sum(1 for p in people if p.active)
        out["with_email_trigger"] = sum(1 for p in people if p.email)
        out["with_department"] = sum(1 for p in people if p.department)
        # NAME truncates at 16 chars in DMP; count how many sit at the cap, a
        # hint the operator's names are being clipped.
        out["names_at_16_char_cap"] = sum(1 for p in people if len(p.full_name) == 16)
        out["blank_names"] = sum(1 for p in people if not p.full_name)

        # Credentials: one per person, keyed on CODE (the card number). The
        # adapter's code->row dict silently drops a row whose CODE collides
        # with another live row, so compare people against distinct cards.
        creds = [c for p in people for c in self.adapter.list_credentials(p.id)]
        codes = [c.card_number for c in creds if c.card_number]
        out["credentials"] = len(creds)
        out["with_card_number"] = len(codes)
        out["distinct_card_numbers"] = len(set(codes))
        out["card_number_collisions"] = len(codes) - len(set(codes))
        out["trigger_active"] = sum(1 for c in creds if c.trigger_active)
        out["status_active"] = sum(1 for c in creds if c.status == self._status.ACTIVE)
        out["status_suspended"] = sum(1 for c in creds if c.status == self._status.SUSPENDED)
        # One redacted sample so the shapes are visible without any real value.
        if people:
            p = people[0]
            out["sample_person"] = {
                "id": redact(p.id, "code"),
                "full_name": redact(p.full_name, "name"),
                "email": redact(p.email, "email"),
                "department": redact(p.department, "department"),
                "active": p.active,
            }

    def email_scan(self) -> None:
        """Which User Field actually holds the enrollment email?

        Reports, per candidate field, how many live users hold an email-shaped
        value, and recommends one. A configured field with zero hits is the
        usual reason nobody enrolls.
        """
        out = self.report["email_field_scan"] = {}
        try:
            rows = self.client.read_users()
        except self._DmpError as e:
            out["error"] = str(e)
            return
        counts: dict[str, int] = {}
        # Every column, so a site that used an unexpected field still surfaces.
        columns = set(rows[0].keys()) if rows else set()
        candidates = [f for f in _EMAIL_FIELD_CANDIDATES if f in columns]
        candidates += sorted(c for c in columns if c not in candidates and c.startswith("U_FIELD"))
        for field in candidates:
            counts[field] = sum(1 for r in rows if _EMAIL_SHAPE.search(str(r.get(field) or "")))
        # Also catch emails hiding in any other column.
        other = {
            c: sum(1 for r in rows if _EMAIL_SHAPE.search(str(r.get(c) or "")))
            for c in columns
            if c not in counts
        }
        out["configured_field"] = self.email_field
        out["configured_field_hits"] = counts.get(self.email_field, 0)
        out["user_field_hits"] = counts
        out["other_columns_with_emails"] = {c: n for c, n in other.items() if n}
        best = max(counts, key=counts.get, default=None)
        if best and counts[best] and best != self.email_field:
            out["recommendation"] = f"configure email_field={best} ({counts[best]} users have an email there)"
        elif counts.get(self.email_field):
            out["recommendation"] = f"configured field {self.email_field} is correct"
        else:
            out["recommendation"] = "no User Field holds emails — enrollment will stay empty until one does"

    def slots(self) -> None:
        """Every record slot in Users.dat, and how the hub classifies it.

        DBISAM frees a deleted user's slot but leaves its bytes. Each record
        starts with a header (status byte, two 32-bit values, a 16-byte
        checksum, as pydbisam reads it); this reports it without the checksum,
        with how the hub reads the record and whether the status bytes or the
        panel-compare dates decided. No names or card numbers: a slot is
        identified by its user number.
        """
        from pydbisam import PyDBISAM

        out = self.report["slots"] = {}
        try:
            db = PyDBISAM(data=self.client._decrypt(self.db / "Users.dat"))
            cols = db.fields()
        except Exception as e:  # noqa: BLE001 — a probe reports, it does not stop
            out["error"] = f"{type(e).__name__}: {e}"
            return
        # The first field's null-marker byte sits just before its offset.
        header_len = db._columns[0].row_offset - 1
        rows: list[tuple[int, dict | None, bytes]] = []
        for i in range(db.total_rows + db._deleted_rows):
            start = db._data_offset + i * db._row_size
            head = bytes(db._data[start:start + header_len])
            try:
                values = db.row(i, extract_deleted=True)
                row = dict(zip(cols, values, strict=False)) if values else None
            except Exception:  # noqa: BLE001
                row = None
            rows.append((i, row, head))

        parsed = [(head[0], r) for _, r, head in rows if r is not None]
        current, how = self.client.classify(parsed, db.total_rows)
        live = {id(r) for r in current}
        listed, by_class = [], {}
        for i, row, head in rows:
            if row is None:
                kind = "unparseable"
            elif not self.client._valid(row):
                kind = "blank"
            elif id(row) in live:
                kind = "current"
            else:
                kind = "deleted" if how == "status" else "deleted (by dates or reused number)"
            status = f"0x{head[0]:02x}" if head else ""
            by_class.setdefault(kind, {}).setdefault(status, 0)
            by_class[kind][status] += 1
            listed.append({
                "slot": i,
                "hub_reads_as": kind,
                "status_byte": status,
                # The two 32-bit values after it, and anything past the checksum.
                "header_1_4": int.from_bytes(head[1:5], "little") if len(head) >= 5 else None,
                "header_5_8": int.from_bytes(head[5:9], "little") if len(head) >= 9 else None,
                "header_after_checksum": head[25:].hex() if len(head) > 25 else "",
                "user_num": row.get("USER_NUM") if row else None,
                "active": row.get("ACTIVE") if row else None,
                "last_pnl_cmp": row.get("LAST_PNL_CMP") if row else None,
                "last_change": row.get("LAST_CHANGE") if row else None,
            })
        out["header_live_records"] = db.total_rows
        out["header_deleted_slots"] = db._deleted_rows
        out["record_header_bytes"] = header_len
        out["hub_reads_as_current"] = sum(1 for s in listed if s["hub_reads_as"] == "current")
        # "status": each record's own flag agreed with the header and decided;
        # "dates": it did not, and panel-compare dates decided.
        out["told_apart_by"] = how
        out["status_byte_by_class"] = by_class
        idx = self.db / "Users.idx"
        if idx.exists():
            st = idx.stat()
            out["users_idx"] = {"bytes": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).isoformat()}
        out["slots"] = listed

    # -- runners --------------------------------------------------------------

    def run_all(self) -> dict[str, Any]:
        self.accessgrid()
        self.environment()
        self.database()
        if not self.report["database"].get("users_dat_present"):
            return self.report
        self.decrypt()
        if not self.report["decrypt"].get("key_accepted"):
            return self.report
        self.live_rows()
        self.mapping()
        self.email_scan()
        return self.report

    def run_tables(self) -> dict[str, Any]:
        self.environment()
        self.database()
        tables = self.report["database"].get("known_tables", {})
        headers: dict[str, Any] = {}
        for name, info in tables.items():
            if not info["present"]:
                continue
            path = self.db / f"{name}.dat"
            try:
                h = _pydbisam_header(self.client, path)
                headers[name] = {
                    "live_records": h["header_live_records"],
                    "deleted_slots": h["header_deleted_slots"],
                    "columns": h["fields"],
                }
            except Exception as e:
                headers[name] = {"error": f"{type(e).__name__}: {e}"}
        self.report["table_headers"] = headers
        return self.report

    def run_slots(self) -> dict[str, Any]:
        self.environment()
        self.database()
        if not self.report["database"].get("users_dat_present"):
            return self.report
        self.decrypt()
        if self.report["decrypt"].get("key_accepted"):
            self.slots()
        return self.report

    def run_fields(self) -> dict[str, Any]:
        self.environment()
        self.database()
        if not self.report["database"].get("users_dat_present"):
            return self.report
        self.decrypt()
        if self.report["decrypt"].get("key_accepted"):
            self.email_scan()
        return self.report


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] not in MODES and not argv[0].startswith("-"):
        argv.insert(0, "all")  # <db_folder> with no mode means `all`
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=MODES, help="all: every check; tables: inventory the folder; "
                                               "fields: find which User Field holds the email; "
                                               "slots: every Users.dat record and how the hub reads it")
    ap.add_argument("db_path", help=r"DMP database folder, e.g. C:\ProgramData\DMP\SystemLink\Database")
    ap.add_argument("--email-field", default="", help="User Field holding the email (default U_FIELD1)")
    args = ap.parse_args(argv)

    # Prompted, never logged. DMP_PROBE_KEY lets an unattended run skip the prompt.
    key = os.environ.get("DMP_PROBE_KEY") or getpass.getpass("DMP encryption key: ")

    probe = Probe(args.db_path, key, args.mode, args.email_field)
    if args.mode == "tables":
        report = probe.run_tables()
    elif args.mode == "fields":
        report = probe.run_fields()
    elif args.mode == "slots":
        report = probe.run_slots()
    else:
        report = probe.run_all()
    report["finished"] = datetime.now().isoformat()

    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out = f"dmp-probe-{args.mode}-{stamp}.json"
    text = json.dumps(report, indent=2, default=str)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nSaved to {out}")
    # Exit 0 only when the key was accepted (or a mode that doesn't need it ran).
    if args.mode == "tables":
        return 0
    return 0 if report.get("decrypt", {}).get("key_accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
