"""Phase 3 — Detect deletions in PACS, remove from AccessGrid.

Walk the tracking table (not the live snapshot — that's the safety
guarantee). A row should be deleted if any of these are true:

  - the person is gone from PACS
  - the person exists but the credential is gone
  - the credential exists but `trigger_active` is False (handled in
    phase 2 — we leave that path here as a no-op)

Safety: if the snapshot has zero people, abort the phase. That's almost
certainly a transient PACS error and we don't want to nuke every card.

The phase then runs a second pass in the opposite direction. Where the
first treats the PACS as the truth and removes what it no longer holds,
`_retire_deleted_credentials` treats AccessGrid as the truth about the
credentials AccessGrid itself minted, and releases what it has deleted —
the abandoned half of a card template pair, most often. That inversion is
safe only because it acts on positive evidence: a successful read whose
card says `state: "deleted"`. Absence is never evidence, and an error is
never evidence.
"""

from __future__ import annotations

import logging

from ...ag import AccessGrid, AccessGridError
from ...lib.pacs import PacsAdapter
from .. import tracking
from ..snapshot import Snapshot
from .writeback import deleted_identities_from_cards

logger = logging.getLogger(__name__)

# A destructive operation on the customer's own records, so it is rationed
# the way provisioning is. A cycle that wants to remove more than this has
# almost certainly misread something.
MAX_RETIREMENTS_PER_CYCLE = 25


def run(snapshot: Snapshot, ag: AccessGrid, pacs: PacsAdapter | None = None) -> int:
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

        if tracked.pacs_person_id not in snapshot.credentials_by_person:
            continue  # their page could not be read this cycle — no evidence

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

    if pacs is not None:
        _retire_deleted_credentials(ag, pacs)

    logger.info("Phase 3 done: %d deletion(s)", deleted)
    return deleted


def _retire_deleted_credentials(ag: AccessGrid, pacs: PacsAdapter) -> int:
    """Release PACS slots holding credentials AccessGrid has deleted.

    Only for adapters whose PACS receives credentials. The pass is read one
    at a time by id rather than from a listing: a listing can omit a card
    for reasons that have nothing to do with deletion — a paired pass's
    unified id never appears in one at all — and inferring deletion from
    absence would delete live cards out of the customer's PACS.
    """
    if not getattr(pacs, "supports_credential_retirement", False):
        return 0

    # Only cardholders we have actually written to can have anything to
    # release, which keeps this to one AG read per enrolled cardholder.
    try:
        written = pacs.written_credentials()
    except Exception as e:  # noqa: BLE001
        logger.warning("Phase 3: could not read what the PACS holds: %s", e)
        return 0
    if not written:
        return 0

    retired = 0
    for tracked in tracking.all_tracked():
        if retired >= MAX_RETIREMENTS_PER_CYCLE:
            logger.warning(
                "Phase 3: reached the retirement cap (%d) — the rest wait for "
                "the next cycle", MAX_RETIREMENTS_PER_CYCLE,
            )
            break
        if tracked.status == "deduped" or not tracked.ag_card_id:
            continue
        if (tracked.pacs_person_id, tracked.pacs_credential_id) not in written:
            continue

        try:
            card = ag.access_cards.get(tracked.ag_card_id)
        except AccessGridError as e:
            # No information. A deleted pass answers 200 with a deleted
            # state, so an error here means the request failed, not that
            # anything is gone.
            logger.debug(
                "  Could not read AG card %s: %s — leaving the PACS alone",
                tracked.ag_card_id, e,
            )
            continue

        gone = deleted_identities_from_cards(card)
        if not gone:
            continue

        try:
            count = pacs.retire_credentials(
                tracked.pacs_person_id, tracked.pacs_credential_id, gone,
            )
        except Exception as e:  # noqa: BLE001 — one cardholder must not stop the cycle
            logger.error(
                "  Failed to retire credentials for %s/%s: %s",
                tracked.pacs_person_id, tracked.pacs_credential_id, e,
            )
            continue
        retired += count

    if retired:
        logger.info("Phase 3: released %d PACS credential(s) deleted in AccessGrid", retired)
    return retired
