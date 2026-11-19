"""ag-cleanup: give the hub back the Unity tokens it created but never recorded.

Before 0.2.37 the hub refused a Seos token it had just created in Unity when
Unity's redirect did not name it (seen live, Nov 2026). The token stayed in
Unity; the hub's record of it (a `unity_tokens` row) kept no token ID. From
then on the hub held the pass on purpose — a token it cannot prove it wrote is
never adopted, changed or deleted — so the enrollment token was never deleted
and a deleted pair half's token never retired.

This tool records the token in its row, which is what the hub would have done
had it accepted the token. Run it after upgrading the hub, so the next token it
creates is accepted. It writes one thing — the token ID into a row that has
none — and never writes to Unity. It adopts a token only when exactly one
Unity token can be the one the hub made: on the row's identity, carrying the
row's card number and the accessgrid marker, not the enrollment token, and not
recorded on any other row. Anything else is reported and left alone.

The database is opened directly, never through `agsync.db`: that runs
migrations, and this tool must not change anything but the rows it adopts.
It is backed up first.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .diagnostics import _decrypt, resolve_db_path, resolve_key


class CleanupError(RuntimeError):
    """Something that stops the tool before it changes anything."""


@dataclass
class Stranded:
    """A row the hub reserved for a token whose ID it never recorded."""

    row: int
    person_id: str
    credential_id: str
    card_number: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    problem: str = ""

    @property
    def adoptable(self) -> bool:
        return not self.problem and len(self.candidates) == 1


def stranded_rows(conn: sqlite3.Connection, key: str) -> tuple[list[Stranded], set[str]]:
    """Rows with no token ID, and every token ID recorded on any row."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'unity_tokens'").fetchone():
        return [], set()
    rows = conn.execute("SELECT id, pacs_person_id, pacs_credential_id, token_id, card_number "
                        "FROM unity_tokens ORDER BY id").fetchall()
    known = {r["token_id"] for r in rows if r["token_id"]}
    out = [Stranded(r["id"], r["pacs_person_id"], r["pacs_credential_id"], _decrypt(r["card_number"], key))
           for r in rows if not r["token_id"]]
    return out, known


def unity_params(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM settings WHERE key = 'pacs'").fetchone()
    if row is None:
        raise CleanupError("no PACS is configured in this install")
    config = json.loads(_decrypt(row["value"], key))
    if config.get("vendor") != "avigilon":
        raise CleanupError(f"this install syncs {config.get('vendor')!r}, not Avigilon Unity — nothing to clean up")
    return config.get("params") or {}


def find_candidates(found: list[Stranded], known: set[str], client: Any) -> None:
    """Fill each row's candidates from Unity: the tokens the hub could have made."""
    from .lib.pacs.avigilon.adapter import TRIGGER_VALUE
    from .lib.pacs.avigilon.client import UnityError

    for s in found:
        try:
            tokens = client.list_tokens(s.person_id)
        except UnityError as e:
            s.problem = f"could not read this identity's tokens from Unity: {e}"
            continue
        logging.getLogger(__name__).info(
            "row %s (identity %s, pass %s, card %s): Unity holds %s", s.row, s.person_id, s.credential_id,
            s.card_number, "; ".join(f"{t['id']} number={t['internal_number']!r} embossed={t['embossed_number']!r} "
                                     f"status={t['status']} issued={t['issue_date']!r}" for t in tokens) or "no tokens")
        s.candidates = [t for t in tokens
                        if t["internal_number"] == s.card_number
                        and t["embossed_number"].strip().lower() == TRIGGER_VALUE
                        and t["id"] != s.credential_id and t["id"] not in known]
        if not s.candidates:
            s.problem = ("no unrecorded token in Unity carries this card number — nothing to adopt; "
                         "the hub creates the token itself on its next cycle")
        elif len(s.candidates) > 1:
            s.problem = (f"{len(s.candidates)} unrecorded tokens carry this card number "
                         f"({', '.join(t['id'] for t in s.candidates)}) — which one the hub made cannot be told; "
                         "left alone, review them in Unity")


def backup(conn: sqlite3.Connection, path: Path) -> Path:
    target = path.with_name(f"{path.name}.before-ag-cleanup-{datetime.now():%Y%m%d-%H%M%S}")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def adopt(conn: sqlite3.Connection, found: list[Stranded]) -> int:
    """Record each adoptable token in its row; all or nothing."""
    adopted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for s in found:
            if not s.adoptable:
                continue
            cur = conn.execute("UPDATE unity_tokens SET token_id = ? WHERE id = ? AND "
                               "(token_id IS NULL OR token_id = '')", (s.candidates[0]["id"], s.row))
            if cur.rowcount != 1:
                raise CleanupError(f"row {s.row} changed while this ran — nothing was written; run it again")
            adopted += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return adopted


def _describe(s: Stranded) -> str:
    head = f"  identity {s.person_id}, pass {s.credential_id}, card {s.card_number} (row {s.row})"
    if s.problem:
        return f"{head}\n      LEFT ALONE: {s.problem}"
    t = s.candidates[0]
    return (f"{head}\n      ADOPT token {t['id']} — status {t['status']}, issued {t['issue_date'] or '?'}")


def run(db_path: Path, key: str | None, client_factory: Any, confirm: Any, out: Any = print) -> int:
    if not db_path.exists():
        raise CleanupError(f"no database at {db_path} (pass --db if the hub keeps it elsewhere)")
    if key is None:
        raise CleanupError("no encryption key found (set AG_SYNC_ENCRYPTION_KEY as the hub has it)")
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        found, known = stranded_rows(conn, key)
        if not found:
            out("Nothing to clean up: every Unity token the hub reserved has its token ID recorded.")
            return 0
        client = client_factory(unity_params(conn, key))
        find_candidates(found, known, client)
        out(f"{len(found)} reservation(s) without a recorded token:")
        for s in found:
            out(_describe(s))
        todo = [s for s in found if s.adoptable]
        if not todo:
            out("\nNothing can be adopted safely. Nothing was changed.")
            return 2
        if not confirm(len(todo)):
            out("\nNothing was changed.")
            return 2
        saved = backup(conn, db_path)
        out(f"\nDatabase backed up to {saved}")
        adopted = adopt(conn, found)
        for s in found:
            if s.adoptable:
                logging.getLogger(__name__).info("row %s: token_id set to %s", s.row, s.candidates[0]["id"])
        out(f"Adopted {adopted} token(s). The hub picks them up on its next cycle: it then writes any "
            "token still missing, deletes the enrollment token, and retires a deleted pair half's token.")
        return 0 if adopted == len(found) else 2
    finally:
        conn.close()


def _client_from(params: dict[str, Any]) -> Any:
    from .lib.pacs.avigilon.client import UnityClient

    if not params.get("host") or not params.get("username"):
        raise CleanupError("the saved Unity connection has no host or username")
    return UnityClient(host=params["host"], username=params["username"], password=params.get("password", ""),
                       verify_ssl=bool(params.get("verify_ssl", False)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ag-cleanup",
        description="Record the Unity tokens the hub created but never recorded, so their passes finish "
                    "issuing. Reads Unity; writes only the hub's own database, after a backup.")
    parser.add_argument("--db", type=Path, help="the hub's app.db (default: where the hub keeps it)")
    parser.add_argument("--yes", action="store_true", help="adopt without asking")
    args = parser.parse_args(argv)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.getLogger().addHandler(console)

    def confirm(n: int) -> bool:
        if args.yes:
            return True
        try:
            answer = input(f"\nType ADOPT to record {n} token(s) in the hub's database: ")
        except EOFError:
            return False
        return answer.strip() == "ADOPT"

    from . import __version__

    db_path = args.db or resolve_db_path()
    out, log_path = _tee(db_path)
    key = resolve_key()
    out(f"ag-cleanup {__version__} — {datetime.now():%Y-%m-%d %H:%M:%S}")
    out(f"  database {db_path}")
    out(f"  key      {'found' if key else 'NOT FOUND'}")
    out(f"  log      {log_path or 'not written (the database folder is missing or read-only)'}\n")
    try:
        code = run(db_path, key, _client_from, confirm, out)
    except CleanupError as e:
        out(f"Stopped, nothing changed: {e}")
        code = 1
    except Exception:
        logging.getLogger(__name__).exception("ag-cleanup failed")
        out("Stopped by an unexpected error (details above and in the log). If it happened while adopting, "
            "nothing was written: adoption is all or nothing.")
        code = 1
    out(f"\nExit code {code}.")
    return code


def _tee(db_path: Path) -> tuple[Any, Path | None]:
    """print, also into a log file next to the database, with the Unity
    client's own log lines, so a closed window loses nothing."""
    log_path: Path | None = db_path.parent / f"ag-cleanup-{datetime.now():%Y%m%d-%H%M%S}.log"
    try:
        handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        return print, None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    report = logging.getLogger("ag-cleanup")

    def out(line: str) -> None:
        print(line)
        report.info("%s", line)

    return out, log_path


if __name__ == "__main__":
    sys.exit(main())
