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

from ...ag import AccessGrid, AccessGridError
from ...lib.pacs import CredentialStatus
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)


def run(snapshot: Snapshot, ag: AccessGrid) -> int:
    updated = 0
    logger.info("Phase 2: Checking PACS → AG status changes")

    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "pending"):
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
                ag.access_cards.delete(card_id=tracked.ag_card_id)
                tracking.update_status(
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                    "deleted", last_known_ag_state="deleted",
                )
                updated += 1
            except AccessGridError as e:
                logger.error("  Failed to delete AG card %s: %s", tracked.ag_card_id, e)
            continue

        # Compare PACS status to AG-tracked status.
        ag_card = snapshot.ag_card_by_id.get(tracked.ag_card_id)
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

        try:
            if desired == "suspended" and ag_state in ("active", "created", ""):
                logger.info(
                    "  Suspending AG card %s (PACS credential %s/%s is %s)",
                    tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id,
                    cred.status.value,
                )
                ag.access_cards.suspend(card_id=tracked.ag_card_id)
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
                ag.access_cards.resume(card_id=tracked.ag_card_id)
                tracking.update_status(
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                    "active", last_known_ag_state="active",
                )
                updated += 1
        except AccessGridError as e:
            logger.error("  Failed to update AG card %s: %s", tracked.ag_card_id, e)

    logger.info("Phase 2 done: %d status change(s)", updated)
    return updated
