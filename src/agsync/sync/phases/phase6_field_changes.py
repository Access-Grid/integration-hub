"""Phase 6 — Sync person field changes (name/email/phone/title) to AG.

Compare current PACS values to the last_synced_* values stored in the
tracking row. If anything changed, push an AG update and store the new
values.
"""

from __future__ import annotations

import logging

from ...ag import AccessGrid, AccessGridError
from .. import tracking
from ..snapshot import Snapshot

logger = logging.getLogger(__name__)


def run(snapshot: Snapshot, ag: AccessGrid) -> int:
    updated = 0
    logger.info("Phase 6: Checking for field changes")

    for tracked in tracking.all_tracked():
        if tracked.status == "deleted" or not tracked.ag_card_id:
            continue
        person = snapshot.people.get(tracked.pacs_person_id)
        if person is None:
            continue

        changes: dict[str, str] = {}
        if person.full_name and person.full_name != tracked.last_synced_full_name:
            changes["full_name"] = person.full_name
        if person.email != tracked.last_synced_email:
            if person.email:
                changes["email"] = person.email
        if person.phone != tracked.last_synced_phone:
            if person.phone:
                changes["phone_number"] = person.phone
        if person.title != tracked.last_synced_title:
            changes["title"] = person.title

        if not changes:
            continue

        try:
            logger.info(
                "  Updating AG card %s — %s",
                tracked.ag_card_id, ", ".join(changes.keys()),
            )
            ag.access_cards.update(card_id=tracked.ag_card_id, **changes)
            tracking.update_field_tracking(
                tracked.pacs_person_id, tracked.pacs_credential_id,
                full_name=person.full_name,
                email=person.email,
                phone=person.phone,
                title=person.title,
            )
            updated += 1
        except AccessGridError as e:
            logger.error("  Field update failed for %s: %s", tracked.ag_card_id, e)

    logger.info("Phase 6 done: %d field update(s)", updated)
    return updated
