"""Reconcile one tracked pass at a time; only Seos activation requires an install."""

from __future__ import annotations

import logging

from ...ag import AccessGrid
from ...lib.pacs import CredentialStatus, PacsAdapter
from .. import tracking
from ..snapshot import Snapshot
from .writeback import is_installed, push_allocated_identities, state_of

logger = logging.getLogger(__name__)

_ACTIVE = {"active", "created"}
_OFF = {"suspended", "unlink", "unlinked", "deleted"}


def _desired_card_status(cards: list) -> CredentialStatus | None:
    """An active, installed member activates the pair; unknown states hold it."""
    active = [c for c in cards if state_of(c) in _ACTIVE]
    if active:
        return (CredentialStatus.ACTIVE if is_installed([c for c in active if state_of(c) == "active"])
                else CredentialStatus.AWAITING_INSTALL)
    if cards and all(state_of(c) in _OFF for c in cards):
        return CredentialStatus.SUSPENDED
    return None


def _pushed_this_cycle(snapshot: Snapshot, tracked) -> bool:
    return (tracked.pacs_person_id, tracked.pacs_credential_id) in snapshot.pushed_to_ag


def _cards(snapshot, tracked, ag=None):
    resolve = snapshot.detailed_cards if ag is not None else snapshot.resolve_ag_cards
    args = (tracked.ag_card_id, tracked.pacs_person_id, tracked.pacs_credential_id, tracked.sync_ref)
    return resolve(ag, *args) if ag is not None else resolve(*args)


def _set_status(snapshot, pacs, tracked, cred, cards, desired) -> int | None:
    if desired is None:
        logger.warning("  %s: unknown AccessGrid state; leaving Unity unchanged", tracking.label(tracked))
        return None
    if desired is CredentialStatus.AWAITING_INSTALL and any(
        getattr(c, "id", None) in snapshot.ag_card_detail_failed for c in cards
    ):
        logger.warning("  %s: AG card could not be re-read; installation unknown", tracking.label(tracked))
        return None
    person = snapshot.people.get(tracked.pacs_person_id)
    if desired is CredentialStatus.ACTIVE and person is not None and not person.active:
        return None
    already = (cred.status is CredentialStatus.ACTIVE) == (desired is CredentialStatus.ACTIVE)
    if already:
        return 0
    logger.info("  Pass %s for %s: %s → %s; members=%s", tracked.ag_card_id,
                tracking.label(tracked), cred.status.value, desired.value,
                [(getattr(c, "id", "?"), state_of(c)) for c in cards])
    return 1 if pacs.update_credential_status(
        tracked.pacs_person_id, tracked.pacs_credential_id, desired,
    ) else None


def run(snapshot: Snapshot, pacs: PacsAdapter, ag: AccessGrid | None = None) -> int:
    updated = 0
    logger.info("Phase 4: Reconciling AccessGrid passes with PACS credentials")
    for tracked in tracking.all_tracked():
        if tracked.status in ("deleted", "deduped", "pending") or not tracked.ag_card_id:
            continue
        if _pushed_this_cycle(snapshot, tracked):
            continue
        creds = snapshot.credentials_by_person.get(tracked.pacs_person_id)
        if creds is None:
            continue  # unread is never evidence of absence
        cred = next((c for c in creds if c.id == tracked.pacs_credential_id), None)
        if cred is not None and cred.hold:
            continue
        allocated = cred is not None and cred.allocate_identity
        cards = _cards(snapshot, tracked, ag)
        if not cards:
            logger.warning("  %s: pass %s could not be resolved", tracking.label(tracked), tracked.ag_card_id)
            continue
        card = snapshot.resolve_ag_card(tracked.ag_card_id, tracked.pacs_person_id,
                                        tracked.pacs_credential_id, tracked.sync_ref)
        state = state_of(card)
        if state == "deleted" and tracked.last_known_ag_state == "deleted" and getattr(
            pacs, "keeps_deleted_pass_tokens", False,
        ):
            # Deleted whole, and its card already switched off.
            updated += _reissue_if_switched_on(pacs, tracked)
            continue
        if not allocated and state == tracked.last_known_ag_state:
            continue
        try:
            if allocated:
                updated += int(push_allocated_identities(
                    pacs, tracked.pacs_person_id, tracked.pacs_credential_id, cards,
                ))
            gate = allocated and ag is not None and getattr(pacs, "supports_credential_writeback", False)
            if gate:
                desired = _desired_card_status(cards)
            else:
                desired = (CredentialStatus.ACTIVE if any(state_of(c) in _ACTIVE for c in cards)
                           else CredentialStatus.SUSPENDED if all(state_of(c) in _OFF for c in cards)
                           else None)
            if gate or (not allocated and state != tracked.last_known_ag_state):
                if cred is not None and pacs.supports_status_writeback:
                    changed = _set_status(snapshot, pacs, tracked, cred, cards, desired)
                    if changed is None:
                        continue
                    updated += changed
            if getattr(pacs, "keeps_deleted_pass_tokens", False) and state == "deleted":
                tracking.update_status(tracked.pacs_person_id, tracked.pacs_credential_id,
                                       "suspended", last_known_ag_state=state)
            else:
                (_record_state if allocated else _record_ag_state)(tracked, state)
        except Exception as e:  # one failing pass must not stop everyone else
            logger.error("  Pass %s for %s could not be reconciled: %s",
                         tracked.ag_card_id, tracking.label(tracked), e)
    logger.info("Phase 4 done: %d PACS update(s)", updated)
    return updated


def _reissue_if_switched_on(pacs, tracked) -> int:
    """A pass deleted whole in AccessGrid gets a new one once its card is switched back on.

    Its card was switched off when the pass was deleted. Switched on again in
    the PACS, the deleted pass is forgotten, and phase 1 of the next cycle
    issues a new one as for any enrollment.
    """
    try:
        switched_on = pacs.release_deleted_pass(tracked.pacs_person_id, tracked.pacs_credential_id)
    except Exception as e:  # noqa: BLE001 — one failing pass must not stop everyone else
        logger.error("  %s: could not tell whether the card of deleted pass %s is back on: %s",
                     tracking.label(tracked), tracked.ag_card_id, e)
        return 0
    if not switched_on:
        logger.debug("  %s: pass %s was deleted in AccessGrid; its card stays off",
                     tracking.label(tracked), tracked.ag_card_id)
        return 0
    logger.info("  %s: pass %s was deleted in AccessGrid and its card has been switched back on — "
                "a new pass is issued next cycle", tracking.label(tracked), tracked.ag_card_id)
    tracking.mark_deleted(tracked.pacs_person_id, tracked.pacs_credential_id)
    return 1


def _record_ag_state(tracked, ag_state: str) -> None:
    """Note the pass's state once the PACS card matches it.

    A pass deleted in AccessGrid is recorded as deleted, its id released,
    as when the hub deletes one: the card is off, and switching it back on
    in the PACS issues a new pass rather than resuming one that is gone.
    """
    if ag_state == "deleted":
        logger.info(
            "  AG pass %s for %s was deleted in AccessGrid — its PACS card is off; "
            "switching it on again issues a new pass",
            tracked.ag_card_id, tracking.label(tracked),
        )
        tracking.mark_deleted(tracked.pacs_person_id, tracked.pacs_credential_id)
        return
    _record_state(tracked, ag_state)


# The row status shown for a pass's AccessGrid state. "deleted" is left
# out on purpose: the row stays live until the hub itself releases it.
_ROW_STATUS = {
    "active": "active",
    "created": "active",
    "suspended": "suspended",
    "unlink": "suspended",
    "unlinked": "suspended",
}


def _record_state(tracked, ag_state: str) -> None:
    """Record the pass's AccessGrid state, and the row status that goes with it."""
    status = _ROW_STATUS.get(ag_state)
    if status is None or status == tracked.status:
        tracking.update_last_known_ag_state(
            tracked.pacs_person_id, tracked.pacs_credential_id, ag_state,
        )
        return
    logger.info(
        "  %s: now %s in AccessGrid — recorded as %s",
        tracking.label(tracked), ag_state, status,
    )
    tracking.update_status(
        tracked.pacs_person_id, tracked.pacs_credential_id, status,
        last_known_ag_state=ag_state,
    )
