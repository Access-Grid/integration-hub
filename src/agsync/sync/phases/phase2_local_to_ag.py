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
from .phase3_deletions import expired

logger = logging.getLogger(__name__)

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
        if cred is None or cred.hold:
            # Gone from the PACS → phase 3 territory. Held → left alone.
            continue

        # Trigger removed → terminate the AG card.
        if not cred.trigger_active:
            try:
                logger.info(
                    "  Terminating AG card %s — trigger no longer set on %s",
                    tracked.ag_card_id, tracking.label(tracked),
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
        if not ag_state:
            # Nothing to compare against: the pass list was not read, or this
            # pass is not in it. Acting anyway suspended every card that was
            # off, unlinked and suspended passes included.
            logger.warning(
                "  Not pushing %s's PACS status (%s) to AG card %s — its "
                "AccessGrid state was not read this cycle",
                tracking.label(tracked), cred.status.value, tracked.ag_card_id,
            )
            continue

        # If AG state diverged from what we last knew, leave it for phase 4.
        if ag_state and ag_state != tracked.last_known_ag_state and tracked.last_known_ag_state:
            logger.debug(
                "  Skip %s — AG changed (%s → %s); phase 4 will reconcile",
                tracking.label(tracked),
                tracked.last_known_ag_state, ag_state,
            )
            continue

        # A card the PACS holds off only until the pass is installed is the
        # sync's own hold, not anyone revoking it: nothing to push. Seen live
        # when it was pushed: the holder installed the pass, and this phase
        # suspended it before phase 4 could switch the card on.
        #
        # Where an expired card suspends its pass (snapshot.suspends_expired),
        # the end date counts as the card being switched off, and a pass it
        # suspended is resumed once the date moves on — awaiting an install
        # or not, since the hub switches a Seos card off when its pass is.
        lapsed = snapshot.suspends_expired and expired(cred)
        resumed_date = (
            tracked.status == "expired" and not lapsed and ag_state == "suspended"
            and cred.status in (CredentialStatus.ACTIVE, CredentialStatus.AWAITING_INSTALL)
        )
        if cred.status == CredentialStatus.AWAITING_INSTALL and not lapsed and not resumed_date:
            continue

        if lapsed:
            desired = "suspended"
        elif resumed_date:
            desired = "active"
        else:
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
        #
        # And not for a deactivated identity: that is a revocation whatever
        # its cards say, and left alone the pass could still be installed.
        person = snapshot.people.get(tracked.pacs_person_id)
        if (
            desired == "suspended"
            and ag_state == "created"
            and getattr(cred, "allocate_identity", False)
            and (person is None or person.active)
            and not lapsed
        ):
            logger.debug(
                "  Skip %s — the pass is not installed yet, so its card is "
                "inactive by design",
                tracking.label(tracked),
            )
            continue

        try:
            if desired == "suspended" and ag_state in ("active", "created"):
                logger.info(
                    "  Suspending AG card %s (PACS credential %s is %s)",
                    tracked.ag_card_id, tracking.label(tracked),
                    f"expired on {cred.deactivate_date:%Y-%m-%d}" if lapsed else cred.status.value,
                )
                if dryrun.active():
                    dryrun.would(
                        "suspend AG card %s for %s/%s",
                        tracked.ag_card_id, tracked.pacs_person_id,
                        tracked.pacs_credential_id,
                    )
                    continue
                snapshot.pushed_to_ag.add((tracked.pacs_person_id, tracked.pacs_credential_id))
                ag.access_cards.suspend(card_id=tracked.ag_card_id)
                tracking.update_status(
                    tracked.pacs_person_id, tracked.pacs_credential_id,
                    # "expired" is what resumes it when the date moves on.
                    "expired" if lapsed else "suspended", last_known_ag_state="suspended",
                )
                updated += 1
            elif desired == "active" and ag_state == "suspended":
                logger.info(
                    "  Resuming AG card %s (PACS credential %s is %s)",
                    tracked.ag_card_id, tracking.label(tracked),
                    cred.status.value,
                )
                if dryrun.active():
                    dryrun.would(
                        "resume AG card %s for %s/%s",
                        tracked.ag_card_id, tracked.pacs_person_id,
                        tracked.pacs_credential_id,
                    )
                    continue
                snapshot.pushed_to_ag.add((tracked.pacs_person_id, tracked.pacs_credential_id))
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
