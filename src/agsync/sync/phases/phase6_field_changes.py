"""Phase 6 — Sync person field changes (name/phone/title) to AG.

Compare current PACS values to the last_synced_* values stored in the
tracking row. If anything changed, push an AG update and store the new
values.

Email is deliberately not among them. AccessGrid rejects it on an issued
pass — "Unexpected parameters provided" — since the address is what the pass
was delivered to, not an attribute of it. A changed address is still worth
knowing about, because the pass keeps the old one, so it is reported once
rather than retried forever.
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
        if tracked.status in ("deleted", "deduped") or not tracked.ag_card_id:
            continue
        person = snapshot.people.get(tracked.pacs_person_id)
        if person is None:
            continue

        changes: dict[str, str] = {}
        if person.full_name and person.full_name != tracked.last_synced_full_name:
            changes["full_name"] = person.full_name
        if person.phone != tracked.last_synced_phone:
            if person.phone:
                changes["phone_number"] = person.phone
        if person.title != tracked.last_synced_title:
            changes["title"] = person.title

        email_changed = bool(person.email) and person.email != tracked.last_synced_email
        if not changes and not email_changed:
            continue

        if email_changed:
            logger.warning(
                "  %s (%s): address is now %s but the pass was issued to %s — "
                "AccessGrid does not accept an email change on an issued pass, "
                "so it keeps the old one; re-issue the pass if that matters",
                person.full_name, tracked.pacs_person_id,
                person.email, tracked.last_synced_email or "(none)",
            )

        if changes:
            try:
                logger.info(
                    "  Updating AG card %s — %s",
                    tracked.ag_card_id, ", ".join(changes.keys()),
                )
                ag.access_cards.update(card_id=tracked.ag_card_id, **changes)
                updated += 1
            except AccessGridError as e:
                logger.error("  Field update failed for %s: %s", tracked.ag_card_id, e)
                # Leave the tracking row alone so the change is retried, and
                # so a failed name push does not get recorded as done.
                continue

        # Recorded even when the only change was an address we cannot push:
        # noticing it once is useful, noticing it every cycle forever is not.
        tracking.update_field_tracking(
            tracked.pacs_person_id, tracked.pacs_credential_id,
            full_name=person.full_name,
            email=person.email,
            phone=person.phone,
            title=person.title,
        )

    logger.info("Phase 6 done: %d field update(s)", updated)
    return updated
