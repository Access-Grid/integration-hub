"""SQLite connection management.

Uses one connection per thread (sqlite3 connections aren't safe to share
across threads). The web server and the sync worker run in different
threads, so each gets its own connection via thread-local storage.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from ..config import get_settings
from .schema import apply_migrations

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    path = get_settings().db_path
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """Apply migrations. Safe to call multiple times."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = _connect()
        try:
            apply_migrations(conn)
        finally:
            conn.close()
        _initialized = True


def get_db() -> sqlite3.Connection:
    """Return the per-thread connection, creating it on first use."""
    if not _initialized:
        init_db()
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def execute(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(get_db().execute(sql, params))


def execute_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    rows = list(get_db().execute(sql, params))
    return rows[0] if rows else None
