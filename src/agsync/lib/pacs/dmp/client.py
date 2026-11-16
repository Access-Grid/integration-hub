"""Read the DMP System Link DBISAM database (decrypt + parse live users).

DMP System Link stores its data in a DBISAM v4 database with per-table
Blowfish encryption. Only the region from file offset 0x200 onward is
encrypted; the basic header (row count, row size, field count) stays plaintext.

Encryption scheme (validated against a live customer database):
    key  = MD5(table password)          # full 16 bytes
    algo = Blowfish, big-endian, CBC, IV = 0, from offset 0x200

The table password is a value the operator supplies via the setup wizard; it is
never stored in code. We read the tables directly off disk each cycle — System
Link writes them as it programs panels — so no export step is needed.

Live vs. deleted rows
---------------------
DBISAM v4 tracks deletions in the index free-list, not with a per-record flag in
the .dat, so a raw read surfaces deleted "ghost" records alongside live ones
(e.g. the previous occupant of a reused user-number slot). We filter to live
records using the panel-compare bookkeeping every user row carries: a live row
sits in the most-recent ``LAST_PNL_CMP`` cohort (System Link stamps every live
user on each panel compare), or was edited after it (``LAST_CHANGE`` newer than
the last compare). Reused-number slots are then de-duplicated to the most
recently active record. This reproduced the header's live count exactly on the
reference database. If the database has never been compared to a panel
(``LAST_PNL_CMP`` all null) we can't distinguish, and fall back to every
parseable row.
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


class DmpError(Exception):
    """Raised when the DMP database can't be read or decrypted."""


class DmpClient:
    """Reads and decrypts DMP System Link DBISAM tables from a folder on disk."""

    def __init__(self, db_path: str, encryption_key: str) -> None:
        self._db_path = Path(db_path)
        self._key = hashlib.md5((encryption_key or "").encode()).digest()

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
        rows = self._read_table("Users")
        return self._live_rows(rows)

    # ----- internals ---------------------------------------------------

    def _decrypt(self, path: Path) -> bytes:
        data = path.read_bytes()
        body = data[_ENC_OFFSET:]
        body = body[: len(body) - (len(body) % 8)]
        try:
            plain = (
                Cipher(Blowfish(self._key), modes.CBC(b"\x00" * 8))
                .decryptor()
                .update(body)
            )
        except Exception as e:  # pragma: no cover - defensive
            raise DmpError(f"decrypt failed for {path.name}: {e}") from e
        return data[:_ENC_OFFSET] + plain

    def _read_table(self, name: str) -> list[dict]:
        if not _PYDBISAM_OK:
            raise DmpError("pydbisam is not installed")
        from pydbisam import PyDBISAM

        path = self._db_path / f"{name}.dat"
        if not path.exists():
            raise DmpError(f"{name}.dat not found in {self._db_path}")
        try:
            db = PyDBISAM(data=self._decrypt(path))
            cols = db.fields()
        except Exception as e:
            # Wrong password decrypts to garbage → field parse fails here.
            raise DmpError(
                f"could not parse {name}.dat — wrong encryption key? ({e})"
            ) from e

        # DBISAM's per-row delete flag is unreliable on these v4 files, so read
        # every slot and let _live_rows() sort out live vs deleted.
        out: list[dict] = []
        total = db.total_rows + db._deleted_rows
        for i in range(total):
            try:
                row = db.row(i, extract_deleted=True)
            except Exception:
                continue
            if row:
                out.append(dict(zip(cols, row, strict=False)))
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
