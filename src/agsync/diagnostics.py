"""A read-only dump of what this install believes it has issued.

Built as a separate executable (agdiag) because the question it answers —
"what does the tracking table and the Seos ledger actually hold?" — has so
far only been inferable from log lines. A rule that decides whether to delete
somebody's pass should be checked against the real rows first.

Three properties it must keep, in order of how badly each would hurt:

  * **No secrets in the output.** The settings table holds the AccessGrid
    API key and the PACS session cookie. This prints which keys exist and
    nothing else, because the output is meant to be pasted into a chat or a
    ticket. The one blob it decrypts is the Seos ledger, which holds card
    numbers and no credentials.
  * **No writes, and no migrations.** Opening the database through
    `agsync.db` runs any pending migration, which once rewrote a live
    install's ledger out from under the running service. This opens SQLite
    directly, read-only, through a URI.
  * **No side effects on disk.** The app generates an encryption key on
    first run if none exists; a diagnostic must never do that, or running it
    against the wrong directory would leave a key that makes the real one
    look wrong later.

Contact details are left out too — names are kept, because correlating rows
with cardholders is the whole point, but nobody needs the roster's email
addresses in a bug report.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

# Settings blobs that hold credentials. No value from this table is ever
# printed — only the key names — but these are called out so the output says
# plainly that something was withheld rather than appearing to be complete.
_NEVER_PRINT = {"accessgrid", "pacs", "notifications"}
# A PACS session is stored per vendor, so its key carries a suffix.
_NEVER_PRINT_PREFIXES = ("pacs_session:",)

# The ledger of what we wrote into Millennium: card numbers and slots, no
# secrets. The reason this tool exists.
_SEOS_LEDGER_KEY = "millennium_seos_slots"


def _data_dir() -> Path:
    """Where the app keeps its database and key, without importing config.

    Mirrors `config._data_dir`. Duplicated on purpose: importing the real one
    pulls in a settings object whose default factory writes an encryption key
    to disk, which a read-only tool must not do.
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "AGSyncTool"


def resolve_db_path() -> Path:
    override = os.environ.get("AG_SYNC_DB_PATH")
    return Path(override) if override else _data_dir() / "app.db"


def resolve_key() -> str | None:
    """The encryption key, if one already exists. Never creates one."""
    env = os.environ.get("AG_SYNC_ENCRYPTION_KEY")
    if env:
        return env.strip()
    key_file = _data_dir() / "encryption.key"
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def open_readonly(path: Path) -> sqlite3.Connection:
    """SQLite opened so that nothing can write and no migration can run."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _decrypt(ciphertext: str, key: str) -> str:
    from cryptography.fernet import Fernet

    return Fernet(key.encode()).decrypt(ciphertext.encode()).decode()


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql))
    except sqlite3.Error:
        return []


def _tracking(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in _rows(conn, "SELECT * FROM ag_credentials ORDER BY pacs_person_id"):
        keys = r.keys()
        out.append({
            "person": r["pacs_person_id"],
            "credential": r["pacs_credential_id"],
            "name": r["full_name"] or "",
            "status": r["status"],
            "ag_card_id": r["ag_card_id"],
            "sync_ref": (r["sync_ref"] if "sync_ref" in keys else "") or "",
            "last_known_ag_state": r["last_known_ag_state"] or "",
            "retry_count": r["retry_count"] or 0,
            "sync_error": r["sync_error"],
            "updated_at": r["updated_at"],
        })
    return out


def _ledger(conn: sqlite3.Connection, key: str | None) -> tuple[dict[str, Any], str]:
    row = next(
        iter(_rows(conn, f"SELECT value FROM settings WHERE key = '{_SEOS_LEDGER_KEY}'")),
        None,
    )
    if row is None:
        return {}, "no ledger stored"
    if key is None:
        return {}, "a ledger is stored, but no encryption key was found to read it"
    try:
        return json.loads(_decrypt(row["value"], key)), ""
    except Exception as e:  # noqa: BLE001 — reported, never raised
        return {}, f"could not decrypt the ledger: {type(e).__name__}: {e}"


def report() -> str:
    """The whole dump, as text meant to be pasted somewhere."""
    from . import __version__

    path = resolve_db_path()
    lines: list[str] = [
        "AccessGrid Sync — diagnostic dump",
        f"  version   {__version__}",
        f"  database  {path}",
    ]
    if not path.exists():
        lines.append("\nNo database at that path. Set AG_SYNC_DB_PATH if it lives elsewhere.")
        return "\n".join(lines)

    key = resolve_key()
    lines.append(f"  key       {'found' if key else 'NOT FOUND — the ledger cannot be read'}")

    conn = open_readonly(path)
    try:
        applied = [r["name"] for r in _rows(conn, "SELECT name FROM _migrations ORDER BY name")]
        lines += ["", f"Migrations applied ({len(applied)}):"]
        lines += [f"  {name}" for name in applied] or ["  none"]

        tracked = _tracking(conn)
        lines += ["", f"Tracking table — ag_credentials ({len(tracked)} rows):"]
        if not tracked:
            lines.append("  none")
        for t in tracked:
            lines.append(
                f"  {t['person']}/{t['credential']}  {t['name']!r}"
                f"\n      status={t['status']}  ag_card_id={t['ag_card_id'] or 'none'}"
                f"  last_known={t['last_known_ag_state'] or '-'}"
                f"\n      sync_ref={t['sync_ref'] or 'none'}  retries={t['retry_count']}"
                f"  updated={t['updated_at']}"
                + (f"\n      error={t['sync_error']}" if t["sync_error"] else "")
            )

        ledger, problem = _ledger(conn, key)
        lines += ["", f"Seos ledger — what we wrote into Millennium ({len(ledger)} entries):"]
        if problem:
            lines.append(f"  {problem}")
        if not ledger and not problem:
            lines.append("  none")
        for entry_key, entries in sorted(ledger.items()):
            cards = ", ".join(
                f"{e.get('facility_code')}/{e.get('card_number')}@slot{e.get('slot')}"
                for e in entries
            )
            lines.append(f"  {entry_key}  ->  {cards or 'nothing'}")

        lines += ["", "Cross-reference:"]
        lines += _cross_reference(tracked, ledger) or ["  nothing inconsistent"]

        present = sorted(r["key"] for r in _rows(conn, "SELECT key FROM settings"))
        lines += ["", "Settings keys present (values withheld):"]
        for name in present:
            secret = name in _NEVER_PRINT or name.startswith(_NEVER_PRINT_PREFIXES)
            note = "  [holds credentials — not printed]" if secret else ""
            lines.append(f"  {name}{note}")
    finally:
        conn.close()

    return "\n".join(lines)


def _cross_reference(
    tracked: list[dict[str, Any]], ledger: dict[str, Any]
) -> list[str]:
    """Where the two records disagree about who has what.

    The rows that matter for anything deciding to delete a pass: a tracked
    credential with no record of the cards behind it, and cards recorded for
    a credential nothing is tracking.
    """
    out: list[str] = []
    ledger_keys = set(ledger)
    tracked_keys = {f"{t['person']}:{t['credential']}" for t in tracked}

    for t in tracked:
        key = f"{t['person']}:{t['credential']}"
        live = t["status"] not in ("deleted", "deduped") and t["ag_card_id"]
        if live and key not in ledger_keys:
            out.append(
                f"  {key} holds a pass ({t['ag_card_id']}) but the ledger records "
                f"no cards for it"
            )
    for key in sorted(ledger_keys - tracked_keys):
        out.append(f"  {key} has cards recorded but no tracking row")
    for key in sorted(ledger_keys & tracked_keys):
        row = next(t for t in tracked if f"{t['person']}:{t['credential']}" == key)
        if row["status"] == "deleted":
            out.append(
                f"  {key} is marked deleted but still has cards recorded — "
                f"those slots will never be released"
            )
    return out


def main() -> int:
    """Print the report, or write it to a file given as the first argument."""
    import sys

    text = report()
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text, encoding="utf-8")
        print(f"Wrote {sys.argv[1]}")
    else:
        print(text)
    return 0
