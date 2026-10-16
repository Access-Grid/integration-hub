"""What the PACS was sent, and what it answered, for a save it refused.

The log can say a cardholder's save was rejected, and since 0.2.18 it can
usually say why — but "why" is the PACS's own wording, and sometimes there
isn't any. Cardholder 12247 is refused on every cycle and the page it
sends back carries no validation message at all, which leaves nothing to
look at and nothing to act on.

So the exchange itself is kept. Few rows, evicted oldest first: a request
is the whole cardholder form and a response is the whole page back, so
this is measured in tens of kilobytes each rather than the hundreds of
rows the log buffer holds.

It contains cardholder data by construction — the form carries name,
address, phone and email, and the response carries the rendered page. That
is the point of it, and it stays on the operator's own machine. The
anti-forgery token is the one thing scrubbed, being a live credential
rather than a record of anything.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from typing import Any

from .db import get_db

logger = logging.getLogger(__name__)

# Deliberately small. Twenty refusals is more than anyone will read, and
# the next one matters more than the twenty-first did.
MAX_ROWS = 20

# A page can be megabytes if something has gone very wrong. Enough to see
# what came back without letting one answer fill the database.
MAX_BODY = 256_000

_TOKEN = re.compile(
    r'(__RequestVerificationToken[^\n]{0,40}?value=")[^"]+(")', re.I
)
_TOKEN_FIELD = re.compile(
    r'(name="__RequestVerificationToken"\r?\n\r?\n)[^\r\n]+', re.I
)

_lock = threading.Lock()


def _scrub(text: str) -> str:
    """Take the anti-forgery token out, wherever it appears.

    It shows up twice: as a hidden input in the response page and as a
    field in the request body. Neither is a record of anything, and both
    are usable until the session ends.
    """
    text = _TOKEN.sub(r"\1[redacted]\2", text)
    return _TOKEN_FIELD.sub(r"\1[redacted]", text)


def _clip(text: str) -> str:
    if len(text) <= MAX_BODY:
        return text
    return text[:MAX_BODY] + f"\n\n… truncated, {len(text)} characters in all"


def record(
    *,
    vendor: str,
    cardholder_id: str,
    status_code: int | None,
    reasons: list[str] | None,
    request: str,
    response: str,
) -> None:
    """Keep one refused exchange. Never raises: this is a diagnostic."""
    try:
        with _lock:
            conn = get_db()
            conn.execute(
                "INSERT INTO failed_saves "
                "(ts, vendor, cardholder_id, status_code, reasons, request, response) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    vendor,
                    str(cardholder_id),
                    status_code,
                    "\n".join(reasons or []),
                    _clip(_scrub(request)),
                    _clip(_scrub(response)),
                ),
            )
            conn.execute(
                "DELETE FROM failed_saves WHERE id NOT IN ("
                "  SELECT id FROM failed_saves ORDER BY id DESC LIMIT ?"
                ")",
                (MAX_ROWS,),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001 — a diagnostic must not break a sync
        logger.debug("Could not record the refused save: %s", e)


def recent(limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """The refusals, newest first."""
    try:
        rows = get_db().execute(
            "SELECT id, ts, vendor, cardholder_id, status_code, reasons, "
            "       request, response "
            "  FROM failed_saves ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not read the refused saves: %s", e)
        return []
    return [dict(row) for row in rows]


def clear() -> int:
    """Forget them all. Returns how many went."""
    with _lock:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM failed_saves").fetchone()[0]
        conn.execute("DELETE FROM failed_saves")
        conn.commit()
    return int(count)
