"""Phase 3 — Detect deletions in PACS, remove from AccessGrid.

Walk the tracking table (not the live snapshot — that's the safety
guarantee). A row should be deleted if any of these are true:

  - the person is gone from PACS
  - the person exists but the credential is gone
  - the credential exists but `trigger_active` is False (handled in
    phase 2 — we leave that path here as a no-op)

Safety: if the snapshot has zero people, abort the phase. That's almost
certainly a transient PACS error and we don't want to nuke every card.
"""

from __future__ import annotations

import logging

from ...ag import AccessGrid, AccessGridError
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)


def run(snapshot: Snapshot, ag: AccessGrid) -> int:
    logger.info("Phase 3: Checking for deletions")

    if not snapshot.people:
        logger.warning("Phase 3: PACS returned 0 people — skipping deletions for safety")
        return 0

    deleted = 0
    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped") or not tracked.ag_card_id:
            continue

        person = snapshot.people.get(tracked.pacs_person_id)
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)

        reason: str | None = None
        if person is None:
            reason = f"person {tracked.pacs_person_id} no longer in PACS"
        elif cred is None:
            reason = f"credential {tracked.pacs_credential_id} no longer on person"
        # cred.trigger_active=False is handled in phase 2 (terminate).

        if reason is None:
            continue

        try:
            logger.info("  Deleting AG card %s — %s", tracked.ag_card_id, reason)
            ag.access_cards.delete(card_id=tracked.ag_card_id)
            tracking.update_status(
                tracked.pacs_person_id, tracked.pacs_credential_id,
                "deleted", last_known_ag_state="deleted",
            )
            deleted += 1
        except AccessGridError as e:
            msg = str(e).lower()
            if "not found" in msg or "404" in msg:
                # Already gone — clean up.
                logger.debug("  AG card %s already gone — removing tracking row", tracked.ag_card_id)
                tracking.remove(tracked.pacs_person_id, tracked.pacs_credential_id)
            else:
                logger.error("  Failed to delete AG card %s: %s", tracked.ag_card_id, e)

    logger.info("Phase 3 done: %d deletion(s)", deleted)
    return deleted
