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
from .writeback import is_dead, is_installed, push_allocated_identities, state_of

logger = logging.getLogger(__name__)

def _devices(card) -> list[str]:
    """One card's per-device credentials, for diagnostics.

    The watch's own card number lives here and nowhere else, so this is the
    line that says whether AccessGrid has issued one yet.
    """
    out = []
    for d in getattr(card, "devices", None) or []:
        get = d.get if isinstance(d, dict) else lambda k, _d=d: getattr(_d, k, None)
        out.append(
            f"{get('device_type') or '?'} {get('site_code') or '?'}/"
            f"{get('card_number') or '?'} {get('status') or '?'}"
        )
    return out


_AG_TO_CRED_STATUS: dict[str, CredentialStatus] = {
    "active": CredentialStatus.ACTIVE,
    "created": CredentialStatus.ACTIVE,
    "suspended": CredentialStatus.SUSPENDED,
}


def _desired_card_status(cards: list) -> CredentialStatus:
    """What a PACS card should be, given the pass behind it.

    Three outcomes, and the order matters. A suspended pass is still an
    installed one — a holder who loses their phone still has it installed —
    so asking "is it installed" first would read a revocation as a working
    credential and turn the card back on.
    """
    live = [c for c in cards if not is_dead(c)]
    if not live:
        # Every card of this issue is gone. Whatever the PACS still holds is
        # not backed by anything; phase 3 releases the slots, this stops the
        # card working in the meantime.
        return CredentialStatus.SUSPENDED
    if any(state_of(c) == "suspended" for c in live):
        return CredentialStatus.SUSPENDED
    if is_installed(live):
        return CredentialStatus.ACTIVE
    return CredentialStatus.AWAITING_INSTALL


_STATUS_REASON = {
    CredentialStatus.ACTIVE: ("Activating", "installed"),
    CredentialStatus.SUSPENDED: ("Deactivating", "suspended in AccessGrid"),
    CredentialStatus.AWAITING_INSTALL: ("Deactivating", "not installed yet"),
}


def _hold_uninstalled_inactive(snapshot: Snapshot, pacs: PacsAdapter, ag) -> int:
    """Make each PACS card say what its pass says.

    Only for adapters we write credentials into: elsewhere the card is the
    customer's own and its Active box is none of our business.

    Deliberately level-triggered, unlike the status loop below. The question
    is not "did anything change" but "does the card match the pass right
    now" — an operator can tick Active at any time, and a pass can be
    suspended and resumed without its state ever differing from what we last
    recorded.
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
            # The "no such credential" case is already named once per cycle
            # by _push_new_credentials, which walks the same rows.
            continue

        cards = snapshot.detailed_cards(
            ag, tracked.ag_card_id, tracked.pacs_person_id,
            tracked.pacs_credential_id, tracked.sync_ref,
        )
        if not cards:
            logger.warning(
                "  Not setting the card state for %s/%s — AG card %s did not "
                "resolve to any card",
                tracked.pacs_person_id, tracked.pacs_credential_id,
                tracked.ag_card_id,
            )
            continue

        desired = _desired_card_status(cards)
        # The adapter reads its own state off the card, where "off" has no
        # reason attached, so an awaited install and a suspension look the
        # same coming back.
        already = (
            cred.status is CredentialStatus.ACTIVE
            if desired is CredentialStatus.ACTIVE
            else cred.status is not CredentialStatus.ACTIVE
        )
        if already:
            continue

        try:
            verb, why = _STATUS_REASON[desired]
            logger.info(
                "  %s card for %s/%s — the pass is %s",
                verb, tracked.pacs_person_id, tracked.pacs_credential_id, why,
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
    _log_tracked_summary()
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
        # A credential we minted has its card driven by the install gate
        # above — one owner per checkbox. Here the card is the customer's
        # own, where an issued pass rightly leaves it active.
        if cred is not None and cred.allocate_identity:
            tracking.update_last_known_ag_state(
                tracked.pacs_person_id, tracked.pacs_credential_id, ag_state,
            )
            continue
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
        if cred is None:
            # The exit that hides a missing watch credential: nothing is
            # offered to the PACS, nothing is written, and nothing is said.
            logger.warning(
                "  Not writing credentials for %s/%s — the PACS reported no such "
                "credential this cycle",
                tracked.pacs_person_id, tracked.pacs_credential_id,
            )
            continue
        if not cred.allocate_identity:
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
            logger.warning(
                "  Not writing credentials for %s/%s — AG card %s did not resolve "
                "to any card (sync_ref %s)",
                tracked.pacs_person_id, tracked.pacs_credential_id,
                tracked.ag_card_id, tracked.sync_ref or "none",
            )
            continue
        logger.info(
            "  %s/%s resolved to %d AG card(s): %s",
            tracked.pacs_person_id, tracked.pacs_credential_id, len(cards),
            ", ".join(
                f"{getattr(c, 'id', '?')} {getattr(c, 'site_code', '?')}/"
                f"{getattr(c, 'card_number', '?')} {state_of(c) or '?'}"
                f" [{', '.join(_devices(c)) or 'no devices'}]"
                for c in cards
            ),
        )
        if push_allocated_identities(
            pacs, tracked.pacs_person_id, tracked.pacs_credential_id, cards,
        ):
            pushed += 1
    if pushed:
        logger.info("Phase 4: wrote credentials back for %d cardholder(s)", pushed)
    return pushed


def _log_tracked_summary() -> None:
    """One line naming every row phase 4 will consider, and its state."""
    rows = tracking.all_tracked()
    logger.info(
        "Phase 4: %d tracked row(s): %s",
        len(rows),
        "; ".join(
            f"{t.pacs_person_id}/{t.pacs_credential_id} status={t.status} "
            f"ag={t.ag_card_id or 'none'} last_known={t.last_known_ag_state or '-'}"
            for t in rows
        ) or "none",
    )
