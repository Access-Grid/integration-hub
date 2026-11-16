#!/usr/bin/env python3
"""Export a DMP System Link DBISAM database to CSV via the DBISAM ODBC driver.

This is the RELIABLE extraction path and the intended way to get real,
decrypted rows out of a System Link install. It must run on a machine that has
Elevate Software's **DBISAM ODBC driver** (Windows only) -- typically the
customer's System Link / Link Server box. It cannot run on macOS/Linux (no
driver exists) and it is NOT what recovers data from a raw offline `.dat` dump.

Why this beats parsing the .dat files ourselves
-----------------------------------------------
The data tables are DBISAM-encrypted (Blowfish, compiled-in password we do not
have), so our pure-Python readers can only touch the one cleartext table
(DebugTable). The ODBC driver uses the real engine, and there are two ways it
gets the password:

  * Server connection (BEST): point the DSN at the running **DBISAM Database
    Server** that Link Server starts. That server already loaded the table
    passwords, so it serves DECRYPTED rows and we never need the password.
  * Direct file connection: the driver opens the .dat files itself and you must
    supply the table password (``--password``). Without it, encrypted tables
    fail with a password prompt/error -- the same wall as pydbisam.

Connection options
------------------
  # Preconfigured DSN (e.g. one that talks to the Link Server DBISAM service):
  python export_via_odbc.py --dsn DMP_SYSTEMLINK_READONLY

  # Explicit driver + local catalog directory of .dat files (+ password if the
  # tables are encrypted and you know it):
  python export_via_odbc.py \\
      --driver "DBISAM 4 ODBC Driver" \\
      --catalog "C:\\ProgramData\\DMP\\SystemLink\\Database" \\
      --password "<table password>"

Notes
-----
* Requires ``pyodbc`` (``pip install pyodbc``) plus the DBISAM ODBC driver.
* Exports every TABLE the connection can see, one CSV per table, continuing
  past any table it cannot read (logged, not fatal).
* Read-only: only issues SELECT. Never writes to the database.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


def build_conn_str(args) -> str:
    if args.dsn:
        parts = [f"DSN={args.dsn}"]
    else:
        if not (args.driver and args.catalog):
            sys.exit("Provide either --dsn, or both --driver and --catalog.")
        # DBISAM ODBC driver params. ConnectionType=Local opens files directly;
        # use a DSN pointed at the server for the no-password server path.
        parts = [
            f"DRIVER={{{args.driver}}}",
            "ConnectionType=Local",
            f"CatalogName={args.catalog}",
        ]
        if args.password:
            # DBISAM exposes the table/encryption password under a few names
            # across driver versions; send the common ones.
            parts += [
                f"Password={args.password}",
                f"CatalogPassword={args.password}",
            ]
    return ";".join(parts) + ";"


def export(args) -> int:
    try:
        import pyodbc
    except ImportError:
        sys.exit("pyodbc is required: pip install pyodbc (and install the "
                 "DBISAM ODBC driver on this machine).")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = pyodbc.connect(build_conn_str(args), autocommit=True)
    cur = conn.cursor()

    tables = sorted({r.table_name for r in cur.tables(tableType="TABLE")
                     if r.table_name})
    if not tables:
        print("No tables visible on this connection.")
        return 1

    print(f"{len(tables)} tables visible. Exporting to {out_dir}/\n")
    ok = fail = 0
    for name in tables:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        path = out_dir / f"{safe}.csv"
        try:
            tc = conn.cursor()
            tc.execute(f'SELECT * FROM "{name}"')          # quote identifiers
            cols = [d[0] for d in tc.description]
            n = 0
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                while True:
                    rows = tc.fetchmany(1000)
                    if not rows:
                        break
                    w.writerows(rows)
                    n += len(rows)
            print(f"  OK   {name:<22} {n:>7} rows -> {path.name}")
            ok += 1
        except pyodbc.Error as e:
            # Encrypted-table-without-password lands here.
            print(f"  FAIL {name:<22} {e}")
            fail += 1

    print(f"\nDone. {ok} exported, {fail} failed.")
    if fail:
        print("Failures on data tables usually mean the table password was not "
              "supplied and the DSN is opening files directly rather than "
              "talking to the running DBISAM/Link Server. See module docstring.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", help="preconfigured ODBC DSN name")
    ap.add_argument("--driver", help="DBISAM ODBC driver name (no DSN)")
    ap.add_argument("--catalog", help="path to the directory of .dat files")
    ap.add_argument("--password", help="table encryption password (direct-file mode)")
    ap.add_argument("--out", default="dmp_dbisam_export", help="output directory")
    export(ap.parse_args(argv))


if __name__ == "__main__":
    main()
