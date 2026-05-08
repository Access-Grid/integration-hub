"""Phase 4 — AccessGrid → PACS status sync.

Direction-of-change check: an AG card whose state changed since
last_known_ag_state means an operator (or a card-holder unlinking from
their phone) acted on the AG side. Push the change back to PACS.

Phase 2 has already pushed PACS-side changes outward, so any remaining
divergence is AG-initiated.
"""

from __future__ import annotations

import logging

from ...lib.pacs import CredentialStatus, PacsAdapter
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)

_AG_TO_CRED_STATUS: dict[str, CredentialStatus] = {
    "active": CredentialStatus.ACTIVE,
    "created": CredentialStatus.ACTIVE,
    "suspended": CredentialStatus.SUSPENDED,
}


def run(snapshot: Snapshot, pacs: PacsAdapter) -> int:
    if not pacs.supports_status_writeback:
        logger.debug("Phase 4: PACS does not support status writeback — skipping")
        return 0

    updated = 0
    logger.info("Phase 4: Checking AG → PACS status changes")

    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped") or not tracked.ag_card_id:
            continue

        ag_card = snapshot.ag_card_by_id.get(tracked.ag_card_id)
        if ag_card is None:
            continue

        ag_state = (getattr(ag_card, "state", "") or "").lower()
        if not ag_state or ag_state == tracked.last_known_ag_state:
            continue

        # AG-side change detected. Translate to a credential status.
        desired = _AG_TO_CRED_STATUS.get(ag_state)
        if desired is None:
            tracking.update_last_known_ag_state(
                tracked.pacs_person_id, tracked.pacs_credential_id, ag_state,
            )
            continue

        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is None or cred.status == desired:
            tracking.update_last_known_ag_state(
                tracked.pacs_person_id, tracked.pacs_credential_id, ag_state,
            )
            continue

        try:
            logger.info(
                "  Pushing AG state '%s' to PACS for %s/%s",
                ag_state, tracked.pacs_person_id, tracked.pacs_credential_id,
            )
            ok = pacs.update_credential_status(
                tracked.pacs_person_id, tracked.pacs_credential_id, desired,
            )
            if ok:
                updated += 1
                tracking.update_last_known_ag_state(
                    tracked.pacs_person_id, tracked.pacs_credential_id, ag_state,
                )
            else:
                logger.warning(
                    "  PACS rejected status update for %s/%s",
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "  Failed to update PACS for %s/%s: %s",
                tracked.pacs_person_id, tracked.pacs_credential_id, e,
            )

    logger.info("Phase 4 done: %d PACS update(s)", updated)
    return updated
