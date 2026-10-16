"""A switch that stops this install changing anything, anywhere.

Added after a renamed field on the PACS side turned into mass suspension
and a burst of re-provisioning: every cardholder's pass was switched off
and a new one issued, because one silently-empty value made every card
look like it had been revoked. The code was confident and wrong, and by
the time anybody read the log the damage was done.

So the default is to do nothing and say what it would have done. An
install that is meant to act has to be told to, rather than an install
that is meant to be safe having to be stopped in time.

Two properties matter:

* It gates the call that changes something *and* the bookkeeping that
  follows it. Recording a pass we did not issue is its own kind of damage,
  and a dry run that writes the tracking table is not a dry run.
* It covers both directions. Gating only the PACS adapter would have left
  phase 1 still minting AccessGrid passes and phase 2 still suspending
  them, which is where the licences and the locked-out residents actually
  came from.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

PREFIX = "[DRYRUN]"

# Where the operator's choice lives. Deliberately the settings page rather
# than the environment: the hub runs as a Windows service, so an
# environment variable means System Properties and a service restart, by
# somebody who already has the hub's own web UI open in front of them.
SETTING_KEY = "sync_dry_run"

# A mutation check happens per credential, so the stored value is cached
# briefly rather than read from SQLite inside the phase loops. Saving the
# setting clears this, so the page never lies about what is in force; the
# window only matters if the row is changed behind the application's back.
_CACHE_TTL_S = 5.0
_cached: tuple[float, bool] | None = None


def active() -> bool:
    """Is this install in dry-run mode?

    Anything other than a stored "false" means yes, including an absent
    row and an unreadable one. The default has to be the safe answer:
    this is the switch that decides whether a misreading becomes a
    revoked pass.
    """
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _CACHE_TTL_S:
        return _cached[1]

    try:
        from .settings_store import get

        raw = get(SETTING_KEY)
    except Exception as e:  # noqa: BLE001 — unreadable settings must mean "safe"
        logger.warning(
            "%s could not read the dry-run setting (%s) — assuming dry run",
            PREFIX, e,
        )
        return True  # not cached: a transient failure should not stick

    value = True if raw is None else raw.strip().lower() != "false"
    _cached = (now, value)
    return value


def set_active(value: bool) -> None:
    """Record the operator's choice and make it take effect at once."""
    from .settings_store import set as store

    store(SETTING_KEY, "true" if value else "false")
    invalidate()


def invalidate() -> None:
    global _cached
    _cached = None


def would(message: str, *args: object) -> None:
    """Log the action that is not being taken."""
    logger.info(f"{PREFIX} would {message}", *args)
