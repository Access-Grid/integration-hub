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

logger = logging.getLogger(__name__)

PREFIX = "[DRYRUN]"


def active() -> bool:
    """Is this install in dry-run mode?

    Read per call rather than captured, so flipping the setting takes
    effect on the next cycle instead of the next restart.
    """
    from .config import get_settings

    try:
        return bool(get_settings().dry_run)
    except Exception:  # noqa: BLE001 — unreadable settings must mean "safe"
        logger.warning(
            "%s could not read the dry-run setting — assuming dry run", PREFIX,
        )
        return True


def would(message: str, *args: object) -> None:
    """Log the action that is not being taken."""
    logger.info(f"{PREFIX} would {message}", *args)
