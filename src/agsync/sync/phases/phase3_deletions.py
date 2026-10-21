"""Phase 3 — Detect deletions in PACS, remove from AccessGrid.

Walk the tracking table (not the live snapshot — that's the safety
guarantee). A row should be deleted if any of these are true:

  - the person is gone from PACS
  - the person exists but the credential is gone
  - the credential exists but `trigger_active` is False (handled in
    phase 2 — we leave that path here as a no-op)

Safety: if the snapshot has zero people, abort the phase. That's almost
certainly a transient PACS error and we don't want to nuke every card. And
if it has under MIN_COMPLETE_FRACTION of the people the last complete read
had, nobody is revoked for being missing this cycle: a read that short is
far likelier to be partial than a real mass departure.

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
from datetime import UTC, datetime

from ... import dryrun
from ...ag import AccessGrid, AccessGridError
from ...lib.pacs import PacsAdapter
from .. import tracking
from ..snapshot import Snapshot
from .writeback import deleted_identities_from_cards, identities_from_cards

logger = logging.getLogger(__name__)


def expired(cred) -> bool:
    """Has this credential passed the date the PACS gave it?

    Only ever True on a date the PACS actually stated: a credential with no
    expiry is not expired, and one whose date we could not parse arrives as
    None rather than as the epoch.
    """
    expires = getattr(cred, "deactivate_date", None)
    return expires is not None and expires < datetime.now(UTC)

# A destructive operation on the customer's own records, so it is rationed
# the way provisioning is. A cycle that wants to remove more than this has
# almost certainly misread something.
MAX_RETIREMENTS_PER_CYCLE = 25

# Deleting a pass because its card ran out of time is destructive and driven
# by a clock, so a date-format change or a badly imported batch could expire
# a whole population at once. Rationed for the same reason provisioning is.
MAX_EXPIRIES_PER_CYCLE = 25

# A read with fewer people than this share of the last complete one revokes
# nobody for being missing. The last complete read's size is kept here.
MIN_COMPLETE_FRACTION = 0.3
_LAST_COMPLETE_KEY = "last_complete_people_count"


def run(snapshot: Snapshot, ag: AccessGrid, pacs: PacsAdapter | None = None) -> int:
    logger.info("Phase 3: Checking for deletions")

    if not snapshot.people:
        logger.warning("Phase 3: PACS returned 0 people — skipping deletions for safety")
        return 0

    from ...settings_store import get_json, set_json

    previous = get_json(_LAST_COMPLETE_KEY) or 0
    complete = len(snapshot.people) >= previous * MIN_COMPLETE_FRACTION
    if complete:
        set_json(_LAST_COMPLETE_KEY, len(snapshot.people))
    else:
        logger.error(
            "Phase 3: the PACS returned %d people against %d in the last complete "
            "read — under %d%%, so nobody is revoked for being missing this cycle",
            len(snapshot.people), previous, int(MIN_COMPLETE_FRACTION * 100),
        )

    deleted = 0
    expired_this_cycle = 0
    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped") or not tracked.ag_card_id:
            continue

        person = snapshot.people.get(tracked.pacs_person_id)
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is not None and cred.hold:
            continue

        # A person still on the roster whose card page we could not read
        # this cycle tells us nothing, so act on nothing. A person who is
        # not on the roster at all is a different claim, made by a source we
        # did read — and one already guarded above, since an empty roster
        # abandons the phase. Checking these in the wrong order meant a
        # genuinely deleted cardholder was skipped along with the unreadable
        # ones, and their pass was never revoked.
        if person is not None and (
            tracked.pacs_person_id not in snapshot.credentials_by_person
        ):
            continue

        reason: str | None = None
        if not complete and (person is None or cred is None):
            continue
        if person is None:
            reason = f"person {tracked.pacs_person_id} no longer in PACS"
        elif cred is None:
            reason = f"credential {tracked.pacs_credential_id} no longer on person"
        elif expired(cred):
            # Whatever the Active box says. A card past its date is not a
            # credential any more, and the pass on the holder's phone is the
            # half the PACS cannot switch off by itself.
            if expired_this_cycle >= MAX_EXPIRIES_PER_CYCLE:
                logger.warning(
                    "Phase 3: reached the expiry cap (%d) — the rest wait for "
                    "the next cycle", MAX_EXPIRIES_PER_CYCLE,
                )
                continue
            expired_this_cycle += 1
            reason = (
                f"credential expired on "
                f"{cred.deactivate_date.strftime('%Y-%m-%d')}"
            )
        # cred.trigger_active=False is handled in phase 2 (terminate).

        if reason is None:
            continue

        # An expired card's pass is deleted, and the cards we wrote for it
        # go from the PACS too: re-issuing is then adding a trigger card, as
        # the first time. Read before the delete, while the pass is listed.
        expired_cards: list = []
        if (
            pacs is not None
            and getattr(pacs, "supports_credential_retirement", False)
            and cred is not None
            and expired(cred)
        ):
            expired_cards = snapshot.detailed_cards(
                ag, tracked.ag_card_id, tracked.pacs_person_id,
                tracked.pacs_credential_id, tracked.sync_ref,
            )

        try:
            logger.info(
                "  Deleting AG card %s for %s — %s",
                tracked.ag_card_id, tracking.label(tracked), reason,
            )
            if dryrun.active():
                dryrun.would(
                    "delete AG card %s for %s — %s",
                    tracked.ag_card_id, tracking.label(tracked), reason,
                )
                continue
            ag.access_cards.delete(card_id=tracked.ag_card_id)
            tracking.mark_deleted(
                tracked.pacs_person_id, tracked.pacs_credential_id,
            )
            deleted += 1
            if expired_cards:
                pacs.retire_credentials(
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                    identities_from_cards(expired_cards),
                )
        except AccessGridError as e:
            msg = str(e).lower()
            if "not found" in msg or "404" in msg:
                # Already gone — clean up.
                logger.debug("  AG card %s already gone — removing tracking row", tracked.ag_card_id)
                tracking.remove(tracked.pacs_person_id, tracked.pacs_credential_id)
            else:
                logger.error("  Failed to delete AG card %s: %s", tracked.ag_card_id, e)

    if pacs is not None:
        _retire_deleted_credentials(snapshot, ag, pacs)

    logger.info("Phase 3 done: %d deletion(s)", deleted)
    return deleted


def _retire_deleted_credentials(
    snapshot: Snapshot, ag: AccessGrid, pacs: PacsAdapter
) -> int:
    """Release PACS slots holding credentials AccessGrid has deleted.

    Only for adapters whose PACS receives credentials.

    Resolved the same way phase 4 resolves a pass, because one issue can
    span several cards and reading only the tracked id finds one of them:
    a re-issue leaves two cards sharing a sync_ref, each with its own card
    number and neither carrying the other in `details`. Fetching the tracked
    id alone reported the survivor and never mentioned the card that had
    been deleted, so its slot was never released.

    Absence is still not evidence. Only a card that is present *and* says
    `state: "deleted"` authorises anything; a pass that cannot be resolved,
    or a listing that came back short, releases nothing.
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

        cards = snapshot.detailed_cards(
            ag, tracked.ag_card_id, tracked.pacs_person_id,
            tracked.pacs_credential_id, tracked.sync_ref,
        )
        if not cards:
            continue

        gone = deleted_identities_from_cards(cards)
        if not gone:
            continue

        try:
            count = pacs.retire_credentials(
                tracked.pacs_person_id, tracked.pacs_credential_id, gone,
            )
        except Exception as e:  # noqa: BLE001 — one cardholder must not stop the cycle
            logger.error(
                "  Failed to retire credentials for %s: %s",
                tracking.label(tracked), e,
            )
            continue
        retired += count

    if retired:
        logger.info("Phase 3: released %d PACS credential(s) deleted in AccessGrid", retired)
    return retired
