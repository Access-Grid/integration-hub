"""Phase 2 — PACS → AccessGrid status changes.

For every tracked credential, compare the PACS-side credential status
to the last known AG state. If the PACS changed (suspended → active or
similar) and the AG state is stale, push the change to AG.

Direction-of-change check: we only act if the PACS-side status differs
from the credential's current status AND the AG state matches what we
last saw. If AG state changed independently, that's phase 4's job.
"""

from __future__ import annotations

import logging

from ... import dryrun
from ...ag import AccessGrid, AccessGridError
from ...lib.pacs import CredentialStatus
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)

# Temporary. Phase 2 suspends by the id on the tracking row, which for a
# card template pair is the unified pass id rather than either half's card
# id — and phase 4 judges the same credential by re-reading the halves. If
# a suspend of the pass does not leave the halves reading "suspended",
# phase 4's install gate sees an installed, unsuspended pass and writes the
# operator's unticked box straight back on. These lines say which id was
# used, what it resolved to, and what the halves looked like afterwards.
SUSPEND_DEBUG = "[SUSPEND-DEBUG]"


def _describe_card(card) -> str:
    state = (getattr(card, "state", "") or "?").lower()
    devices = [
        (getattr(d, "device_type", None) or getattr(d, "type", None) or "?",
         (getattr(d, "status", None) or getattr(d, "state", None) or "?"))
        for d in (getattr(card, "devices", None) or [])
    ]
    return f"{getattr(card, 'id', '?')} state={state} devices={devices or '[]'}"


def _suspend_debug_before(snapshot, tracked, ag_state: str, cred) -> None:
    """What we are about to call, and what that id actually points at."""
    try:
        halves = snapshot.resolve_ag_cards(
            tracked.ag_card_id, tracked.pacs_person_id,
            tracked.pacs_credential_id, tracked.sync_ref,
        )
        ids = [getattr(c, "id", "?") for c in halves]
        logger.info(
            "%s %s/%s: suspending by tracked id %s — resolves to %d card(s): %s "
            "| listing state=%r last_known=%r pacs=%r | tracked id is %s",
            SUSPEND_DEBUG, tracked.pacs_person_id, tracked.pacs_credential_id,
            tracked.ag_card_id, len(halves),
            "; ".join(_describe_card(c) for c in halves) or "none",
            ag_state, tracked.last_known_ag_state, cred.status.value,
            "one of them" if tracked.ag_card_id in ids else "NOT one of them",
        )
    except Exception as e:  # noqa: BLE001 — diagnostics must never break a cycle
        logger.info("%s could not describe the call: %s", SUSPEND_DEBUG, e)


def _suspend_debug_after(snapshot, tracked, ag) -> None:
    """Did suspending that id actually leave the halves suspended?

    This is the question the whole hypothesis turns on, so the cards are
    re-read rather than taken from the per-cycle cache.
    """
    try:
        halves = snapshot.resolve_ag_cards(
            tracked.ag_card_id, tracked.pacs_person_id,
            tracked.pacs_credential_id, tracked.sync_ref,
        )
        fresh = []
        for card in halves:
            card_id = getattr(card, "id", None)
            if not card_id:
                continue
            try:
                fresh.append(_describe_card(ag.access_cards.get(card_id)))
            except Exception as e:  # noqa: BLE001
                fresh.append(f"{card_id} re-read failed: {e}")
        try:
            pass_state = _describe_card(ag.access_cards.get(tracked.ag_card_id))
        except Exception as e:  # noqa: BLE001
            pass_state = f"{tracked.ag_card_id} re-read failed: {e}"
        logger.info(
            "%s %s/%s: suspend returned without error. Pass now %s | halves now %s",
            SUSPEND_DEBUG, tracked.pacs_person_id, tracked.pacs_credential_id,
            pass_state, "; ".join(fresh) or "none",
        )
    except Exception as e:  # noqa: BLE001
        logger.info("%s could not re-read after the suspend: %s", SUSPEND_DEBUG, e)


def run(snapshot: Snapshot, ag: AccessGrid) -> int:
    updated = 0
    logger.info("Phase 2: Checking PACS → AG status changes")

    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "pending", "deduped"):
            continue
        if not tracked.ag_card_id:
            continue

        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is None:
            # Credential gone from PACS → phase 3 territory.
            continue

        # Trigger removed → terminate the AG card.
        if not cred.trigger_active:
            try:
                logger.info(
                    "  Terminating AG card %s — trigger no longer set on %s/%s",
                    tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id,
                )
                if dryrun.active():
                    dryrun.would(
                        "terminate AG card %s for %s/%s",
                        tracked.ag_card_id, tracked.pacs_person_id,
                        tracked.pacs_credential_id,
                    )
                    continue
                ag.access_cards.delete(card_id=tracked.ag_card_id)
                tracking.mark_deleted(
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                )
                updated += 1
            except AccessGridError as e:
                logger.error("  Failed to delete AG card %s: %s", tracked.ag_card_id, e)
            continue

        # Compare PACS status to AG-tracked status.
        ag_card = snapshot.resolve_ag_card(
            tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id,
            tracked.sync_ref,
        )
        ag_state = (getattr(ag_card, "state", "") or "").lower() if ag_card else ""

        # If AG state diverged from what we last knew, leave it for phase 4.
        if ag_state and ag_state != tracked.last_known_ag_state and tracked.last_known_ag_state:
            logger.debug(
                "  Skip %s/%s — AG changed (%s → %s); phase 4 will reconcile",
                tracked.pacs_person_id, tracked.pacs_credential_id,
                tracked.last_known_ag_state, ag_state,
            )
            continue

        desired = "active" if cred.status == CredentialStatus.ACTIVE else "suspended"
        if ag_state == desired:
            continue

        # A pass nobody has installed has an inactive card in the PACS
        # because phase 4 put it that way, not because anyone revoked it.
        # Pushing that back would suspend the pass the holder is about to
        # install, and a suspended pass cannot be installed — so the two
        # phases would deadlock on each other.
        #
        # Only for credentials we minted. Where the card is the customer's
        # own, an operator switching it off before anyone installed is a
        # real revocation and has to reach AccessGrid.
        if (
            desired == "suspended"
            and ag_state == "created"
            and getattr(cred, "allocate_identity", False)
        ):
            logger.debug(
                "  Skip %s/%s — the pass is not installed yet, so its card is "
                "inactive by design",
                tracked.pacs_person_id, tracked.pacs_credential_id,
            )
            continue

        try:
            if desired == "suspended" and ag_state in ("active", "created", ""):
                logger.info(
                    "  Suspending AG card %s (PACS credential %s/%s is %s)",
                    tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id,
                    cred.status.value,
                )
                if dryrun.active():
                    dryrun.would(
                        "suspend AG card %s for %s/%s",
                        tracked.ag_card_id, tracked.pacs_person_id,
                        tracked.pacs_credential_id,
                    )
                    continue
                _suspend_debug_before(snapshot, tracked, ag_state, cred)
                ag.access_cards.suspend(card_id=tracked.ag_card_id)
                _suspend_debug_after(snapshot, tracked, ag)
                tracking.update_status(
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                    "suspended", last_known_ag_state="suspended",
                )
                updated += 1
            elif desired == "active" and ag_state == "suspended":
                logger.info(
                    "  Resuming AG card %s (PACS credential %s/%s is %s)",
                    tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id,
                    cred.status.value,
                )
                if dryrun.active():
                    dryrun.would(
                        "resume AG card %s for %s/%s",
                        tracked.ag_card_id, tracked.pacs_person_id,
                        tracked.pacs_credential_id,
                    )
                    continue
                ag.access_cards.resume(card_id=tracked.ag_card_id)
                tracking.update_status(
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                    "active", last_known_ag_state="active",
                )
                updated += 1
        except AccessGridError as e:
            logger.error("  Failed to update AG card %s: %s", tracked.ag_card_id, e)
            logger.info(
                "%s %s/%s: the call raised %s: %s",
                SUSPEND_DEBUG, tracked.pacs_person_id, tracked.pacs_credential_id,
                type(e).__name__, e,
            )

    logger.info("Phase 2 done: %d status change(s)", updated)
    return updated
