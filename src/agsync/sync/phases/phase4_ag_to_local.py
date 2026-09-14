"""Phase 4 — AccessGrid → PACS status sync.

Direction-of-change check: an AG card whose state changed since
last_known_ag_state means an operator (or a card-holder unlinking from
their phone) acted on the AG side. Push the change back to PACS.

Phase 2 has already pushed PACS-side changes outward, so any remaining
divergence is AG-initiated.

This phase also carries the other AG → PACS traffic: for adapters whose
PACS *receives* credentials (the Seos direction), it re-offers each tracked
card's AccessGrid-allocated identities. Provisioning wrote the first one;
this is what catches the second, which only exists once the holder installs
the pass on a second device.
"""

from __future__ import annotations

import logging

from ...ag import AccessGrid
from ...lib.pacs import CredentialStatus, PacsAdapter
from .. import tracking
from ..snapshot import Snapshot
from .writeback import is_installed, push_allocated_identities

logger = logging.getLogger(__name__)

_AG_TO_CRED_STATUS: dict[str, CredentialStatus] = {
    "active": CredentialStatus.ACTIVE,
    "created": CredentialStatus.ACTIVE,
    "suspended": CredentialStatus.SUSPENDED,
}


def _hold_uninstalled_inactive(snapshot: Snapshot, pacs: PacsAdapter, ag) -> int:
    """Keep a PACS card inactive until its pass is on a device.

    Only for adapters we write credentials into: elsewhere the card is the
    customer's own and its Active box is none of our business.

    Deliberately level-triggered, unlike the status loop below. The question
    is not "did anything change" but "does the card match the pass right
    now", because the operator can tick Active at any time and the answer
    has to keep being no until an install happens.
    """
    if not getattr(pacs, "supports_credential_writeback", False):
        return 0

    changed = 0
    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped", "pending") or not tracked.ag_card_id:
            continue
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is None or not cred.allocate_identity:
            continue

        cards = snapshot.detailed_cards(
            ag, tracked.ag_card_id, tracked.pacs_person_id,
            tracked.pacs_credential_id, tracked.sync_ref,
        )
        if not cards:
            continue

        desired = (
            CredentialStatus.ACTIVE if is_installed(cards)
            else CredentialStatus.SUSPENDED
        )
        if cred.status == desired:
            continue

        try:
            logger.info(
                "  %s card for %s/%s — the pass is %s",
                "Activating" if desired is CredentialStatus.ACTIVE else "Deactivating",
                tracked.pacs_person_id, tracked.pacs_credential_id,
                "installed" if desired is CredentialStatus.ACTIVE else "not installed yet",
            )
            if pacs.update_credential_status(
                tracked.pacs_person_id, tracked.pacs_credential_id, desired,
            ):
                changed += 1
        except Exception as e:  # noqa: BLE001 — one cardholder must not stop the cycle
            logger.error(
                "  Could not set the card state for %s/%s: %s",
                tracked.pacs_person_id, tracked.pacs_credential_id, e,
            )
    if changed:
        logger.info("Phase 4: %d card(s) brought in line with their pass", changed)
    return changed


def run(snapshot: Snapshot, pacs: PacsAdapter, ag: AccessGrid | None = None) -> int:
    updated = _push_new_credentials(snapshot, pacs, ag)
    if ag is not None:
        updated += _hold_uninstalled_inactive(snapshot, pacs, ag)

    if not pacs.supports_status_writeback:
        logger.debug("Phase 4: PACS does not support status writeback — skipping")
        return updated

    logger.info("Phase 4: Checking AG → PACS status changes")

    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped") or not tracked.ag_card_id:
            continue

        ag_card = snapshot.resolve_ag_card(
            tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id,
            tracked.sync_ref,
        )
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


def _push_new_credentials(
    snapshot: Snapshot, pacs: PacsAdapter, ag: AccessGrid | None = None
) -> int:
    """Write any AG-allocated identities the PACS has not received yet.

    Only meaningful for adapters that receive credentials; everyone else
    returns immediately. Adapters skip identities already present, so the
    cost of re-offering is one read of the cardholder we would fetch anyway.
    """
    if not getattr(pacs, "supports_credential_writeback", False):
        return 0

    pushed = 0
    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped", "pending") or not tracked.ag_card_id:
            continue
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id, [])
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is None or not cred.allocate_identity:
            continue
        # All of them, each re-read: one issue can span several cards, and a
        # second device's card number lives on the pass's `devices`, which
        # the listing does not carry.
        cards = (
            snapshot.detailed_cards(
                ag, tracked.ag_card_id, tracked.pacs_person_id,
                tracked.pacs_credential_id, tracked.sync_ref,
            )
            if ag is not None
            else snapshot.resolve_ag_cards(
                tracked.ag_card_id, tracked.pacs_person_id,
                tracked.pacs_credential_id, tracked.sync_ref,
            )
        )
        if not cards:
            continue
        if push_allocated_identities(
            pacs, tracked.pacs_person_id, tracked.pacs_credential_id, cards,
        ):
            pushed += 1
    if pushed:
        logger.info("Phase 4: wrote credentials back for %d cardholder(s)", pushed)
    return pushed
