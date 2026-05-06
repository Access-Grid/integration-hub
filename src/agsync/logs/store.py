"""SQLite-backed log buffer with FIFO eviction.

Capped at MAX_ROWS — when the table reaches that size, every new insert
also deletes the oldest row(s) to keep the size stable.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..db.connection import get_db

MAX_ROWS = 100_000
EVICT_BATCH = 1_000  # delete this many rows once we exceed MAX_ROWS

_lock = threading.Lock()
_row_count_cache: int | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _row_count(conn: sqlite3.Connection) -> int:
    global _row_count_cache
    if _row_count_cache is None:
        _row_count_cache = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    return _row_count_cache


def log(level: str, message: str, phase: str | None = None) -> None:
    """Persist a log entry. Safe to call from any thread."""
    level = level.upper()
    with _lock:
        conn = get_db()
        global _row_count_cache
        conn.execute(
            "INSERT INTO logs (ts, level, phase, message) VALUES (?, ?, ?, ?)",
            (_now_iso(), level, phase, message),
        )
        if _row_count_cache is None:
            _row_count_cache = _row_count(conn)
        else:
            _row_count_cache += 1

        if _row_count_cache > MAX_ROWS:
            conn.execute(
                "DELETE FROM logs WHERE id IN ("
                "  SELECT id FROM logs ORDER BY id ASC LIMIT ?"
                ")",
                (EVICT_BATCH,),
            )
            _row_count_cache -= EVICT_BATCH


def query_logs(
    level: str | None = None,
    phase: str | None = None,
    search: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    sql = "SELECT id, ts, level, phase, message FROM logs WHERE 1=1"
    params: list[Any] = []
    if level and level.upper() != "ALL":
        sql += " AND level = ?"
        params.append(level.upper())
    if phase:
        sql += " AND phase = ?"
        params.append(phase)
    if search:
        sql += " AND message LIKE ?"
        params.append(f"%{search}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in get_db().execute(sql, tuple(params))]


class _SqliteLogHandler(logging.Handler):
    """Bridges Python's logging module into our SQLite buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            phase = getattr(record, "phase", None)
            log(record.levelname, self.format(record), phase=phase)
        except Exception:  # noqa: BLE001
            self.handleError(record)


def install_handler(level: str = "INFO") -> None:
    """Attach the SQLite handler to the root logger.

    Idempotent: re-attaching is safe (we replace any existing handler of
    our type). Logging from anywhere in the app will then land in the
    SQLite logs table.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if isinstance(h, _SqliteLogHandler):
            root.removeHandler(h)
    handler = _SqliteLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)


def levels() -> Iterable[str]:
    return ("DEBUG", "INFO", "WARNING", "ERROR")
