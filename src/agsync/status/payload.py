"""Assemble the status report body.

Pure: everything it needs is passed in. It performs no I/O at all, which is
what lets the reporting thread build a report without touching the database
or taking a lock, and what lets the whole shape be tested without a server.

The contract is documented at `SCHEMA`. Fields are never dropped when a
value is unknown — they are sent as null, because an absent key and a zero
count read identically to a consumer and one of them is a real reading.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

# Bumped only for a breaking change. Added fields do not bump it, so
# AccessGrid tolerates unknown keys.
SCHEMA = 1

# `pause_reason` and a cycle's `error` are free text built from our own
# exception strings, so they can carry a PACS URL or a local path. Capped
# and scrubbed before they leave the building.
_MAX_FREE_TEXT = 200
_URLISH = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\\S+")
_POSIX_PATH = re.compile(r"(?<![\w.])/(?:[\w.-]+/){2,}[\w.-]*")

# Which way credentials flow. Named for the direction rather than the
# protocol because the two diverge: a DESFire integration that writes into
# the PACS is "ag_to_pacs" just as a Seos one is, and an adapter answers
# this for itself through `supports_credential_writeback`.
AG_TO_PACS = "ag_to_pacs"
PACS_TO_AG = "pacs_to_ag"

CONNECTED = "connected"
SESSION_EXPIRED = "session_expired"
UNREACHABLE = "unreachable"
NOT_CONFIGURED = "not_configured"

RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"


def _z(iso: str | None) -> str | None:
    """Normalise a timestamp to ISO8601 UTC with a trailing Z.

    The engine stores `+00:00`; AccessGrid asked for `Z`. Anything
    unparseable is passed through rather than guessed at.
    """
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def scrub(text: str | None) -> str:
    """Strip anything that could carry infrastructure detail, then cap."""
    if not text:
        return ""
    cleaned = _URLISH.sub("[url]", str(text))
    cleaned = _WINDOWS_PATH.sub("[path]", cleaned)
    cleaned = _POSIX_PATH.sub("[path]", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_FREE_TEXT:
        cleaned = cleaned[: _MAX_FREE_TEXT - 1].rstrip() + "…"
    return cleaned


def _direction(writes_credentials: bool | None) -> str | None:
    """null until an adapter has been built and asked."""
    if writes_credentials is None:
        return None
    return AG_TO_PACS if writes_credentials else PACS_TO_AG


def _connection_status(status: dict[str, Any], configured: bool) -> str:
    if not configured:
        return NOT_CONFIGURED
    # Checked before reachability: an expired session also reads as
    # unreachable, but only one of the two tells the operator to go and sign
    # in, and that is the one worth saying.
    if status.get("reconnect_required"):
        return SESSION_EXPIRED
    if not status.get("pacs_reachable", True):
        return UNREACHABLE
    return CONNECTED


def _sync_state(status: dict[str, Any]) -> str:
    if not status.get("running"):
        return STOPPED
    return PAUSED if status.get("paused") else RUNNING


def _last_cycle(status: dict[str, Any]) -> dict[str, Any] | None:
    cycle = status.get("last_cycle")
    if not cycle:
        return None
    error = cycle.get("error")
    return {
        "started_at": _z(cycle.get("started_at")),
        "duration_ms": cycle.get("duration_ms"),
        "ok": not error,
        "error": scrub(error) or None,
        "provisioned": cycle.get("provisioned", 0),
        "status_changes": cycle.get("status_changes", 0),
        "deleted": cycle.get("deleted", 0),
        # Reported under a different name than it is held under: the
        # report also carries "ag_to_pacs" as a *direction*, and one
        # payload saying it in two senses reads badly cold. The engine's
        # own field keeps its name.
        "credentials_written": cycle.get("ag_to_pacs", 0),
        "field_updates": cycle.get("field_updates", 0),
        "retried": cycle.get("retried", 0),
    }


def _conditions(
    status: dict[str, Any], connection: str, state: str, totals: dict[str, int]
) -> list[str]:
    """A rollup of the fields above, so alerting need not re-encode them.

    Derived, never a source of new information. An empty list means healthy.
    """
    found: list[str] = []
    if connection == SESSION_EXPIRED:
        found.append("pacs_session_expired")
    if connection == UNREACHABLE:
        found.append("pacs_unreachable")
    if not status.get("ag_reachable", True):
        found.append("ag_unreachable")
    if state == PAUSED:
        found.append("engine_paused")
    if not status.get("last_cycle"):
        found.append("never_synced")
    if totals.get("failed", 0) > 0:
        found.append("provision_failures")
    return found


def build(
    status: dict[str, Any],
    *,
    instance: dict[str, str],
    uptime_s: int,
    vendor: str = "",
    display_name: str = "",
    writes_credentials: bool | None = None,
    totals: dict[str, int] | None = None,
) -> dict[str, Any]:
    """The complete report body, ready to sign and send."""
    totals = dict(totals or {})
    configured = bool(vendor)
    connection = _connection_status(status, configured)
    state = _sync_state(status)

    pacs: list[dict[str, Any]] = []
    if configured:
        # A list because one hub will eventually hold more than one
        # connection. It is never longer than one today.
        pacs.append({
            "vendor": vendor,
            "display_name": display_name or vendor,
            "direction": _direction(writes_credentials),
            "connection_status": connection,
            "last_successful_read_at": _z(status.get("last_pacs_read_iso")),
        })

    return {
        "schema": SCHEMA,
        "sent_at": now_z(),
        "instance": {**instance, "uptime_s": uptime_s},
        "pacs": pacs,
        "sync": {
            "state": state,
            "pause_reason": scrub(status.get("pause_reason")),
            "interval_s": int(status.get("cached_interval_s") or 0),
            "next_run_at": _z(status.get("next_run_iso")),
            "consecutive_errors": status.get("consecutive_errors", 0),
            "last_cycle": _last_cycle(status),
        },
        "totals": totals,
        "conditions": _conditions(status, connection, state, totals),
    }
