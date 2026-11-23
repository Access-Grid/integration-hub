"""Read the DMP System Link DBISAM database (decrypt + parse live users).

DMP System Link stores its data in a DBISAM v4 database with per-table
Blowfish encryption. Only the region from file offset 0x200 onward is
encrypted; the basic header (row count, row size, field count) stays plaintext.

Encryption scheme (validated against a live customer database):
    key  = MD5(table password)          # full 16 bytes
    algo = Blowfish, big-endian, CBC, IV = 0
    units: the field definitions (0x200 up to the first record) are one CBC
           run; each record is its own run, from IV 0 again

Decrypting the records as one run with the definitions reads every field
correctly but scrambles each record's first 8 bytes — the status byte and
record IDs — with the previous record's ciphertext. Seen in two dmp-discovery
`slots` runs (Nov 2026): a record's first 8 bytes changed exactly when the
record before it changed, and at no other time.

The table password is a value the operator supplies via the setup wizard; it is
never stored in code. We read the tables directly off disk each cycle — System
Link writes them as it programs panels — so no export step is needed.

Live vs. deleted rows
---------------------
A deleted record keeps its bytes in the .dat, so a raw read surfaces deleted
records alongside live ones. Each record's status byte (its first) is 0 while
it is live. That is trusted only when the records it calls live number exactly
what the table's plaintext header says are live — the header count is DBISAM's
own and exact.

Where they disagree, the read falls back to panel-compare dates, which is how
every read worked before: a live row
sits in the most-recent ``LAST_PNL_CMP`` cohort (System Link stamps every live
user on each panel compare), or was edited after it (``LAST_CHANGE`` newer than
the last compare). Reused-number slots are then de-duplicated to the most
recently active record. This reproduced the header's live count exactly on the
reference database. If the database has never been compared to a panel
(``LAST_PNL_CMP`` all null) we can't distinguish, and fall back to every
parseable row. The dates cannot see a user deleted since the last compare —
the compare stamped them like everyone else — so in that fallback such a user
reads as current, and their pass is not deleted; read_users() says so.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes

logger = logging.getLogger(__name__)

_ENC_OFFSET = 0x200
_FIELD_DEF_SIZE = 768  # one field definition, as pydbisam reads it
# A record's first byte: 0 while it is live.
_LIVE = 0
# Rows whose LAST_PNL_CMP is within this window of the newest one are treated as
# belonging to the same panel-compare pass (a compare stamps all live users
# within seconds); ghosts are days/months older.
_COMPARE_WINDOW = timedelta(days=1)

# pydbisam doesn't enumerate DBISAM BLOB subtypes (memo/wide-blob); map them to
# BLOB so the reader skips them cleanly instead of raising. Applied at import.
try:  # pragma: no cover - trivial import guard
    import pydbisam.field as _pdb_field

    for _tid in (5635, 5379, 5507, 5891):
        _pdb_field.FieldType._value2member_map_.setdefault(
            _tid, _pdb_field.FieldType.BLOB
        )
    _PYDBISAM_OK = True
except Exception:  # pragma: no cover
    _PYDBISAM_OK = False


def _cipher_runs(data: bytes) -> list[tuple[int, int]]:
    """(start, end) of each separately encrypted run after the plaintext header.

    The field definitions are one run and each record another. The record
    size and field count come from the plaintext header; if they do not lay
    out in whole cipher blocks, the file is decrypted as a single run, as
    before — fields still read right, only record status bytes would not.
    """
    if len(data) < _ENC_OFFSET:
        return [(_ENC_OFFSET, len(data))]
    row_size = int.from_bytes(data[0x2D:0x2F], "little")
    fields = int.from_bytes(data[0x2F:0x31], "little")
    first_row = _ENC_OFFSET + fields * _FIELD_DEF_SIZE
    if not row_size or row_size % 8 or first_row % 8 or first_row > len(data):
        return [(_ENC_OFFSET, len(data))]
    runs = [(_ENC_OFFSET, first_row)]
    runs += [(start, min(start + row_size, len(data))) for start in range(first_row, len(data), row_size)]
    return runs


class DmpError(Exception):
    """Raised when the DMP database can't be read or decrypted."""


class DmpClient:
    """Reads and decrypts DMP System Link DBISAM tables from a folder on disk."""

    def __init__(self, db_path: str, encryption_key: str) -> None:
        self._db_path = Path(db_path)
        self._key = hashlib.md5((encryption_key or "").encode()).digest()
        # Table name -> the live-record count in its plaintext header, from
        # the last read. DBISAM keeps it exact, so it is the check on what the
        # status bytes (or, failing them, the dates) call live.
        self.header_live: dict[str, int] = {}
        # Set by read_users() when more users read as live than the header
        # holds: users deleted in System Link that the hub still sees.
        self.undetected_deletions = 0
        # How the last read_users() told current users from deleted ones:
        # "status" (each record's own flag) or "dates" (the fallback).
        self.live_by = ""

    # ----- public API --------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        if not _PYDBISAM_OK:
            return False, "pydbisam is not installed"
        users = self._db_path / "Users.dat"
        if not users.exists():
            return False, f"Users.dat not found in {self._db_path}"
        try:
            rows = self.read_users()
        except DmpError as e:
            return False, str(e)
        return True, f"Connected — {len(rows)} active users in {self._db_path.name}"

    def read_users(self) -> list[dict]:
        """Return the live user rows as dicts (column name -> value)."""
        live, how = self.classify(self._read_table("Users", with_status=True), self.header_live.get("Users"))
        self.live_by = how
        return live

    def classify(self, slots: list[tuple[int, dict]], header: int | None) -> tuple[list[dict], str]:
        """The live rows among (status byte, row) slots, and how they were told apart.

        By status byte when the records it calls live number exactly what the
        header says; otherwise by panel-compare dates, saying so.
        """
        flagged = [row for status, row in slots if status == _LIVE]
        self.undetected_deletions = 0
        if header is not None and len(flagged) == header:
            return [row for row in flagged if self._valid(row)], "status"
        statuses: dict[str, int] = {}
        for status, _ in slots:
            statuses[f"0x{status:02x}"] = statuses.get(f"0x{status:02x}", 0) + 1
        logger.warning(
            "DMP: %d record(s) are marked live but the header says %s — telling current users from deleted "
            "ones by panel-compare dates instead, which cannot see a user deleted since the last compare "
            "(record status bytes: %s)", len(flagged), header, statuses,
        )
        live = self._live_rows([row for _, row in slots])
        if header is not None and len(live) > header:
            # A user deleted in System Link keeps its bytes in Users.dat, and
            # the timestamps _live_rows() goes by still look current until a
            # later panel compare. Read as live, their pass is never deleted.
            self.undetected_deletions = len(live) - header
            logger.error(
                "DMP: %d user(s) read as current, but Users.dat's header says %d are — %d deleted "
                "user(s) still read as current, so their AccessGrid passes will not be deleted. "
                "A panel compare in System Link should clear this; dmp-discovery's `slots` mode "
                "shows which records they are",
                len(live), header, self.undetected_deletions,
            )
        return live, "dates"

    # ----- internals ---------------------------------------------------

    def _decrypt(self, path: Path) -> bytes:
        data = path.read_bytes()
        try:
            return data[:_ENC_OFFSET] + b"".join(
                self._run(data[start:end]) for start, end in _cipher_runs(data)
            )
        except Exception as e:  # pragma: no cover - defensive
            raise DmpError(f"decrypt failed for {path.name}: {e}") from e

    def _run(self, chunk: bytes) -> bytes:
        """One CBC run from IV 0; a trailing part block is left out, as ever."""
        chunk = chunk[: len(chunk) - (len(chunk) % 8)]
        return Cipher(Blowfish(self._key), modes.CBC(b"\x00" * 8)).decryptor().update(chunk)

    def _read_table(self, name: str, with_status: bool = False) -> list:
        if not _PYDBISAM_OK:
            raise DmpError("pydbisam is not installed")
        from pydbisam import PyDBISAM

        path = self._db_path / f"{name}.dat"
        if not path.exists():
            raise DmpError(f"{name}.dat not found in {self._db_path}")
        try:
            db = PyDBISAM(data=self._decrypt(path))
            cols = db.fields()
            self.header_live[name] = db.total_rows
        except Exception as e:
            # Wrong password decrypts to garbage → field parse fails here.
            raise DmpError(
                f"could not parse {name}.dat — wrong encryption key? ({e})"
            ) from e

        # Every slot, deleted ones included: classify() decides which are live.
        out: list = []
        total = db.total_rows + db._deleted_rows
        for i in range(total):
            try:
                row = db.row(i, extract_deleted=True)
            except Exception:
                continue
            if row:
                values = dict(zip(cols, row, strict=False))
                out.append((db._data[db._data_offset + i * db._row_size], values) if with_status else values)
        return out

    @staticmethod
    def _valid(row: dict) -> bool:
        num = row.get("USER_NUM")
        return (
            isinstance(num, int)
            and 1 <= num <= 99999
            and bool(row.get("NAME"))
            and bool(row.get("CODE"))
        )

    def _live_rows(self, rows: list[dict]) -> list[dict]:
        valid = [r for r in rows if self._valid(r)]
        if not valid:
            return []

        cmps = [r["LAST_PNL_CMP"] for r in valid if isinstance(r.get("LAST_PNL_CMP"), datetime)]
        max_cmp = max(cmps) if cmps else None

        def is_live(r: dict) -> bool:
            if max_cmp is None:
                return True  # never compared to a panel — can't distinguish
            cmp = r.get("LAST_PNL_CMP")
            if isinstance(cmp, datetime) and max_cmp - cmp <= _COMPARE_WINDOW:
                return True  # part of the latest panel-compare cohort
            chg = r.get("LAST_CHANGE")
            if isinstance(chg, datetime) and chg > max_cmp:
                return True  # edited/added after the last compare
            return False

        live = [r for r in valid if is_live(r)]

        # Resolve reused user-number slots: keep the most recently active row.
        def activity(r: dict) -> datetime:
            ts = [r.get("LAST_PNL_CMP"), r.get("LAST_CHANGE")]
            ts = [t for t in ts if isinstance(t, datetime)]
            return max(ts) if ts else datetime.min

        by_num: dict[int, dict] = {}
        for r in live:
            num = r["USER_NUM"]
            if num not in by_num or activity(r) > activity(by_num[num]):
                by_num[num] = r
        return list(by_num.values())
